import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace

import app.workers.analyzer as analyzer_module
import app.workers.celery_app as celery_module
import app.database as database_module
from app.database import RadarRepository
from app.workers.analyzer import (
    RedisEventPublisher,
    TweetAnalyzerWorker,
    account_age_days,
    anomaly_strength,
    process_cartel_pipeline,
)
from app.intel.scoring import EnterpriseScoringEngine


class FakeRedisQueue:
    def __init__(self, initial=None):
        self.queue = list(initial or [])
        self.processing = []
        self.dead = []
        self.dead_ttl = None
        self.values = {}
        self.events = []
        self.wait_forever = asyncio.Event()

    async def lrange(self, key, start, end):
        if key == "queue:raw_tweets:processing":
            return list(self.processing)
        return []

    async def brpoplpush(self, source, destination, timeout=0):
        if self.queue:
            value = self.queue.pop()
            self.processing.insert(0, value)
            self.events.append("claimed")
            return value
        await self.wait_forever.wait()
        return None

    async def lrem(self, key, count, value):
        self.events.append("lrem")
        rows = self.processing if key == "queue:raw_tweets:processing" else self.queue
        removed = 0
        for _ in range(min(count, rows.count(value)) if count > 0 else rows.count(value)):
            rows.remove(value)
            removed += 1
        return removed

    async def eval(self, _script, _key_count, *args):
        if _script == analyzer_module.BOUND_LEGACY_DEAD_QUEUE_SCRIPT:
            assert _key_count == 1
            key, max_dead_records, dead_ttl = args
            assert key == analyzer_module.DEAD_QUEUE
            size = len(self.dead)
            del self.dead[max_dead_records:]
            if size and self.dead_ttl is None:
                self.dead_ttl = dead_ttl
            return size
        if _script == analyzer_module.COUNT_SOURCE_ACTIVITY_SCRIPT:
            source_key, text_key, activity_key = args
            cached = self.values.get(source_key)
            if cached is not None:
                return [cached["repetition"], cached["activity"]]
            repetition = await self.incr(text_key)
            activity = await self.incr(activity_key)
            self.values[source_key] = {"repetition": repetition, "activity": activity}
            return [repetition, activity]
        source, destination, value, operation, queue_kind, max_dead_records, dead_ttl = args
        removed = await self.lrem(source, 1, value)
        if not removed:
            return 0
        if destination == "queue:raw_tweets:dead":
            assert queue_kind == "dead"
            self.dead.insert(0, value)
            moved_size = len(self.dead)
            del self.dead[max_dead_records:]
            self.dead_ttl = dead_ttl
        elif operation == "RPUSH":
            assert queue_kind == "retry"
            self.queue.append(value)
            moved_size = len(self.queue)
        else:
            assert queue_kind == "retry"
            self.queue.insert(0, value)
            moved_size = len(self.queue)
        self.events.append("atomic_move")
        return moved_size

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, *_args):
        return True

    async def delete(self, key):
        self.values.pop(key, None)

    async def publish(self, channel, payload):
        self.events.append((channel, payload))
        return 1


class FakePool:
    def __init__(self):
        self.statements = []
        self.archived_identity = None
        self.hot_identity = None
        self.raw_identity = None

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, *args):
        self.statements.append((sql, args))

    async def fetch(self, sql, *_args):
        if "FROM cartel_embeddings" in sql:
            return [{"similarity": 0.91}, {"similarity": 0.89}]
        return []

    async def fetchrow(self, sql, *_args):
        if "FROM archived_cti_source_identities" in sql:
            return self.archived_identity
        if "FROM cartel_threat_signals" in sql:
            return self.hot_identity
        if "FROM raw_tweets" in sql:
            return self.raw_identity
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_analyzer_acknowledges_only_after_processing_succeeds():
    raw = json.dumps({"tweet_id": "123", "text": "fixture", "user_id": "u1"})
    redis = FakeRedisQueue([raw])
    pool = FakePool()
    worker = TweetAnalyzerWorker(redis, pool, event_publisher=object())
    processed = asyncio.Event()

    async def process_tweet(_tweet, raw_str):
        assert raw_str == raw
        redis.events.append("processed")
        processed.set()

    worker.process_tweet = process_tweet
    task = asyncio.create_task(worker.start_loop())
    await asyncio.wait_for(processed.wait(), timeout=1)
    for _ in range(50):
        if "lrem" in redis.events:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert redis.processing == []
    assert redis.events.index("processed") < redis.events.index("lrem")


@pytest.mark.asyncio
async def test_dead_letter_move_bounds_malformed_record_retention():
    redis = FakeRedisQueue()
    redis.dead = [f"old-{index}" for index in range(analyzer_module.DEAD_QUEUE_MAX_RECORDS)]
    worker = TweetAnalyzerWorker(redis, FakePool(), event_publisher=object())
    malformed_record = "{" + "x" * (2 * 1024 * 1024 - 1)
    redis.processing.append(malformed_record)

    moved = await worker._move_processing_record(
        analyzer_module.DEAD_QUEUE, malformed_record, "LPUSH"
    )

    assert moved == analyzer_module.DEAD_QUEUE_MAX_RECORDS + 1
    assert redis.processing == []
    assert redis.dead[0] == malformed_record
    assert len(redis.dead) == analyzer_module.DEAD_QUEUE_MAX_RECORDS
    assert "old-99" not in redis.dead
    assert redis.dead_ttl == analyzer_module.DEAD_QUEUE_TTL_SECONDS


