"""One-time, offline backfill of archived CTI source identities.

The source is the visible ClickHouse FINAL row for each signal ID. This tool
only creates a PostgreSQL tombstone when the cold row carries a complete,
internally consistent canonical source identity. It never guesses identities
for legacy rows whose source fields were discarded by an older migration.

Run with the analyzer and archive writers stopped. The default is a dry run:
it creates a session-local temporary staging table but changes no persistent
rows. ``--apply`` is required to insert tombstones.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import asyncpg
import httpx

from app.config import settings
from app.cron.archive_to_cold import ColdDataLakeArchiver
from app.source_identity import normalize_source_record


class BackfillError(RuntimeError):
    """An ambiguous source or unsafe backfill state stopped the operation."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackfillError(f"Cold payload contains duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class ArchivedIdentity:
    tweet_id: str
    signal_id: int
    source_platform: str
    source_channel_id: str
    source_message_id: str
    raw_text_sha256: str

    def values(self) -> tuple[str, int, str, str, str, str]:
        return (
            self.tweet_id,
            self.signal_id,
            self.source_platform,
            self.source_channel_id,
            self.source_message_id,
            self.raw_text_sha256,
        )


@dataclass(frozen=True)
class BackfillSummary:
    cold_rows: int
    already_present: int
    would_insert: int
    inserted: int
    applied: bool


def validate_cold_row(row: Mapping[str, Any]) -> ArchivedIdentity:
    """Reject legacy, conflicting, or noncanonical source identities."""
    try:
        signal_id_text = row["id"]
        payload = json.loads(
            row["payload_json"], object_pairs_hook=_unique_json_object
        )
        tweet_id = row["tweet_id"]
        platform = row["platform"]
        channel_id = row["channel_id"]
        message_id = row["source_message_id"]
        raw_text = row["raw_text"]
    except (KeyError, TypeError, ValueError) as exc:
        raise BackfillError("Cold row has missing or malformed identity fields") from exc

    if (
        not isinstance(signal_id_text, str)
        or not signal_id_text.isascii()
        or not signal_id_text.isdecimal()
        or not 0 < int(signal_id_text) <= 2_147_483_647
        or str(int(signal_id_text)) != signal_id_text
        or not isinstance(payload, dict)
        or not isinstance(tweet_id, str)
        or not tweet_id
        or not isinstance(platform, str)
        or not platform
        or not isinstance(channel_id, str)
        or not isinstance(message_id, str)
        or not message_id
        or not isinstance(raw_text, str)
    ):
        raise BackfillError("Cold row has a blank, ambiguous, or invalid source identity")

    signal_id = int(signal_id_text)
    if (
        type(payload.get("signal_id")) is not int
        or payload["signal_id"] != signal_id
        or payload.get("tweet_id") != tweet_id
        or payload.get("source_platform") != platform
        or payload.get("source_channel_id") != channel_id
        or payload.get("source_message_id") != message_id
        or payload.get("raw_text") != raw_text
    ):
        raise BackfillError("Cold top-level and payload identities or raw text disagree")

    try:
        normalized = normalize_source_record({
            "tweet_id": tweet_id,
            "source_platform": platform,
            "source_channel_id": channel_id,
            "source_message_id": message_id,
        })
    except ValueError as exc:
        raise BackfillError("Cold source identity is not canonical") from exc
    if any(normalized[key] != expected for key, expected in (
        ("tweet_id", tweet_id),
        ("source_platform", platform),
        ("source_channel_id", channel_id),
        ("source_message_id", message_id),
    )):
        raise BackfillError("Cold source identity is not canonical")

    return ArchivedIdentity(
        tweet_id=tweet_id,
        signal_id=signal_id,
        source_platform=platform,
        source_channel_id=channel_id,
        source_message_id=message_id,
        raw_text_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
    )


_COLD_QUERY = (
    "SELECT id, tweet_id, platform, channel_id, source_message_id, "
    "raw_text, payload_json "
    "FROM shortmox_cold_lake.historical_threat_signals FINAL FORMAT JSONEachRow"
)
_COLD_COUNT_QUERY = (
    "SELECT count() AS total "
    "FROM shortmox_cold_lake.historical_threat_signals FINAL FORMAT JSONEachRow"
)

