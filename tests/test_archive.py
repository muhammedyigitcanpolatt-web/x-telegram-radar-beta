import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.cron.archive_to_cold import ColdDataLakeArchiver


class Record(dict):
    pass


def make_rows(count, start_id=1):
    detected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=45)
    return [
        Record(
            signal_id=start_id + index,
            tweet_id=str(start_id + index),
            username=f"fixture-{index}",
            raw_text="fixture event",
            text_hash=f"hash-{index}",
            threat_category="fixture category",
            location_context="fixture location",
            confidence_score=80,
            detected_at=detected,
            source_platform="X_TWITTER",
            source_channel_id="",
            source_message_id=str(start_id + index),
            llm_analysis={"fixture": True},
            visual_analysis={"fixture": "not-run"},
            ai_enriched=True,
            row_version="1",
        )
        for index in range(count)
    ]


class FakeConnection:
    def __init__(self, rows, delete_count=None, insert_on_delete=None, locked=True,
                 archived_sources=None, insert_count=None):
        self.rows = rows
        self.delete_count = delete_count
        self.insert_on_delete = insert_on_delete
        self.locked = locked
        self.selected = []
        self.cutoff_for_select = None
        self.cutoff_for_delete = None
        self.deleted_ids = []
        self.unlocked = False
        self.next_version = 2
        self.archived_sources = dict(archived_sources or {})
        self.insert_count = insert_count

    async def fetchval(self, query, _lock_id):
        assert "pg_try_advisory_lock" in query
        return self.locked

    async def fetch(self, query, cutoff, limit=None):
        if "nextval('public.cold_write_version_seq'::regclass)" in query:
            assert isinstance(cutoff, int)
            assert cutoff == len(self.selected)
            versions = range(self.next_version, self.next_version + cutoff)
            self.next_version += cutoff
            return [{"version": version} for version in versions]
        if "FROM archived_cti_source_identities" in query:
            signal_ids, tweet_ids = cutoff, limit
            return [
                identity for identity in self.archived_sources.values()
                if identity["signal_id"] in signal_ids
                or identity["tweet_id"] in tweet_ids
            ][:1]
        assert "xmin::text AS row_version" in query
        assert "ORDER BY detected_at, signal_id" in query
        assert "LIMIT $2" in query
        self.cutoff_for_select = cutoff
        eligible = [row for row in self.rows if row["detected_at"] < cutoff]
        eligible.sort(key=lambda row: (row["detected_at"], row["signal_id"]))
        # A SELECT result is a snapshot: mutating a hot row later must not
        # change the version that the archiver observed for its cold payload.
        self.selected = [Record(row.copy()) for row in eligible[:limit]]
        return list(self.selected)

    @asynccontextmanager
    async def transaction(self):
        snapshot = list(self.rows)
        identity_snapshot = dict(self.archived_sources)
        deleted_snapshot = list(self.deleted_ids)
        try:
            yield
        except Exception:
            self.rows[:] = snapshot
            self.archived_sources = identity_snapshot
            self.deleted_ids = deleted_snapshot
            raise

    async def execute(self, query, *args):
        if "pg_advisory_unlock" in query:
            self.unlocked = True
            return "SELECT 1"
        if "INSERT INTO archived_cti_source_identities" in query:
            assert "FROM unnest($1::text[], $2::integer[], $3::text[]" in query
            assert len(args) == 6
            assert len({len(values) for values in args}) == 1
            identities = [
                dict(zip(("tweet_id", "signal_id", "source_platform",
                          "source_channel_id", "source_message_id", "raw_text_sha256"), values))
                for values in zip(*args, strict=True)
            ]
            existing_ids = {identity["signal_id"] for identity in self.archived_sources.values()}
            new_tweet_ids = [identity["tweet_id"] for identity in identities]
            new_signal_ids = [identity["signal_id"] for identity in identities]
            if (
                len(set(new_tweet_ids)) != len(identities)
                or len(set(new_signal_ids)) != len(identities)
                or any(tweet_id in self.archived_sources for tweet_id in new_tweet_ids)
                or any(signal_id in existing_ids for signal_id in new_signal_ids)
            ):
                raise RuntimeError("duplicate key value violates unique constraint")
            self.archived_sources.update(
                (identity["tweet_id"], identity) for identity in identities
            )
            return f"INSERT 0 {len(identities) if self.insert_count is None else self.insert_count}"
        assert "DELETE FROM cartel_threat_signals AS hot" in query
        assert "unnest($1::integer[], $2::text[])" in query
        assert "hot.xmin::text = copied.row_version" in query
        assert "hot.detected_at < $3" in query
        ids, versions, cutoff = args
        selected_versions = dict(zip(ids, versions, strict=True))
        self.cutoff_for_delete = cutoff
        if self.insert_on_delete:
            self.rows.extend(self.insert_on_delete)
            self.insert_on_delete = None
        self.deleted_ids = [
            row["signal_id"] for row in self.rows
            if selected_versions.get(row["signal_id"]) == row["row_version"]
            and row["detected_at"] < cutoff
        ]
        self.rows[:] = [
            row for row in self.rows
            if row["signal_id"] not in self.deleted_ids
        ]
        deleted = len(self.deleted_ids)
        if self.delete_count is not None:
            deleted = self.delete_count
        return f"DELETE {deleted}"


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class FakeDatabase:
    def __init__(self, rows, **connection_options):
        self.connection = FakeConnection(rows, **connection_options)
        self.pool = FakePool(self.connection)


class HttpResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def http_client_factory(statuses, cold_rows):
    def factory(*, timeout):
        def handler(request):
            assert request.method == "POST"
            assert "historical_threat_signals FORMAT JSONEachRow" in request.url.params["query"]
            assert request.url.params["async_insert"] == "0"
            rows = [json.loads(line) for line in request.content.decode().splitlines() if line]
            status = statuses.pop(0)
            # A failed HTTP request may have delivered an initial block. Replays
            # use the same ReplacingMergeTree key, so the fake materialized view
            # retains one record per stable ID.
            for row in (rows if status == 200 else rows[:1]):
                cold_rows[row["id"]] = row
            return httpx.Response(status, text="fixture response")
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))
    return factory


@pytest.mark.asyncio
async def test_50001_rows_delete_only_the_50000_transferred_ids_and_keep_concurrent_insert():
    rows = make_rows(50_001)
    concurrent = make_rows(1, start_id=60_000)
    database = FakeDatabase(rows, insert_on_delete=concurrent)
    cold_rows = {}
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([200], cold_rows),
        clickhouse_url="http://clickhouse.test/",
    )

    await archiver.archive_migration_cycle()

    connection = database.connection
    selected_ids = {row["signal_id"] for row in connection.selected}
    assert len(selected_ids) == 50_000
    assert set(cold_rows) == {str(signal_id) for signal_id in selected_ids}
    assert set(connection.deleted_ids) == selected_ids
    assert set(connection.archived_sources) == {str(signal_id) for signal_id in selected_ids}
    assert connection.cutoff_for_select == connection.cutoff_for_delete
    assert len(database.connection.rows) == 2
    assert {row["signal_id"] for row in database.connection.rows} == {50_001, 60_000}
    assert connection.unlocked


@pytest.mark.asyncio
async def test_failed_http_transfer_does_not_delete_any_postgres_rows():
    rows = make_rows(3)
    database = FakeDatabase(rows)
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([503], {}),
    )

    with pytest.raises(RuntimeError, match="ClickHouse archive transfer failed"):
        await archiver.archive_migration_cycle()

    assert [row["signal_id"] for row in database.connection.rows] == [1, 2, 3]
    assert database.connection.deleted_ids == []
    assert database.connection.archived_sources == {}
    assert database.connection.unlocked