@pytest.mark.asyncio
async def test_failed_dead_letter_move_keeps_existing_record():
    redis = FakeRedisQueue()
    worker = TweetAnalyzerWorker(redis, FakePool(), event_publisher=object())

    moved = await worker._move_processing_record(
        analyzer_module.DEAD_QUEUE, "missing", "LPUSH"
    )

    assert moved == 0
    assert redis.dead == []
    assert redis.dead_ttl is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_ttl", [None, 123])
async def test_worker_startup_bounds_legacy_dead_letter_queue_without_resetting_ttl(existing_ttl):
    redis = FakeRedisQueue()
    redis.dead = [f"old-{index}" for index in range(125)]
    redis.dead_ttl = existing_ttl
    worker = TweetAnalyzerWorker(redis, FakePool(), event_publisher=object())

    await worker._recover_unacknowledged_messages()

    assert redis.dead == [f"old-{index}" for index in range(100)]
    assert redis.dead_ttl == (
        analyzer_module.DEAD_QUEUE_TTL_SECONDS if existing_ttl is None else existing_ttl
    )


@pytest.mark.asyncio
async def test_analyzer_event_publisher_emits_versioned_source_identity():
    redis = FakeRedisQueue()
    publisher = RedisEventPublisher(redis)
    await publisher.broadcast_alert({
        "source_platform": "TELEGRAM",
        "source_channel_id": "-100123",
        "source_message_id": "42",
        "tweet_id": "telegram:-100123:42",
        "username": "tg_fixture",
        "text": "fixture text",
        "reason": "fixture match",
        "score": 81,
    })

    channel, serialized = redis.events[0]
    event = json.loads(serialized)
    assert channel == "radar:events:v1"
    assert event["schema_version"] == 1
    assert event["event"] == "ANOMALY_DETECTED"
    assert event["source_platform"] == "TELEGRAM"
    assert event["source_channel_id"] == "-100123"
    assert event["source_message_id"] == "42"
    assert event["tweet_id"] == "telegram:-100123:42"
    assert event["score"] == 81
    assert event["score_kind"] == "confidence"


@pytest.mark.asyncio
async def test_analyzer_event_id_uses_stable_signal_kind_across_retries():
    redis = FakeRedisQueue()
    publisher = RedisEventPublisher(redis)
    base = {
        "tweet_id": "fixture-1", "source_platform": "X_TWITTER",
        "source_message_id": "fixture-1", "event_key": "repeated_text",
        "score_kind": "anomaly_strength", "score": 84,
    }
    await publisher.broadcast_alert({**base, "reason": "Repeated 6 times"})
    await publisher.broadcast_alert({**base, "reason": "Repeated 7 times"})
    first, second = (json.loads(payload) for _, payload in redis.events)
    assert first["event_id"] == second["event_id"]
    assert first["reason"] != second["reason"]


def test_anomaly_strength_is_bounded_and_increases_with_evidence():
    repetition_scores = [anomaly_strength(count, 0) for count in (6, 7, 10, 20)]
    activity_scores = [anomaly_strength(1, z_score) for z_score in (3.6, 4.1, 6, 12)]
    assert repetition_scores == sorted(repetition_scores)
    assert activity_scores == sorted(activity_scores)
    assert all(80 <= score <= 100 for score in repetition_scores + activity_scores)
    assert repetition_scores[-1] == activity_scores[-1] == 100


@pytest.mark.asyncio
async def test_anomaly_publish_waits_for_signal_write_and_retry_keeps_counters(monkeypatch):
    timeline = []

    class FlakyPool(FakePool):
        fail_signal = True

        async def execute(self, sql, *args):
            if "INSERT INTO coordinated_signals" in sql and self.fail_signal:
                self.fail_signal = False
                timeline.append("signal_failed")
                raise RuntimeError("fixture write failed")
            if "INSERT INTO raw_tweets" in sql:
                timeline.append("raw")
            elif "INSERT INTO coordinated_signals" in sql:
                timeline.append("signal")
            return await super().execute(sql, *args)

    class TrackingRedis(FakeRedisQueue):
        async def publish(self, channel, payload):
            timeline.append("publish")
            return await super().publish(channel, payload)

    class NoEmbedding:
        async def get_embedding(self, _text):
            return None

    async def skip_pipeline(*_args):
        return None

    monkeypatch.setattr(analyzer_module, "process_cartel_pipeline", skip_pipeline)
    redis = TrackingRedis()
    pool = FlakyPool()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    worker.semantic = NoEmbedding()
    record = {"tweet_id": "fixture-1", "user_id": "u1", "text": "repeated fixture"}
    raw = json.dumps(record)
    text_key = f"hash:{hashlib.sha256(record['text'].encode()).hexdigest()}"
    redis.values[text_key] = 5

    with pytest.raises(RuntimeError, match="fixture write failed"):
        await worker.process_tweet(record, raw)
    assert timeline == ["raw", "signal_failed"]
    assert redis.events == []
    assert redis.values[text_key] == 6
    assert redis.values["user_activity:u1"] == 1

    await worker.process_tweet(record, raw)
    assert timeline == ["raw", "signal_failed", "raw", "signal", "publish"]
    assert redis.values[text_key] == 6
    assert redis.values["user_activity:u1"] == 1
    event = json.loads(redis.events[0][1])
    assert event["score_kind"] == "anomaly_strength"
    assert event["score"] == anomaly_strength(6, -2.0235)
    assert "6 times" in event["reason"]


