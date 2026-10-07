import hashlib
import json
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

from app.cron.backfill_archived_cti_identities import (
    BackfillError,
    backfill_archived_identities,
    validate_cold_row,
)


def cold_row(
    signal_id=17,
    tweet_id="telegram:-100123:42",
    platform="TELEGRAM",
    channel_id="-100123",
    message_id="42",
    raw_text="fixture text",
):
    payload = {
        "signal_id": signal_id,
        "tweet_id": tweet_id,
        "source_platform": platform,
        "source_channel_id": channel_id,
        "source_message_id": message_id,
        "raw_text": raw_text,
    }
    return {
        "id": str(signal_id),
        "tweet_id": tweet_id,
        "platform": platform,
        "channel_id": channel_id,
        "source_message_id": message_id,
        "raw_text": raw_text,
        "payload_json": json.dumps(payload),
    }


def client_for(rows, *, status=200, reported_count=None):
    def handler(request):
        assert request.method == "GET"
        assert "historical_threat_signals FINAL" in request.url.params["query"]
        if "count() AS total" in request.url.params["query"]:
            total = len(rows) if reported_count is None else reported_count
            return httpx.Response(status, text=json.dumps({"total": str(total)}) + "\n")
        content = "\n".join(json.dumps(row) for row in rows) + "\n"
        return httpx.Response(status, text=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeConnection:
    def __init__(self):
        self.stage = []
        self.ledger = {}
        self.hot = set()
        self.locked = False
        self.unlocks = 0
        self.writes = 0
        self.sequence_last_value = 100
        self.sequence_is_called = True

    async def fetchrow(self, sql, *_args):
        if "FROM public.cartel_threat_signals_signal_id_seq" in sql:
            return {
                "last_value": self.sequence_last_value,
                "is_called": self.sequence_is_called,
            }
        if "FROM pg_sequence" in sql:
            return {"seqincrement": 1, "seqcycle": False}
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            assert len(args) == 1
            self.locked = True
            return True
        if "JOIN cartel_threat_signals AS hot" in sql:
            return sum(
                (candidate[0] in self.hot or candidate[1] in self.hot)
                for candidate in self.stage
            )
        if "SELECT GREATEST(" in sql:
            return max(
                [0, *[candidate[1] for candidate in self.stage],
                 *[candidate[1] for candidate in self.ledger.values()],
                 *[value for value in self.hot if isinstance(value, int)]]
            )
        if "IS DISTINCT FROM" in sql:
            return sum(
                any(
                    existing[0] == candidate[0] or existing[1] == candidate[1]
                    for existing in self.ledger.values()
                )
                and candidate not in self.ledger.values()
                for candidate in self.stage
            )
        if "JOIN archived_cti_source_identities AS existing" in sql:
            return sum(candidate[0] in self.ledger for candidate in self.stage)
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "CREATE TEMP TABLE" in sql:
            self.stage = []
            return "CREATE TABLE"
        if "DROP TABLE IF EXISTS pg_temp" in sql:
            self.stage = []
            return "DROP TABLE"
        if "pg_advisory_unlock" in sql:
            assert self.locked
            self.locked = False
            self.unlocks += 1
            return "SELECT 1"
        if "INSERT INTO archived_cti_source_identities" in sql:
            count = 0
            for candidate in self.stage:
                if candidate[0] not in self.ledger:
                    self.ledger[candidate[0]] = candidate
                    count += 1
            self.writes += count
            return f"INSERT 0 {count}"
        raise AssertionError(sql)

    async def executemany(self, sql, rows):
        assert "INSERT INTO archived_cti_backfill_stage" in sql
        for candidate in rows:
            if any(
                old[0] == candidate[0] or old[1] == candidate[1]
                for old in self.stage
            ):
                raise asyncpg.UniqueViolationError("duplicate cold identity")
            self.stage.append(candidate)

    @asynccontextmanager
    async def transaction(self, *, readonly):
        snapshot = dict(self.ledger)
        try:
            yield
        except Exception:
            self.ledger = snapshot
            raise


def test_validate_complete_canonical_identity_and_exact_empty_text():
    telegram = validate_cold_row(cold_row())
    assert telegram.tweet_id == "telegram:-100123:42"
    assert telegram.signal_id == 17
    assert telegram.raw_text_sha256 == hashlib.sha256(b"fixture text").hexdigest()

    x_record = cold_row(
        signal_id=18,
        tweet_id="12345",
        platform="X_TWITTER",
        channel_id="",
        message_id="12345",
        raw_text="",
    )
    assert validate_cold_row(x_record).raw_text_sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(tweet_id=""),
        lambda row: row.update(source_message_id=""),
        lambda row: row.update(raw_text=None),
        lambda row: row.update(id="017"),
        lambda row: row.update(platform="X"),
        lambda row: row.update(tweet_id="telegram:-100123:other"),
        lambda row: row.update(payload_json="not-json"),
        lambda row: row.update(payload_json=json.dumps({"signal_id": 17})),
    ],
)
def test_validate_rejects_blank_legacy_and_mismatched_identity(mutate):
    row = cold_row()
    mutate(row)
    with pytest.raises(BackfillError):
        validate_cold_row(row)