@pytest.mark.asyncio
async def test_partial_failure_retry_is_idempotent_and_deletes_only_after_confirmed_retry():
    rows = make_rows(2)
    database = FakeDatabase(rows)
    cold_rows = {}
    factory = http_client_factory([500, 200], cold_rows)
    archiver = ColdDataLakeArchiver(database=database, http_client_factory=factory)

    with pytest.raises(RuntimeError):
        await archiver.archive_migration_cycle()
    assert len(database.connection.rows) == 2
    assert database.connection.archived_sources == {}
    await archiver.archive_migration_cycle()

    assert len(cold_rows) == 2
    assert [row["signal_id"] for row in database.connection.rows] == []
    assert set(database.connection.archived_sources) == {"1", "2"}


@pytest.mark.asyncio
async def test_concurrent_enrichment_keeps_hot_row_until_updated_version_is_archived():
    rows = make_rows(1)
    rows[0]["ai_enriched"] = False
    rows[0]["llm_analysis"] = {}
    database = FakeDatabase(rows)
    cold_rows = {}
    transfers = 0

    def factory(*, timeout):
        def handler(request):
            nonlocal transfers
            transfers += 1
            row = json.loads(request.content.decode().strip())
            cold_rows[row["id"]] = row
            if transfers == 1:
                # A separate worker commits an UPDATE after the cold payload is
                # formed but before the archiver's compare-and-delete.
                database.connection.rows[0].update(
                    row_version="2",
                    ai_enriched=True,
                    llm_analysis={"category": "confirmed"},
                )
            return httpx.Response(200)

        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    archiver = ColdDataLakeArchiver(database=database, http_client_factory=factory)
    with pytest.raises(RuntimeError, match="Archive deletion count mismatch"):
        await archiver.archive_migration_cycle()

    assert len(database.connection.rows) == 1
    assert database.connection.archived_sources == {}
    assert database.connection.rows[0]["ai_enriched"] is True
    assert json.loads(cold_rows["1"]["payload_json"])["ai_enriched"] is False
    first_version = cold_rows["1"]["version"]

    await archiver.archive_migration_cycle()

    assert transfers == 2
    assert database.connection.rows == []
    archived = json.loads(cold_rows["1"]["payload_json"])
    assert cold_rows["1"]["version"] > first_version
    assert archived["ai_enriched"] is True
    assert archived["llm_analysis"] == {"category": "confirmed"}
    assert database.connection.archived_sources["1"]["raw_text_sha256"] == hashlib.sha256(
        b"fixture event"
    ).hexdigest()


@pytest.mark.asyncio
async def test_delete_count_mismatch_rolls_back_selected_batch():
    rows = make_rows(4)
    database = FakeDatabase(rows, delete_count=3)
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([200], {}),
    )

    with pytest.raises(RuntimeError, match="Archive deletion count mismatch"):
        await archiver.archive_migration_cycle()
    assert [row["signal_id"] for row in database.connection.rows] == [1, 2, 3, 4]
    assert database.connection.archived_sources == {}


@pytest.mark.asyncio
async def test_archive_records_source_identity_with_exact_deleted_hot_rows():
    row = make_rows(1)[0]
    row.update(
        tweet_id="telegram:-100123:42",
        source_platform="TELEGRAM",
        source_channel_id="-100123",
        source_message_id="42",
        raw_text="sinyal ü",
    )
    database = FakeDatabase([row])
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([200], {}),
    )

    await archiver.archive_migration_cycle()

    assert database.connection.rows == []
    assert database.connection.archived_sources == {
        "telegram:-100123:42": {
            "tweet_id": "telegram:-100123:42",
            "signal_id": 1,
            "source_platform": "TELEGRAM",
            "source_channel_id": "-100123",
            "source_message_id": "42",
            "raw_text_sha256": hashlib.sha256("sinyal ü".encode()).hexdigest(),
        },
    }


def test_archived_identity_can_hash_an_empty_but_known_text():
    row = make_rows(1)[0]
    row["raw_text"] = ""
    assert ColdDataLakeArchiver._archived_identity(row)["raw_text_sha256"] == (
        hashlib.sha256(b"").hexdigest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["tweet_id", "signal_id"])