@pytest.mark.asyncio
async def test_raw_write_failure_does_not_count_or_publish_anomaly():
    class BrokenPool(FakePool):
        async def execute(self, sql, *args):
            if "INSERT INTO raw_tweets" in sql:
                raise RuntimeError("raw storage unavailable")
            return await super().execute(sql, *args)

    redis = FakeRedisQueue()
    worker = TweetAnalyzerWorker(redis, BrokenPool(), RedisEventPublisher(redis))
    record = {"tweet_id": "fixture-raw", "user_id": "u1", "text": "fixture"}
    with pytest.raises(RuntimeError, match="raw storage unavailable"):
        await worker.process_tweet(record, json.dumps(record))
    assert redis.values == {}
    assert redis.events == []


@pytest.mark.asyncio
async def test_semantic_alert_waits_for_signal_write():
    class BrokenPool(FakePool):
        async def execute(self, sql, *args):
            if "INSERT INTO coordinated_signals" in sql:
                raise RuntimeError("semantic signal unavailable")
            return await super().execute(sql, *args)

    class SemanticMatch:
        async def get_embedding(self, _text):
            return [0.1, 0.2]

        async def find_semantic_coordination(self, _vector, threshold):
            assert threshold == 0.88
            return ["one", "two", "three"]

    redis = FakeRedisQueue()
    worker = TweetAnalyzerWorker(redis, BrokenPool(), RedisEventPublisher(redis))
    worker.semantic = SemanticMatch()
    record = {"tweet_id": "fixture-semantic", "user_id": "u1", "text": "ordinary fixture"}
    with pytest.raises(RuntimeError, match="semantic signal unavailable"):
        await worker.process_tweet(record, json.dumps(record))
    assert redis.events == []


@pytest.mark.asyncio
async def test_heuristic_and_semantic_signals_keep_distinct_durable_rows_on_retry(monkeypatch):
    class UniqueSignalsPool(FakePool):
        def __init__(self):
            super().__init__()
            # Migration 004 leaves historical rows untyped and in place.
            self.signal_rows = {("fixture-both", None): ("legacy",)}

        async def execute(self, sql, *args):
            if "INSERT INTO coordinated_signals" in sql:
                assert "ON CONFLICT (tweet_id, signal_kind)" in sql
                assert "WHERE signal_kind IS NOT NULL DO NOTHING" in sql
                key = (args[1], args[2])
                self.signal_rows.setdefault(key, args)
            return await super().execute(sql, *args)

    class SemanticMatch:
        async def get_embedding(self, _text):
            return [0.1, 0.2]

        async def find_semantic_coordination(self, _vector, threshold):
            assert threshold == 0.88
            return ["one", "two", "three"]

    async def skip_pipeline(*_args):
        return None

    monkeypatch.setattr(analyzer_module, "process_cartel_pipeline", skip_pipeline)
    redis = FakeRedisQueue()
    pool = UniqueSignalsPool()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    worker.semantic = SemanticMatch()
    record = {"tweet_id": "fixture-both", "user_id": "u1", "text": "repeated fixture"}
    text_key = f"hash:{hashlib.sha256(record['text'].encode()).hexdigest()}"
    redis.values[text_key] = 5

    await worker.process_tweet(record, json.dumps(record))
    await worker.process_tweet(record, json.dumps(record))

    assert set(pool.signal_rows) == {
        ("fixture-both", None),
        ("fixture-both", "HEURISTIC"),
        ("fixture-both", "SEMANTIC"),
    }
    assert redis.values[text_key] == 6
    events = [json.loads(payload) for _, payload in redis.events]
    assert len(events) == 4
    assert len({event["event_id"] for event in events}) == 2


def test_account_age_requires_separate_timezone_aware_account_timestamp():
    assert account_age_days(None) is None
    assert account_age_days("2000-01-01T00:00:00") is None
    assert account_age_days("not a date") is None
    assert account_age_days("2999-01-01T00:00:00Z") is None
    account_created = datetime.now(timezone.utc) - timedelta(days=400)
    iso_age = account_age_days(account_created.isoformat())
    legacy_age = account_age_days(account_created.strftime("%a %b %d %H:%M:%S %z %Y"))
    assert iso_age in (399, 400)
    assert legacy_age in (399, 400)

    scoring = EnterpriseScoringEngine()
    unknown_code, _ = scoring.calculate_admiralty_code(
        vector_score=0.95, has_crypto=False, visual_risk=0, account_age_days=None,
    )
    known_code, _ = scoring.calculate_admiralty_code(
        vector_score=0.95, has_crypto=False, visual_risk=0, account_age_days=400,
    )
    assert unknown_code.startswith("F")
    assert known_code.startswith("A")