_CREATE_STAGE = """
CREATE TEMP TABLE archived_cti_backfill_stage (
    tweet_id TEXT PRIMARY KEY,
    signal_id INTEGER UNIQUE NOT NULL,
    source_platform TEXT NOT NULL,
    source_channel_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    raw_text_sha256 CHAR(64) NOT NULL
) ON COMMIT PRESERVE ROWS;
"""

_STAGE_BATCH = """
INSERT INTO archived_cti_backfill_stage
    (tweet_id, signal_id, source_platform, source_channel_id,
     source_message_id, raw_text_sha256)
VALUES ($1, $2, $3, $4, $5, $6)
"""

_CONFLICT_COUNT = """
SELECT count(*) FROM archived_cti_backfill_stage AS candidate
JOIN archived_cti_source_identities AS existing
  ON existing.tweet_id = candidate.tweet_id
  OR existing.signal_id = candidate.signal_id
WHERE existing.tweet_id IS DISTINCT FROM candidate.tweet_id
   OR existing.signal_id IS DISTINCT FROM candidate.signal_id
   OR existing.source_platform IS DISTINCT FROM candidate.source_platform
   OR existing.source_channel_id IS DISTINCT FROM candidate.source_channel_id
   OR existing.source_message_id IS DISTINCT FROM candidate.source_message_id
   OR existing.raw_text_sha256 IS DISTINCT FROM candidate.raw_text_sha256;
"""

_HOT_OVERLAP_COUNT = """
SELECT count(*) FROM archived_cti_backfill_stage AS candidate
JOIN cartel_threat_signals AS hot
  ON hot.tweet_id = candidate.tweet_id OR hot.signal_id = candidate.signal_id;
"""

_MAX_SIGNAL_ID = """
SELECT GREATEST(
    COALESCE((SELECT max(signal_id) FROM cartel_threat_signals), 0),
    COALESCE((SELECT max(signal_id) FROM archived_cti_source_identities), 0),
    COALESCE((SELECT max(signal_id) FROM archived_cti_backfill_stage), 0)
);
"""


async def _check_signal_id_sequence(conn) -> None:
    """Refuse a restore whose next hot ID could overwrite a cold signal ID."""
    sequence = await conn.fetchrow(
        "SELECT last_value, is_called "
        "FROM public.cartel_threat_signals_signal_id_seq"
    )
    definition = await conn.fetchrow(
        "SELECT seqincrement, seqcycle FROM pg_sequence "
        "WHERE seqrelid = 'public.cartel_threat_signals_signal_id_seq'::regclass"
    )
    if (
        sequence is None or definition is None
        or type(sequence["last_value"]) is not int
        or type(sequence["is_called"]) is not bool
        or definition["seqincrement"] != 1
        or definition["seqcycle"] is not False
    ):
        raise BackfillError("Hot CTI signal-ID sequence is unavailable or unsafe")
    next_signal_id = sequence["last_value"] + int(sequence["is_called"])
    max_signal_id = int(await conn.fetchval(_MAX_SIGNAL_ID))
    if next_signal_id <= max_signal_id:
        raise BackfillError(
            "Hot CTI signal-ID sequence is behind existing hot/cold IDs: "
            f"next={next_signal_id}, highest={max_signal_id}; "
            "advance it during maintenance and rerun the dry run"
        )

_EXISTING_COUNT = """
SELECT count(*) FROM archived_cti_backfill_stage AS candidate
JOIN archived_cti_source_identities AS existing
  ON existing.tweet_id = candidate.tweet_id;
"""

_APPLY = """
INSERT INTO archived_cti_source_identities
    (tweet_id, signal_id, source_platform, source_channel_id,
     source_message_id, raw_text_sha256)
SELECT candidate.tweet_id, candidate.signal_id, candidate.source_platform,
       candidate.source_channel_id, candidate.source_message_id,
       candidate.raw_text_sha256
FROM archived_cti_backfill_stage AS candidate
WHERE NOT EXISTS (
    SELECT 1 FROM archived_cti_source_identities AS existing
    WHERE existing.tweet_id = candidate.tweet_id
);
"""