def test_validate_rejects_duplicate_payload_identity_key():
    row = cold_row()
    row["payload_json"] = row["payload_json"].replace(
        '"tweet_id": "telegram:-100123:42",',
        '"tweet_id": "different", "tweet_id": "telegram:-100123:42",',
    )
    with pytest.raises(BackfillError, match="duplicate JSON key: tweet_id"):
        validate_cold_row(row)


@pytest.mark.asyncio
async def test_dry_run_apply_and_idempotent_rerun():
    conn = FakeConnection()
    rows = [cold_row(), cold_row(
        signal_id=18, tweet_id="12345", platform="X_TWITTER",
        channel_id="", message_id="12345",
    )]
    async with client_for(rows) as client:
        preview = await backfill_archived_identities(
            conn, client, clickhouse_url="http://clickhouse.test/", batch_size=1
        )
        assert preview.cold_rows == 2
        assert preview.already_present == 0
        assert preview.would_insert == 2
        assert preview.inserted == 0
        assert preview.applied is False
        assert conn.ledger == {}
        assert conn.stage == []

        applied = await backfill_archived_identities(
            conn, client, clickhouse_url="http://clickhouse.test/",
            apply=True, batch_size=1,
        )
        assert applied.inserted == 2
        assert len(conn.ledger) == 2

        rerun = await backfill_archived_identities(
            conn, client, clickhouse_url="http://clickhouse.test/", apply=True
        )
        assert rerun.already_present == 2
        assert rerun.would_insert == 0
        assert rerun.inserted == 0
        assert conn.writes == 2
        assert conn.unlocks == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["hot", "ledger", "signal_id_collision"])
async def test_conflicting_hot_or_ledger_identity_fails_closed(reason):
    conn = FakeConnection()
    row = cold_row()
    if reason == "hot":
        conn.hot.add(row["tweet_id"])
    else:
        conn.ledger[row["tweet_id"]] = (
            row["tweet_id"], 99, "TELEGRAM", "-100123", "42", "0" * 64
        )
    if reason == "signal_id_collision":
        conn.ledger = {
            "different-source": (
                "different-source", 17, "X_TWITTER", "", "different-source", "0" * 64
            )
        }
    async with client_for([row]) as client:
        with pytest.raises(BackfillError, match="overlap|conflict"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/", apply=True
            )
    assert conn.writes == 0
    assert conn.stage == []
    assert not conn.locked


@pytest.mark.asyncio
async def test_late_malformed_row_does_not_apply_earlier_staged_rows():
    conn = FakeConnection()
    invalid = cold_row(signal_id=18)
    invalid["tweet_id"] = ""
    async with client_for([cold_row(), invalid]) as client:
        with pytest.raises(BackfillError, match="blank|disagree"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/",
                apply=True, batch_size=1,
            )
    assert conn.ledger == {}
    assert conn.stage == []
    assert not conn.locked


@pytest.mark.asyncio
async def test_ambiguous_duplicate_cold_source_fails_even_across_batches():
    conn = FakeConnection()
    first = cold_row()
    second = cold_row(signal_id=18)
    async with client_for([first, second]) as client:
        with pytest.raises(BackfillError, match="duplicate"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/",
                apply=True, batch_size=1,
            )
    assert conn.writes == 0
    assert conn.stage == []
    assert not conn.locked


@pytest.mark.asyncio
async def test_http_failure_cannot_write_tombstones():
    conn = FakeConnection()
    async with client_for([], status=503) as client:
        with pytest.raises(BackfillError, match="HTTP 503"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/", apply=True
            )
    assert conn.ledger == {}
    assert conn.stage == []
    assert not conn.locked


@pytest.mark.asyncio
async def test_complete_http_200_prefix_cannot_apply_partial_backfill():
    conn = FakeConnection()
    # The scan ends cleanly after one valid JSONEachRow line while a separate
    # FINAL count still sees two rows.
    async with client_for([cold_row()], reported_count=2) as client:
        with pytest.raises(BackfillError, match="cold scan was incomplete"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/", apply=True
            )
    assert conn.ledger == {}
    assert conn.stage == []
    assert not conn.locked


@pytest.mark.asyncio
async def test_backfill_refuses_sequence_that_can_reuse_a_cold_signal_id():
    conn = FakeConnection()
    conn.sequence_last_value = 17
    conn.sequence_is_called = False

    async with client_for([cold_row(signal_id=17)]) as client:
        with pytest.raises(BackfillError, match="sequence is behind"):
            await backfill_archived_identities(
                conn, client, clickhouse_url="http://clickhouse.test/", apply=True
            )

    assert conn.ledger == {}
    assert conn.stage == []
    assert not conn.locked