@pytest.mark.asyncio
async def test_ingest_processor_routes_cti_candidate_and_preserves_identity(monkeypatch):
    redis = FakeRedisQueue()
    pool = FakePool()
    worker = TweetAnalyzerWorker(redis, pool, event_publisher=object())
    routed = []

    async def fake_pipeline(record, pg_pool, publisher):
        routed.append((record, pg_pool, publisher))

    class NoEmbedding:
        async def get_embedding(self, _text):
            return None

    monkeypatch.setattr(analyzer_module, "process_cartel_pipeline", fake_pipeline)
    worker.semantic = NoEmbedding()
    record = {
        "source_platform": "TELEGRAM",
        "source_channel_id": "-100123",
        "source_message_id": "42",
        "tweet_id": "42",
        "user_id": "-100123",
        "username": "tg_fixture",
        "text": "Sinaloa cartel fixture",
        "created_at": "2026-10-03T10:00:00",
    }
    raw = json.dumps(record)
    await worker.process_tweet(record, raw)

    assert len(routed) == 1
    assert routed[0][0]["tweet_id"] == "telegram:-100123:42"
    assert routed[0][1] is pool
    raw_insert = next(item for item in pool.statements if "INSERT INTO raw_tweets" in item[0])
    assert raw_insert[1][:4] == ("telegram:-100123:42", "TELEGRAM", "-100123", "42")
    assert raw_insert[1][6] == datetime(2026, 10, 3, 10, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_timestamp", "expected"),
    [
        ("2026-10-03T10:00:00.000Z", datetime(2026, 10, 3, 10, 0)),
        ("2026-10-03T12:30:00+02:30", datetime(2026, 10, 3, 10, 0)),
        ("2026-10-03T10:00:00", datetime(2026, 10, 3, 10, 0)),
        ("Sat Oct 03 10:00:00 +0000 2026", datetime(2026, 10, 3, 10, 0)),
    ],
)
async def test_raw_timestamp_is_bound_as_utc_naive_datetime(source_timestamp, expected):
    class TimestampPool(FakePool):
        async def execute(self, sql, *args):
            if "INSERT INTO raw_tweets" in sql:
                # asyncpg infers TIMESTAMP from the cast and rejects a string.
                assert isinstance(args[6], datetime)
                assert args[6].tzinfo is None
            return await super().execute(sql, *args)

    class NoEmbedding:
        async def get_embedding(self, _text):
            return None

    redis = FakeRedisQueue()
    pool = TimestampPool()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    worker.semantic = NoEmbedding()
    record = {
        "tweet_id": "fixture-date", "user_id": "u1", "text": "ordinary fixture",
        "created_at": source_timestamp,
    }
    await worker.process_tweet(record, json.dumps(record))

    raw_insert = next(item for item in pool.statements if "INSERT INTO raw_tweets" in item[0])
    assert raw_insert[1][6] == expected


@pytest.mark.asyncio
async def test_missing_raw_timestamp_uses_current_utc_time():
    class NoEmbedding:
        async def get_embedding(self, _text):
            return None

    redis = FakeRedisQueue()
    pool = FakePool()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    worker.semantic = NoEmbedding()
    record = {"tweet_id": "fixture-no-date", "user_id": "u1", "text": "ordinary fixture"}
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await worker.process_tweet(record, json.dumps(record))
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    raw_insert = next(item for item in pool.statements if "INSERT INTO raw_tweets" in item[0])
    assert before <= raw_insert[1][6] <= after


@pytest.mark.asyncio
async def test_invalid_raw_timestamp_fails_before_write_or_activity_count():
    redis = FakeRedisQueue()
    pool = FakePool()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    record = {
        "tweet_id": "fixture-bad-date", "user_id": "u1", "text": "ordinary fixture",
        "created_at": "not a date",
    }
    with pytest.raises(ValueError, match="Invalid source created_at timestamp"):
        await worker.process_tweet(record, json.dumps(record))
    assert not any("INSERT INTO raw_tweets" in sql for sql, _ in pool.statements)
    assert redis.values == {}


def archived_identity_for(record):
    return {
        "tweet_id": record["tweet_id"],
        "source_platform": record["source_platform"],
        "source_channel_id": record["source_channel_id"],
        "source_message_id": record["source_message_id"],
        "raw_text_sha256": hashlib.sha256(record["text"].encode("utf-8")).hexdigest(),
    }


def hot_identity_for(record):
    return {
        "tweet_id": record["tweet_id"],
        "source_platform": record["source_platform"],
        "source_channel_id": record["source_channel_id"],
        "source_message_id": record["source_message_id"],
        "raw_text": record["text"],
    }


def raw_identity_for(record):
    return {
        "tweet_id": record["tweet_id"],
        "source_platform": record["source_platform"],
        "source_channel_id": record["source_channel_id"],
        "source_message_id": record["source_message_id"],
        "raw_json": json.dumps(record),
    }


@pytest.mark.asyncio
async def test_exact_archived_replay_is_acknowledged_before_raw_or_activity_side_effects():
    redis = FakeRedisQueue()
    pool = FakePool()
    record = {
        "tweet_id": "fixture-archived", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-archived",
        "user_id": "u1", "text": "cartel fixture",
    }
    pool.archived_identity = archived_identity_for(record)
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))

    await worker.process_tweet(record, json.dumps(record))

    assert not any("INSERT INTO raw_tweets" in sql for sql, _ in pool.statements)
    assert redis.values == {}
    assert redis.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["text", "source_channel_id"])
async def test_archived_source_id_conflict_fails_before_side_effects(changed_field):
    redis = FakeRedisQueue()
    pool = FakePool()
    record = {
        "tweet_id": "fixture-archived", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-archived",
        "user_id": "u1", "text": "cartel fixture",
    }
    pool.archived_identity = archived_identity_for(record)
    record[changed_field] = "different"
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))

    with pytest.raises(RuntimeError, match="Archived CTI source identity conflicts"):
        await worker.process_tweet(record, json.dumps(record))

    assert not any("INSERT INTO raw_tweets" in sql for sql, _ in pool.statements)
    assert redis.values == {}
    assert redis.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source_tier", ["hot", "raw"])