async def _cold_row_count(http_client: httpx.AsyncClient, clickhouse_url: str) -> int:
    """Read an independent FINAL row count to detect incomplete streams."""
    response = await http_client.get(
        clickhouse_url, params={"query": _COLD_COUNT_QUERY}
    )
    if response.status_code != 200:
        raise BackfillError(
            f"ClickHouse cold count failed with HTTP {response.status_code}"
        )
    lines = [line for line in response.text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BackfillError("ClickHouse cold count returned an invalid result")
    try:
        value = json.loads(lines[0])["total"]
    except (TypeError, ValueError, KeyError) as exc:
        raise BackfillError("ClickHouse cold count returned an invalid result") from exc
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise BackfillError("ClickHouse cold count returned an invalid result")


async def backfill_archived_identities(
    conn,
    http_client: httpx.AsyncClient,
    *,
    clickhouse_url: str,
    apply: bool = False,
    batch_size: int = 1000,
) -> BackfillSummary:
    """Scan once, validate every row, then atomically apply validated rows.

    A session-local PostgreSQL stage bounds Python memory while its unique
    constraints catch duplicate source and signal IDs in the cold lake.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    locked = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1)", ColdDataLakeArchiver.LOCK_ID
    )
    if not locked:
        raise BackfillError("Archive or enrichment writer holds the advisory lock")

    stage_created = False
    try:
        expected_rows = await _cold_row_count(http_client, clickhouse_url)
        await conn.execute(_CREATE_STAGE)
        stage_created = True
        cold_rows = 0
        batch: list[tuple[str, int, str, str, str, str]] = []
        async with http_client.stream(
            "GET", clickhouse_url, params={"query": _COLD_QUERY}
        ) as response:
            if response.status_code != 200:
                raise BackfillError(
                    f"ClickHouse cold scan failed with HTTP {response.status_code}"
                )
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise BackfillError("ClickHouse cold scan returned malformed JSON") from exc
                identity = validate_cold_row(parsed)
                batch.append(identity.values())
                cold_rows += 1
                if len(batch) >= batch_size:
                    await _stage_rows(conn, batch)
                    batch.clear()
        if batch:
            await _stage_rows(conn, batch)

        final_rows = await _cold_row_count(http_client, clickhouse_url)
        if cold_rows != expected_rows or final_rows != expected_rows:
            raise BackfillError(
                "ClickHouse FINAL row count changed or cold scan was incomplete: "
                f"before={expected_rows}, received={cold_rows}, after={final_rows}"
            )

        async with conn.transaction(readonly=not apply):
            await _check_signal_id_sequence(conn)
            hot_overlap = int(await conn.fetchval(_HOT_OVERLAP_COUNT))
            if hot_overlap:
                raise BackfillError(
                    f"{hot_overlap} cold identities still overlap hot CTI rows"
                )
            conflicts = int(await conn.fetchval(_CONFLICT_COUNT))
            if conflicts:
                raise BackfillError(
                    f"{conflicts} cold identities conflict with existing tombstones"
                )
            already_present = int(await conn.fetchval(_EXISTING_COUNT))
            would_insert = cold_rows - already_present
            inserted = 0
            if apply:
                try:
                    result = await conn.execute(_APPLY)
                except asyncpg.UniqueViolationError as exc:
                    raise BackfillError(
                        "Tombstone identity changed during apply; transaction rolled back"
                    ) from exc
                inserted = int(result.rsplit(" ", 1)[-1])
                if inserted != would_insert:
                    raise BackfillError("Backfill insert count changed; transaction rolled back")

        return BackfillSummary(
            cold_rows=cold_rows,
            already_present=already_present,
            would_insert=would_insert,
            inserted=inserted,
            applied=apply,
        )
    finally:
        try:
            if stage_created:
                await conn.execute("DROP TABLE IF EXISTS pg_temp.archived_cti_backfill_stage")
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", ColdDataLakeArchiver.LOCK_ID
            )


async def _stage_rows(conn, rows: list[tuple[str, int, str, str, str, str]]) -> None:
    try:
        await conn.executemany(_STAGE_BATCH, rows)
    except asyncpg.UniqueViolationError as exc:
        raise BackfillError("Cold lake has duplicate tweet_id or signal_id") from exc


async def _run_cli(*, apply: bool) -> BackfillSummary:
    if not settings.POSTGRES_PASSWORD:
        raise BackfillError("POSTGRES_PASSWORD must be configured")
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as client:
            return await backfill_archived_identities(
                conn, client, clickhouse_url=settings.CLICKHOUSE_URL, apply=apply
            )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write validated tombstones; without this flag only a dry run is performed",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(_run_cli(apply=args.apply))
    except BackfillError as exc:
        parser.exit(2, f"Backfill refused: {exc}\n")
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