async def test_archive_identity_conflict_rolls_back_hot_delete(conflict):
    row = make_rows(1)[0]
    prior = ColdDataLakeArchiver._archived_identity(row)
    if conflict == "tweet_id":
        prior["signal_id"] = 777
        prior_key = row["tweet_id"]
    else:
        prior["tweet_id"] = "previous"
        prior_key = "previous"
    database = FakeDatabase([row], archived_sources={prior_key: prior})
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([200], {}),
    )

    with pytest.raises(RuntimeError, match="already exists in the cold identity ledger"):
        await archiver.archive_migration_cycle()

    assert database.connection.rows == [row]
    assert database.connection.archived_sources == {prior_key: prior}
    assert database.connection.deleted_ids == []
    assert database.connection.unlocked


@pytest.mark.asyncio
async def test_archive_identity_insert_count_mismatch_rolls_back_hot_delete():
    row = make_rows(1)[0]
    database = FakeDatabase([row], insert_count=0)
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=http_client_factory([200], {}),
    )

    with pytest.raises(RuntimeError, match="source identity count mismatch"):
        await archiver.archive_migration_cycle()

    assert database.connection.rows == [row]
    assert database.connection.archived_sources == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tweet_id", ""),
    ("source_platform", ""),
    ("source_message_id", "wrong"),
    ("source_message_id", ""),
    ("raw_text", None),
])
async def test_archive_rejects_incomplete_or_ambiguous_identity_before_cold_write(field, value):
    row = make_rows(1)[0]
    row[field] = value
    database = FakeDatabase([row])
    archiver = ColdDataLakeArchiver(
        database=database,
        http_client_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("cold write must not begin")
        ),
    )

    with pytest.raises(RuntimeError, match="Archive source identity"):
        await archiver.archive_migration_cycle()

    assert database.connection.rows == [row]
    assert database.connection.archived_sources == {}
    assert database.connection.unlocked


@pytest.mark.asyncio
async def test_second_archiver_exits_when_advisory_lock_is_held():
    database = FakeDatabase(make_rows(1), locked=False)
    archiver = ColdDataLakeArchiver(database=database)

    await archiver.archive_migration_cycle()

    assert database.connection.selected == []
    assert database.connection.deleted_ids == []


def test_archived_payload_is_lossless_for_long_text_analysis_and_source_identity():
    row = make_rows(1)[0]
    row["raw_text"] = "x" * 5001
    row["source_platform"] = "TELEGRAM"
    row["source_channel_id"] = "-100200300"
    row["source_message_id"] = "77"
    row["llm_analysis"] = {"category": "fixture", "score": 91}
    row["visual_analysis"] = {"objects": ["fixture"]}

    archived = ColdDataLakeArchiver._to_cold_row(row)
    payload = json.loads(archived["payload_json"])

    assert archived["version"] == 1
    assert len(archived["raw_text"]) == 5001
    assert payload["raw_text"] == row["raw_text"]
    assert payload["llm_analysis"] == row["llm_analysis"]
    assert payload["visual_analysis"] == row["visual_analysis"]
    assert payload["source_channel_id"] == "-100200300"
    assert payload["source_message_id"] == "77"


def test_archive_restores_asyncpg_jsonb_text_to_json_values():
    row = make_rows(1)[0]
    row["llm_analysis"] = '{"category":"confirmed","score":91}'
    row["visual_analysis"] = '[{"object":"fixture"}]'

    archived = ColdDataLakeArchiver._to_cold_row(row)
    payload = json.loads(archived["payload_json"])

    assert payload["llm_analysis"] == {"category": "confirmed", "score": 91}
    assert payload["visual_analysis"] == [{"object": "fixture"}]