async def test_conflicting_replay_is_rejected_before_archival_even_without_cti_trigger(source_tier):
    redis = FakeRedisQueue()
    pool = FakePool()
    original = {
        "tweet_id": "fixture-reused", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-reused",
        "user_id": "u1", "text": "cartel source",
    }
    if source_tier == "hot":
        pool.hot_identity = hot_identity_for(original)
    else:
        pool.raw_identity = raw_identity_for(original)
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    replay = {**original, "text": "ordinary replacement"}

    with pytest.raises(RuntimeError, match=f"{source_tier.capitalize()} .* conflicts"):
        await worker.process_tweet(replay, json.dumps(replay))

    assert not any("INSERT INTO raw_tweets" in sql for sql, _ in pool.statements)
    assert redis.values == {}
    assert redis.events == []


@pytest.mark.asyncio
async def test_archived_source_conflict_with_empty_text_is_not_acknowledged():
    pool = FakePool()
    original = {
        "tweet_id": "fixture-empty", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-empty",
        "user_id": "u1", "text": "cartel source",
    }
    pool.archived_identity = archived_identity_for(original)
    redis = FakeRedisQueue()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))

    with pytest.raises(RuntimeError, match="Archived CTI source identity conflicts"):
        await worker.process_tweet({**original, "text": ""}, "{}")
    assert redis.values == {}


@pytest.mark.asyncio
async def test_concurrent_raw_id_collision_is_rechecked_after_insert():
    original = {
        "tweet_id": "fixture-raw-race", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-raw-race",
        "user_id": "u1", "text": "original text",
    }

    class RacingPool(FakePool):
        async def execute(self, sql, *args):
            if "INSERT INTO raw_tweets" in sql:
                self.raw_identity = raw_identity_for(original)
            return await super().execute(sql, *args)

    pool = RacingPool()
    redis = FakeRedisQueue()
    worker = TweetAnalyzerWorker(redis, pool, RedisEventPublisher(redis))
    replay = {**original, "text": "different text"}

    with pytest.raises(RuntimeError, match="Raw source identity conflicts"):
        await worker.process_tweet(replay, json.dumps(replay))

    assert redis.values == {}
    assert redis.events == []


