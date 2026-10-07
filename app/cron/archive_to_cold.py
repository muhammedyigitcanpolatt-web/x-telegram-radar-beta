"""
app/cron/archive_to_cold.py
ClickHouse hot-to-cold archive

Moves raw signals older than 30 days from PostgreSQL to ClickHouse during
the scheduled archive cycle.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from app.database import db
from app.config import settings
from app.archived_cti_identity import ARCHIVED_CTI_LOCK_ID
from app.source_identity import normalize_source_record

logger = logging.getLogger("DataLakeArchiver")

CLICKHOUSE_URL = getattr(settings, "CLICKHOUSE_URL", "http://cti_clickhouse_lake:8123/")


class ColdDataLakeArchiver:
    BATCH_SIZE = 50_000
    LOCK_ID = ARCHIVED_CTI_LOCK_ID
    COLD_TABLE = "shortmox_cold_lake.historical_threat_signals"
    COLD_COLUMNS = (
        "id", "tweet_id", "platform", "channel_id", "source_message_id",
        "username", "raw_text", "admiralty_code", "confidence_score",
        "detected_at", "payload_json", "version",
    )

    def __init__(self, database=db, http_client_factory=httpx.AsyncClient,
                 clickhouse_url=CLICKHOUSE_URL):
        self.database = database
        self.http_client_factory = http_client_factory
        self.clickhouse_url = clickhouse_url

    async def archive_migration_cycle(self):
        """
        Move records older than 30 days from PostgreSQL to ClickHouse.
        """
        logger.warning(
            "🗄️ [CRON] Hot-to-cold archive cycle started."
        )

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        async with self.database.pool.acquire() as conn:
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", self.LOCK_ID
            )
            if not locked:
                logger.info("Another archive job is running; skipping this cycle.")
                return

            try:
                old_records = await conn.fetch(
                    """
                    SELECT signal_id, xmin::text AS row_version,
                           tweet_id, source_platform,
                           source_channel_id, source_message_id, username,
                           raw_text, text_hash, threat_category,
                           location_context, confidence_score, detected_at,
                           llm_analysis, visual_analysis, ai_enriched
                    FROM cartel_threat_signals
                    WHERE detected_at < $1
                    ORDER BY detected_at, signal_id
                    LIMIT $2;
                    """,
                    cutoff,
                    self.BATCH_SIZE,
                )
                if not old_records:
                    logger.info("No records are old enough to archive.")
                    return

                records_by_id = {int(row["signal_id"]): row for row in old_records}
                if len(records_by_id) != len(old_records):
                    raise RuntimeError("Archive batch contains duplicate signal IDs.")

                # Validate every source before writing to ClickHouse. Once the
                # hot row is removed, the canonical identity must remain
                # recoverable without guessing from username or raw text.
                source_identities = [
                    self._archived_identity(row) for row in old_records
                ]
                if len({source["tweet_id"] for source in source_identities}) != len(old_records):
                    raise RuntimeError("Archive batch contains duplicate source IDs")

                # Detect a restored/reseeded hot sequence before writing by
                # signal_id into ReplacingMergeTree. A later PG uniqueness
                # failure would roll back the hot delete but cannot undo a
                # ClickHouse row that displaced an older FINAL version.
                collisions = await conn.fetch(
                    """SELECT signal_id, tweet_id
                       FROM archived_cti_source_identities
                       WHERE signal_id = ANY($1::integer[])
                          OR tweet_id = ANY($2::text[])
                       LIMIT 1""",
                    [source["signal_id"] for source in source_identities],
                    [source["tweet_id"] for source in source_identities],
                )
                if collisions:
                    raise RuntimeError(
                        "Archive source or signal ID already exists in the cold identity ledger"
                    )

                # PostgreSQL nextval is nontransactional: a failed or timed-out
                # ClickHouse write can never reuse its version on retry.
                allocated = await conn.fetch(
                    "SELECT nextval('public.cold_write_version_seq'::regclass) AS version "
                    "FROM generate_series(1, $1::integer)",
                    len(old_records),
                )
                if len(allocated) != len(old_records):
                    raise RuntimeError("Cold archive version allocation count mismatch")
                clickhouse_payload = "".join(
                    json.dumps(
                        self._to_cold_row(row, version=int(revision["version"])),
                        ensure_ascii=False,
                    ) + "\n"
                    for row, revision in zip(old_records, allocated, strict=True)
                )
                query = (
                    "INSERT INTO shortmox_cold_lake."
                    "historical_threat_signals FORMAT JSONEachRow"
                )
                async with self.http_client_factory(timeout=120.0) as client:
                    response = await client.post(
                        self.clickhouse_url,
                        params={"query": query, "async_insert": "0"},
                        content=clickhouse_payload,
                    )

                if response.status_code != 200:
                    raise RuntimeError(
                        f"ClickHouse archive transfer failed: "
                        f"HTTP {response.status_code} — {response.text[:500]}"
                    )

                # The ClickHouse table uses ReplacingMergeTree(version) keyed by
                # signal_id; retries have higher versions. Delete only the exact row
                # versions copied above. A concurrent enrichment changes xmin,
                # so a stale cold copy cannot replace the updated hot record.
                signal_ids = list(records_by_id)
                row_versions = [
                    str(records_by_id[signal_id]["row_version"])
                    for signal_id in signal_ids
                ]
                async with conn.transaction():
                    result = await conn.execute(
                        """
                        DELETE FROM cartel_threat_signals AS hot
                        USING unnest($1::integer[], $2::text[]) AS copied(signal_id, row_version)
                        WHERE hot.signal_id = copied.signal_id
                          AND hot.xmin::text = copied.row_version
                          AND hot.detected_at < $3;
                        """,
                        signal_ids,
                        row_versions,
                        cutoff,
                    )
                    deleted = int(result.rsplit(" ", 1)[-1])
                    if deleted != len(signal_ids):
                        raise RuntimeError(
                            f"Archive deletion count mismatch: "
                            f"selected={len(signal_ids)}, deleted={deleted}"
                        )
                    # A plain INSERT deliberately fails on either a source or
                    # signal-ID conflict. PostgreSQL then restores the deleted
                    # hot rows along with the whole transaction.
                    result = await conn.execute(
                        """
                        INSERT INTO archived_cti_source_identities
                            (tweet_id, signal_id, source_platform,
                             source_channel_id, source_message_id, raw_text_sha256)
                        SELECT source.tweet_id, source.signal_id,
                               source.source_platform, source.source_channel_id,
                               source.source_message_id, source.raw_text_sha256
                        FROM unnest($1::text[], $2::integer[], $3::text[],
                                    $4::text[], $5::text[], $6::text[]) AS source(
                            tweet_id, signal_id, source_platform,
                            source_channel_id, source_message_id, raw_text_sha256
                        );
                        """,
                        [source["tweet_id"] for source in source_identities],
                        [source["signal_id"] for source in source_identities],
                        [source["source_platform"] for source in source_identities],
                        [source["source_channel_id"] for source in source_identities],
                        [source["source_message_id"] for source in source_identities],
                        [source["raw_text_sha256"] for source in source_identities],
                    )
                    inserted = int(result.rsplit(" ", 1)[-1])
                    if inserted != len(signal_ids):
                        raise RuntimeError(
                            f"Archive source identity count mismatch: "
                            f"selected={len(signal_ids)}, inserted={inserted}"
                        )

                logger.warning(
                    "[DATA LAKE SUCCESS] Transferred %s records and deleted the corresponding hot rows.",
                    len(signal_ids),
                )
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", self.LOCK_ID)

    @staticmethod
    def _archived_identity(row):
        tweet_id = row["tweet_id"]
        platform = row["source_platform"]
        channel_id = row["source_channel_id"]
        message_id = row["source_message_id"]
        raw_text = row["raw_text"]
        signal_id = row["signal_id"]
        if (
            type(signal_id) is not int or signal_id <= 0
            or not isinstance(tweet_id, str) or not tweet_id
            or not isinstance(platform, str) or not platform
            or not isinstance(channel_id, str)
            or not isinstance(message_id, str) or not message_id
            or not isinstance(raw_text, str)
        ):
            raise RuntimeError("Archive source identity is incomplete")
        try:
            canonical = normalize_source_record({
                "tweet_id": tweet_id,
                "source_platform": platform,
                "source_channel_id": channel_id,
                "source_message_id": message_id,
            })
        except ValueError as exc:
            raise RuntimeError("Archive source identity is ambiguous") from exc
        if any(canonical[key] != value for key, value in (
            ("tweet_id", tweet_id),
            ("source_platform", platform),
            ("source_channel_id", channel_id),
            ("source_message_id", message_id),
        )):
            raise RuntimeError("Archive source identity is ambiguous")
        return {
            "tweet_id": tweet_id,
            "signal_id": signal_id,
            "source_platform": platform,
            "source_channel_id": channel_id,
            "source_message_id": message_id,
            "raw_text_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _to_cold_row(row, *, version=1):
        if type(version) is not int or not 1 <= version < 2**64:
            raise ValueError("Cold archive version must fit ClickHouse UInt64")
        username = str(row["username"] or "")
        platform = row.get("source_platform") if hasattr(row, "get") else None
        detected_at = row["detected_at"]
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=timezone.utc)
        source_record = {
            "signal_id": int(row["signal_id"]),
            "tweet_id": str(row["tweet_id"]),
            "source_platform": platform or ("TELEGRAM" if username.startswith("tg_") else "X_TWITTER"),
            "source_channel_id": str(row.get("source_channel_id", "") or ""),
            "source_message_id": str(row.get("source_message_id", row["tweet_id"]) or row["tweet_id"]),
            "username": username,
            "raw_text": str(row["raw_text"] or ""),
            "text_hash": row["text_hash"],
            "threat_category": row["threat_category"],
            "location_context": row["location_context"],
            "confidence_score": row["confidence_score"],
            "detected_at": detected_at.isoformat(),
            "llm_analysis": _jsonb_value(row["llm_analysis"]),
            "visual_analysis": _jsonb_value(row["visual_analysis"]),
            "ai_enriched": row["ai_enriched"],
        }
        return {
            "id": str(row["signal_id"]),
            # A delayed retry of this archive copy must lose to any later
            # versioned cold enrichment for the same signal ID.
            "version": version,
            "tweet_id": str(row["tweet_id"]),
            "platform": source_record["source_platform"],
            "channel_id": str(row.get("source_channel_id", "") or ""),
            "source_message_id": str(row.get("source_message_id", row["tweet_id"]) or row["tweet_id"]),
            "username": username,
            "raw_text": source_record["raw_text"],
            "admiralty_code": "F6",
            "confidence_score": int(row["confidence_score"] or 0),
            "detected_at": detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "payload_json": json.dumps(source_record, ensure_ascii=False, default=_json_default),
        }

    async def enrich_archived_signal(
        self, tweet_id, raw_text, text_analysis, visual_analysis, *, version
    ):
        """Append and verify an enriched cold row while the caller holds LOCK_ID.

        The same PostgreSQL advisory lock guards archive copy/delete and this
        read/append pair. Every write also gets a durable sequence version so
        an insert completing after an HTTP timeout cannot replace newer data.
        """
        if not tweet_id or not isinstance(raw_text, str):
            raise ValueError("An exact source ID and raw text are required")
        if type(version) is not int or not 1 <= version < 2**64:
            raise ValueError("A positive cold write version is required")

        columns = ", ".join(self.COLD_COLUMNS)
        lookup = (
            f"SELECT {columns} FROM {self.COLD_TABLE} FINAL "
            "WHERE tweet_id = {tweet_id:String} LIMIT 2 FORMAT JSONEachRow"
        )
        verify = (
            f"SELECT {columns} FROM {self.COLD_TABLE} FINAL "
            "WHERE id = {signal_id:String} LIMIT 2 FORMAT JSONEachRow"
        )

        async with self.http_client_factory(timeout=30.0) as client:
            response = await client.get(
                self.clickhouse_url,
                params={"query": lookup, "param_tweet_id": tweet_id},
            )
            rows = self._parse_cold_rows(response, "lookup")
            if len(rows) != 1:
                raise RuntimeError(
                    f"Cold enrichment requires exactly one signal; found {len(rows)}"
                )

            original = rows[0]
            signal_id = str(original.get("id", ""))
            try:
                parsed_id = int(signal_id)
                payload = json.loads(original["payload_json"])
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeError("Cold enrichment source payload is malformed") from exc
            try:
                original_version = self._cold_version(original.get("version"))
            except ValueError as exc:
                raise RuntimeError("Cold enrichment source identity or text does not match") from exc
            if (
                parsed_id <= 0
                or str(parsed_id) != signal_id
                or not isinstance(payload, dict)
                or payload.get("signal_id") != parsed_id
                or original.get("tweet_id") != tweet_id
                or payload.get("tweet_id") != tweet_id
                or original.get("raw_text") != raw_text
                or payload.get("raw_text") != raw_text
                or any(column not in original for column in self.COLD_COLUMNS)
            ):
                raise RuntimeError("Cold enrichment source identity or text does not match")
            if version <= original_version:
                raise RuntimeError("Cold enrichment version must advance")

            updated_payload = dict(payload)
            updated_payload.update(
                llm_analysis=text_analysis,
                visual_analysis=visual_analysis,
                ai_enriched=True,
            )
            updated = {column: original[column] for column in self.COLD_COLUMNS}
            updated["version"] = version
            updated["payload_json"] = json.dumps(
                updated_payload, ensure_ascii=False, default=_json_default
            )
            response = await client.post(
                self.clickhouse_url,
                params={
                    "query": f"INSERT INTO {self.COLD_TABLE} FORMAT JSONEachRow",
                    "async_insert": "0",
                },
                content=json.dumps(updated, ensure_ascii=False) + "\n",
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"ClickHouse cold enrichment insert failed: HTTP {response.status_code}"
                )

            response = await client.get(
                self.clickhouse_url,
                params={"query": verify, "param_signal_id": signal_id},
            )
            verified = self._parse_cold_rows(response, "verification")
            if len(verified) != 1:
                raise RuntimeError("Cold enrichment verification did not find one signal")
            try:
                verified_payload = json.loads(verified[0]["payload_json"])
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeError("Cold enrichment verification payload is malformed") from exc
            try:
                verified_version = self._cold_version(verified[0].get("version"))
            except ValueError as exc:
                raise RuntimeError("Cold enrichment verification did not match inserted data") from exc
            if (
                any(verified[0].get(column) != updated[column]
                    for column in self.COLD_COLUMNS if column not in {"payload_json", "version"})
                or verified_version != version
                or verified_payload != updated_payload
            ):
                raise RuntimeError("Cold enrichment verification did not match inserted data")
            return signal_id

    @staticmethod
    def _cold_version(value):
        """Accept ClickHouse UInt64 in either supported JSONEachRow representation."""
        if type(value) is int:
            version = value
        elif (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and 1 <= len(value) <= 20
        ):
            version = int(value)
            if str(version) != value:
                raise ValueError("Non-canonical cold version")
        else:
            raise ValueError("Invalid cold version")
        if not 1 <= version < 2**64:
            raise ValueError("Cold version outside UInt64 range")
        return version

    @staticmethod
    def _parse_cold_rows(response, phase):
        if response.status_code != 200:
            raise RuntimeError(
                f"ClickHouse cold enrichment {phase} failed: HTTP {response.status_code}"
            )
        try:
            rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"ClickHouse cold enrichment {phase} response is malformed") from exc
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError(f"ClickHouse cold enrichment {phase} response is malformed")
        return rows

    async def query_cold_lake(self, username: str, days: int = 365):
        """
        Query historical analytics from ClickHouse.
        """
        if not 1 <= days <= 3650:
            raise ValueError("days must be between 1 and 3650")
        query = (
            "SELECT * FROM shortmox_cold_lake.historical_threat_signals FINAL "
            "WHERE username = {username:String} "
            "AND detected_at > now() - INTERVAL {days:UInt16} DAY "
            "ORDER BY detected_at DESC LIMIT 100 FORMAT JSONEachRow"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                self.clickhouse_url,
                params={"query": query, "param_username": username, "param_days": days},
            )
            if resp.status_code == 200:
                rows = [
                    json.loads(line)
                    for line in resp.text.strip().split("\n")
                    if line.strip()
                ]
                return rows
            logger.error(f"Cold archive query failed: {resp.text[:500]}")
            return []


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__str__"):
        return str(value)
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _jsonb_value(value):
    """Restore the JSON value that asyncpg returns as serialized JSONB text."""
    if isinstance(value, str):
        return json.loads(value)
    return value