def test_archive_rejects_malformed_asyncpg_jsonb_text():
    row = make_rows(1)[0]
    row["llm_analysis"] = "{malformed"
    with pytest.raises(ValueError):
        ColdDataLakeArchiver._to_cold_row(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted_uint64", [False, True])
async def test_cold_enrichment_appends_versioned_row_and_verifies_final_result(quoted_uint64):
    original = ColdDataLakeArchiver._to_cold_row(make_rows(1)[0])
    original["version"] = 1
    state = {"row": original, "calls": []}

    def factory(*, timeout):
        assert timeout == 30.0

        def handler(request):
            query = request.url.params["query"]
            if request.method == "GET" and "WHERE tweet_id" in query:
                assert request.url.params["param_tweet_id"] == original["tweet_id"]
                assert " FINAL " in query and "LIMIT 2" in query
                state["calls"].append("lookup")
                row = dict(state["row"])
                if quoted_uint64:
                    row["version"] = str(row["version"])
                return httpx.Response(200, text=json.dumps(row) + "\n")
            if request.method == "POST":
                assert request.url.params["async_insert"] == "0"
                assert "FORMAT JSONEachRow" in query
                state["calls"].append("insert")
                state["row"] = json.loads(request.content.decode().strip())
                return httpx.Response(200)
            assert request.method == "GET" and "WHERE id" in query
            assert request.url.params["param_signal_id"] == original["id"]
            state["calls"].append("verify")
            row = dict(state["row"])
            if quoted_uint64:
                row["version"] = str(row["version"])
            return httpx.Response(200, text=json.dumps(row) + "\n")

        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    archiver = ColdDataLakeArchiver(http_client_factory=factory)
    signal_id = await archiver.enrich_archived_signal(
        original["tweet_id"], original["raw_text"],
        {"category": "confirmed"}, {"objects": []}, version=101,
    )

    assert signal_id == original["id"]
    assert state["calls"] == ["lookup", "insert", "verify"]
    assert state["row"]["version"] == 101
    assert all(
        state["row"][column] == value
        for column, value in original.items() if column not in {"payload_json", "version"}
    )
    payload = json.loads(state["row"]["payload_json"])
    assert payload["llm_analysis"] == {"category": "confirmed"}
    assert payload["visual_analysis"] == {"objects": []}
    assert payload["ai_enriched"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected_error",
    [
        ("missing", "exactly one signal"),
        ("ambiguous", "exactly one signal"),
        ("corrupt_payload", "payload is malformed"),
        ("identity_mismatch", "identity or text does not match"),
        ("text_mismatch", "identity or text does not match"),
        ("lookup_http_error", "lookup failed"),
        ("insert_http_error", "insert failed"),
        ("verification_stale", "did not match inserted data"),
        ("verification_http_error", "verification failed"),
        ("version_missing", "identity or text does not match"),
        ("stale_version", "version must advance"),
    ],
)
async def test_cold_enrichment_fails_closed_on_unsafe_or_unverified_row(case, expected_error):
    original = ColdDataLakeArchiver._to_cold_row(make_rows(1)[0])
    original["version"] = 1
    state = {"inserted": False, "row": original}

    def factory(*, timeout):
        def handler(request):
            query = request.url.params["query"]
            if request.method == "GET" and "WHERE tweet_id" in query:
                if case == "lookup_http_error":
                    return httpx.Response(503)
                if case == "missing":
                    return httpx.Response(200, text="")
                row = dict(original)
                if case == "corrupt_payload":
                    row["payload_json"] = "{broken"
                elif case == "identity_mismatch":
                    row["tweet_id"] = "other-source"
                elif case == "text_mismatch":
                    row["raw_text"] = "other text"
                elif case == "version_missing":
                    row.pop("version")
                rows = [row, dict(row, id="2")] if case == "ambiguous" else [row]
                return httpx.Response(200, text="".join(json.dumps(x) + "\n" for x in rows))
            if request.method == "POST":
                if case == "insert_http_error":
                    return httpx.Response(503)
                state["inserted"] = True
                state["row"] = json.loads(request.content.decode().strip())
                return httpx.Response(200)
            if case == "verification_http_error":
                return httpx.Response(503)
            row = original if case == "verification_stale" else state["row"]
            return httpx.Response(200, text=json.dumps(row) + "\n")

        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    archiver = ColdDataLakeArchiver(http_client_factory=factory)
    with pytest.raises(RuntimeError, match=expected_error):
        await archiver.enrich_archived_signal(
            original["tweet_id"], original["raw_text"], {"category": "fixture"}, {},
            version=1 if case == "stale_version" else 101,
        )
    assert state["inserted"] is (case in {"verification_stale", "verification_http_error"})