@pytest.mark.asyncio
async def test_cti_archive_race_rechecks_under_lock_and_skips_hot_writes(monkeypatch):
    class FakeCrypto:
        def extract_wallets(self, _text):
            return []

    class NoEmbedding:
        async def get_embedding(self, _text):
            return None

    class FakeCartel:
        async def analyze_cartel_signal(self, _text):
            return {"is_threat": True, "category": "fixture", "confidence_score": 82}

    class FakeScoring:
        def calculate_admiralty_code(self, **_kwargs):
            return "A1", 87

    monkeypatch.setattr(analyzer_module, "CryptoOSINTTracker", FakeCrypto)
    monkeypatch.setattr(analyzer_module, "SemanticAnomalyDetector", NoEmbedding)
    monkeypatch.setattr(analyzer_module, "CartelIntelligence", FakeCartel)
    monkeypatch.setattr(analyzer_module, "EnterpriseScoringEngine", FakeScoring)
    monkeypatch.setattr(
        analyzer_module.link_hunter,
        "sniff_and_store_links",
        lambda **_kwargs: asyncio.sleep(0, result=0),
    )

    record = {
        "tweet_id": "fixture-race", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-race",
        "username": "fixture", "text": "cartel fixture",
    }
    pool = FakePool()
    pool.archived_identity = archived_identity_for(record)
    redis = FakeRedisQueue()

    await process_cartel_pipeline(record, pool, RedisEventPublisher(redis))

    lock_sql = [sql for sql, _ in pool.statements if "pg_advisory_" in sql]
    assert len(lock_sql) == 2
    assert "pg_advisory_lock" in lock_sql[0]
    assert "pg_advisory_unlock" in lock_sql[1]
    assert not any(
        "INSERT INTO cartel_threat_signals" in sql
        or "INSERT INTO enterprise_intel_reports" in sql
        for sql, _ in pool.statements
    )
    assert redis.events == []

    pool.statements.clear()
    pool.archived_identity["raw_text_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="Archived CTI source identity conflicts"):
        await process_cartel_pipeline(record, pool, RedisEventPublisher(redis))
    assert ["pg_advisory_lock" in sql for sql, _ in pool.statements] == [True, False]
    assert "pg_advisory_unlock" in pool.statements[-1][0]

    pool.statements.clear()
    pool.archived_identity = archived_identity_for(record)
    pool.hot_identity = None
    await process_cartel_pipeline(record, pool, object())
    assert not any("INSERT INTO crypto_intelligence" in sql for sql, _ in pool.statements)
    assert not any("INSERT INTO cartel_threat_signals" in sql for sql, _ in pool.statements)

    pool.statements.clear()
    pool.archived_identity = None
    pool.hot_identity = hot_identity_for({**record, "text": "original CTI text"})
    with pytest.raises(RuntimeError, match="Hot CTI source identity conflicts"):
        await process_cartel_pipeline(record, pool, RedisEventPublisher(redis))
    assert not any(
        "INSERT INTO cartel_threat_signals" in sql
        or "INSERT INTO enterprise_intel_reports" in sql
        for sql, _ in pool.statements
    )
    assert "pg_advisory_unlock" in pool.statements[-1][0]


@pytest.mark.asyncio
async def test_direct_cti_pipeline_rejects_conflicting_raw_source_before_enrichment():
    record = {
        "tweet_id": "fixture-direct-raw", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-direct-raw",
        "username": "fixture", "text": "replacement text",
    }
    original = {**record, "text": "original text"}
    pool = FakePool()
    pool.raw_identity = raw_identity_for(original)

    with pytest.raises(RuntimeError, match="Raw source identity conflicts"):
        await process_cartel_pipeline(record, pool, object())

    assert not any(
        "INSERT INTO crypto_intelligence" in sql
        or "INSERT INTO cartel_threat_signals" in sql
        or "INSERT INTO enterprise_intel_reports" in sql
        for sql, _ in pool.statements
    )


@pytest.mark.asyncio
async def test_repository_cti_writer_respects_archived_source_identity():
    pool = FakePool()
    repository = RadarRepository(pool)
    record = {
        "tweet_id": "fixture-repository", "source_platform": "X_TWITTER",
        "source_channel_id": "", "source_message_id": "fixture-repository",
        "text": "cartel fixture",
    }
    kwargs = {
        "tweet_id": record["tweet_id"], "username": "fixture",
        "raw_text": record["text"],
        "text_hash": hashlib.sha256(record["text"].encode()).hexdigest(),
        "threat_category": "fixture", "location_context": "fixture",
        "confidence_score": 80,
    }
    pool.archived_identity = archived_identity_for(record)

    await repository.save_cartel_threat(**kwargs)
    assert not any("INSERT INTO cartel_threat_signals" in sql for sql, _ in pool.statements)
    assert "pg_advisory_unlock" in pool.statements[-1][0]

    pool.statements.clear()
    pool.archived_identity["raw_text_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="Archived CTI source identity conflicts"):
        await repository.save_cartel_threat(**kwargs)
    assert "pg_advisory_unlock" in pool.statements[-1][0]

    pool.statements.clear()
    pool.archived_identity = None
    await repository.save_cartel_threat(**kwargs)
    assert any("INSERT INTO cartel_threat_signals" in sql for sql, _ in pool.statements)

    pool.statements.clear()
    pool.hot_identity = hot_identity_for({**record, "text": "original CTI text"})
    with pytest.raises(RuntimeError, match="Hot CTI source identity conflicts"):
        await repository.save_cartel_threat(**kwargs)
    assert not any("INSERT INTO cartel_threat_signals" in sql for sql, _ in pool.statements)


@pytest.mark.asyncio
async def test_cti_pipeline_persists_pending_report_before_shared_alarm(monkeypatch):
    scoring_calls = []

    class FakeCrypto:
        def extract_wallets(self, _text):
            return []

    class FakeSemantic:
        async def get_embedding(self, _text):
            return [0.1, 0.2]

    class FakeCartel:
        async def analyze_cartel_signal(self, _text):
            return {
                "is_threat": True,
                "category": "fixture threat",
                "detected_faction": "fixture",
                "confidence_score": 82,
            }

    class FakeScoring:
        def calculate_admiralty_code(self, **kwargs):
            scoring_calls.append(kwargs)
            return "A1", 87

    class FakeSIEM:
        def generate_stix_bundle(self, *_args):
            return {"type": "bundle", "id": "bundle--fixture", "objects": []}

        async def push_to_siem(self, *_args, **_kwargs):
            raise AssertionError("pre-review SIEM export must never run")

    class FakeGraph:
        async def map_threat_network(self, **_kwargs):
            return None

    monkeypatch.setattr(analyzer_module, "CryptoOSINTTracker", FakeCrypto)
    monkeypatch.setattr(analyzer_module, "SemanticAnomalyDetector", FakeSemantic)
    monkeypatch.setattr(analyzer_module, "CartelIntelligence", FakeCartel)
    monkeypatch.setattr(analyzer_module, "EnterpriseScoringEngine", FakeScoring)
    monkeypatch.setattr(analyzer_module, "SIEMIntegrationEngine", FakeSIEM)
    monkeypatch.setattr(analyzer_module, "Neo4jIntelligence", FakeGraph)
    monkeypatch.setattr(
        analyzer_module.link_hunter,
        "sniff_and_store_links",
        lambda **_kwargs: asyncio.sleep(0, result=0),
    )

    redis = FakeRedisQueue()
    pool = FakePool()

    class OrderedPublisher(RedisEventPublisher):
        async def broadcast_alert(self, message):
            assert any("INSERT INTO enterprise_intel_reports" in sql for sql, _ in pool.statements)
            return await super().broadcast_alert(message)

    publisher = OrderedPublisher(redis)
    record = {
        "source_platform": "TELEGRAM",
        "source_channel_id": "-100123",
        "source_message_id": "42",
        "tweet_id": "42",
        "username": "tg_fixture",
        "text": "Sinaloa cartel fixture",
        "created_at": "Sat Oct 03 10:00:00 +0000 2020",
    }
    await process_cartel_pipeline(record, pool, publisher)

    signal_insert = next(item for item in pool.statements if "INSERT INTO cartel_threat_signals" in item[0])
    report_insert = next(item for item in pool.statements if "INSERT INTO enterprise_intel_reports" in item[0])
    assert signal_insert[1][:4] == ("telegram:-100123:42", "TELEGRAM", "-100123", "42")
    assert report_insert[1][1:5] == ("telegram:-100123:42", "TELEGRAM", "-100123", "42")
    assert "FALSE" in report_insert[0]
    assert "review_status = 'PENDING'" in report_insert[0]
    channel, serialized = next(item for item in redis.events if isinstance(item, tuple))
    event = json.loads(serialized)
    assert channel == "radar:events:v1"
    assert event["event"] == "ANOMALY_DETECTED"
    assert event["tweet_id"] == "telegram:-100123:42"
    assert event["score"] == 87
    assert event["score_kind"] == "confidence"
    assert scoring_calls[0]["account_age_days"] is None

    account_record = {
        **record,
        "tweet_id": "43",
        "source_message_id": "43",
        "account_created_at": "2020-01-01T00:00:00Z",
    }
    await process_cartel_pipeline(account_record, pool, publisher)
    assert scoring_calls[1]["account_age_days"] > 365


def test_celery_tasks_reuse_one_process_event_loop(monkeypatch):
    class FakeDatabase:
        def __init__(self):
            self.pool = object()
            self.connected = 0
            self.disconnected = 0

        async def connect(self):
            self.connected += 1

        async def disconnect(self):
            self.disconnected += 1
            self.pool = None

    database = FakeDatabase()
    monkeypatch.setattr(celery_module, "_database_manager", lambda: database)
    celery_module.initialize_worker_database()

    async def current_loop_id():
        return id(asyncio.get_running_loop())

    first = celery_module.run_task_coroutine(current_loop_id())
    second = celery_module.run_task_coroutine(current_loop_id())
    assert first == second
    assert database.connected == 1

    celery_module.close_worker_database()
    assert database.disconnected == 1
    assert celery_module._worker_loop is None


@pytest.mark.parametrize(
    "update_result,hot_exists,cold_called,expected_error",
    [
        ("UPDATE 1", False, False, None),
        ("UPDATE 0", False, True, None),
        ("UPDATE 0", True, False, "hot signal text does not match"),
        ("UPDATE 2", False, False, "expected one signal update"),
    ],
)
def test_heavy_ai_inference_serializes_hot_and_cold_write(
    monkeypatch, update_result, hot_exists, cold_called, expected_error
):
    from app.cron.archive_to_cold import ColdDataLakeArchiver

    class FakeCartel:
        async def analyze_cartel_signal(self, raw_text):
            assert raw_text == "fixture text"
            return {"category": "fixture"}

    class UpdatePool:
        def __init__(self):
            self.statement = None
            self.calls = []

        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, *args):
            if "pg_advisory_lock" in sql:
                self.calls.append("lock")
                assert args == (ColdDataLakeArchiver.LOCK_ID,)
                return "SELECT 1"
            if "pg_advisory_unlock" in sql:
                self.calls.append("unlock")
                return "SELECT 1"
            self.calls.append("update")
            self.statement = (sql, args)
            return update_result

        async def fetchval(self, sql, *args):
            if "FROM enterprise_intel_reports" in sql:
                self.calls.append("report_source")
                assert args == ("report-1",)
                return "source-1"
            if "nextval('public.cold_write_version_seq'::regclass)" in sql:
                self.calls.append("cold_version")
                assert args == ()
                return 100
            self.calls.append("hot_exists")
            assert "SELECT EXISTS" in sql
            assert args == ("source-1",)
            return hot_exists

    pool = UpdatePool()
    monkeypatch.setattr(database_module.db, "pool", pool)
    monkeypatch.setattr(analyzer_module, "CartelIntelligence", FakeCartel)
    monkeypatch.setattr(celery_module, "operations_paused", lambda: False)
    monkeypatch.setattr(celery_module, "run_task_coroutine", asyncio.run)
    async def enrich(_self, tweet_id, raw_text, text_analysis, visual_analysis, *, version):
        pool.calls.append("cold")
        assert tweet_id == "source-1"
        assert raw_text == "fixture text"
        assert text_analysis == {"category": "fixture"}
        assert visual_analysis == {}
        assert version == 100
        return "42"

    monkeypatch.setattr(ColdDataLakeArchiver, "enrich_archived_signal", enrich)

    if expected_error:
        with pytest.raises(RuntimeError, match=expected_error):
            celery_module.heavy_ai_inference.run("report-1", "source-1", "fixture text")
    else:
        result = celery_module.heavy_ai_inference.run("report-1", "source-1", "fixture text")
        assert "report-1" in result

    assert "UPDATE cartel_threat_signals" in pool.statement[0]
    assert json.loads(pool.statement[1][0]) == {"category": "fixture"}
    assert pool.statement[1][2] == "source-1"
    assert pool.statement[1][3] == "fixture text"
    assert pool.calls[0:3] == ["lock", "report_source", "update"]
    assert ("cold" in pool.calls) is cold_called
    assert pool.calls[-1] == "unlock"


def test_heavy_ai_inference_releases_lock_when_cold_write_fails(monkeypatch):
    from app.cron.archive_to_cold import ColdDataLakeArchiver

    class FakeCartel:
        async def analyze_cartel_signal(self, _raw_text):
            return {"category": "fixture"}

    class FakePool:
        def __init__(self):
            self.calls = []

        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, *_args):
            if "pg_advisory_unlock" in sql:
                self.calls.append("unlock")
            elif "pg_advisory_lock" in sql:
                self.calls.append("lock")
            else:
                self.calls.append("update")
                return "UPDATE 0"
            return "SELECT 1"

        async def fetchval(self, sql, *_args):
            if "FROM enterprise_intel_reports" in sql:
                self.calls.append("report_source")
                return "source-1"
            if "nextval('public.cold_write_version_seq'::regclass)" in sql:
                self.calls.append("cold_version")
                return 100
            self.calls.append("hot_exists")
            return False

    async def fail_cold(*_args, **_kwargs):
        pool.calls.append("cold")
        raise RuntimeError("cold lake unavailable")

    pool = FakePool()
    monkeypatch.setattr(database_module.db, "pool", pool)
    monkeypatch.setattr(analyzer_module, "CartelIntelligence", FakeCartel)
    monkeypatch.setattr(celery_module, "operations_paused", lambda: False)
    monkeypatch.setattr(celery_module, "run_task_coroutine", asyncio.run)
    monkeypatch.setattr(ColdDataLakeArchiver, "enrich_archived_signal", fail_cold)

    with pytest.raises(RuntimeError, match="cold lake unavailable"):
        celery_module.heavy_ai_inference.run("report-1", "source-1", "fixture text")
    assert pool.calls == [
        "lock", "report_source", "update", "hot_exists", "cold_version",
        "cold", "unlock",
    ]


@pytest.mark.parametrize("report_source", [None, "different-source"])
def test_heavy_ai_inference_rejects_unrelated_report_before_writing(monkeypatch, report_source):
    class FakeCartel:
        async def analyze_cartel_signal(self, _raw_text):
            return {"category": "fixture"}

    class FakePool:
        def __init__(self):
            self.calls = []

        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, *_args):
            if "pg_advisory_unlock" in sql:
                self.calls.append("unlock")
            elif "pg_advisory_lock" in sql:
                self.calls.append("lock")
            else:
                raise AssertionError("The hot signal must not be updated")
            return "SELECT 1"

        async def fetchval(self, sql, report_id):
            self.calls.append("report_source")
            assert "FROM enterprise_intel_reports" in sql
            assert report_id == "report-1"
            return report_source

    pool = FakePool()
    monkeypatch.setattr(database_module.db, "pool", pool)
    monkeypatch.setattr(analyzer_module, "CartelIntelligence", FakeCartel)
    monkeypatch.setattr(celery_module, "operations_paused", lambda: False)
    monkeypatch.setattr(celery_module, "run_task_coroutine", asyncio.run)

    with pytest.raises(RuntimeError, match="report does not match"):
        celery_module.heavy_ai_inference.run("report-1", "source-1", "fixture text")
    assert pool.calls == ["lock", "report_source", "unlock"]


def test_collection_tasks_stay_off_without_opt_in(monkeypatch):
    from app.scrapers import collection as collection_module

    monkeypatch.setattr(collection_module.settings, "COLLECTION_ENABLED", False)
    monkeypatch.setattr(
        celery_module,
        "operations_paused",
        lambda: (_ for _ in ()).throw(AssertionError("disabled tasks must not contact Redis")),
    )

    assert celery_module.light_ingest_sweep("fixture")["status"] == "disabled"
    assert celery_module.campaign_ingest_sweep("narcotics_mexico")["status"] == "disabled"
    assert celery_module.x_telegram_link_seeder()["status"] == "disabled"
    assert celery_module.osint_telegram_link_seeder()["status"] == "disabled"
    assert celery_module.telegram_channel_sweep()["status"] == "disabled"
    assert celery_module.persona_warming_cycle()["status"] == "disabled"


@pytest.mark.asyncio
async def test_kill_switch_reports_paused_only_after_queue_and_analyzer_ack(monkeypatch):
    import app.routers.tasks as tasks_module

    class FakeControl:
        def inspect(self, timeout):
            assert timeout == 3.0
            return FakeInspection()

        def revoke(self, *_args, **_kwargs):
            raise AssertionError("No task needs revoking in this fixture")

        def purge(self):
            return 0

    class FakeInspection:
        def ping(self):
            return {"worker@fixture": {}}

        def active(self):
            return {"worker@fixture": []}

        def reserved(self):
            return {"worker@fixture": []}

    class FakeRedis:
        def __init__(self):
            self.values = {"radar:analyzer:state": "PAUSED"}

        async def set(self, key, value):
            self.values[key] = value

        async def get(self, key):
            return self.values.get(key)

        async def llen(self, _key):
            return 0

    monkeypatch.setattr(tasks_module, "celery", SimpleNamespace(control=FakeControl()))
    redis = FakeRedis()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis_client=redis)))
    response = await tasks_module.engage_kill_switch(request, _user=None)

    assert redis.values["radar:operations:state"] == "PAUSED"
    assert response["analyzer_acknowledged"] is True
    assert response["celery_workers_acknowledged"] is True
    assert response["worker_acknowledged"] is True
    assert response["status"] == "PAUSED"
    assert response["queued_tasks_removed"] == 0


@pytest.mark.asyncio
async def test_kill_switch_terminates_active_task_without_json_backend_system_exit(monkeypatch):
    import app.routers.tasks as tasks_module

    class FakeInspection:
        def __init__(self):
            self.running = True

        def ping(self):
            return {"worker@fixture": {}}

        def active(self):
            return {"worker@fixture": [{"id": "active-task"}] if self.running else []}

        def reserved(self):
            return {"worker@fixture": []}

    inspection = FakeInspection()

    class FakeControl:
        def __init__(self):
            self.revoked = []

        def inspect(self, timeout):
            assert timeout == 3.0
            return inspection

        def revoke(self, task_id, **kwargs):
            self.revoked.append((task_id, kwargs))
            inspection.running = False

        def purge(self):
            return 0

    class FakeRedis:
        def __init__(self):
            self.values = {"radar:analyzer:state": "PAUSED"}

        async def set(self, key, value):
            self.values[key] = value

        async def get(self, key):
            return self.values.get(key)

        async def llen(self, _key):
            return 0

    control = FakeControl()
    monkeypatch.setattr(tasks_module, "celery", SimpleNamespace(control=control))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis_client=FakeRedis()))
    )

    response = await tasks_module.engage_kill_switch(request, _user=None)

    assert control.revoked == [
        ("active-task", {"terminate": True, "signal": "SIGKILL"})
    ]
    assert response["active_tasks_cancel_requested"] == 1
    assert response["active_tasks_remaining"] == 0
    assert response["status"] == "PAUSED"
