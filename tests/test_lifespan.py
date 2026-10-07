import asyncio

import pytest
from fastapi.responses import JSONResponse

import app.main as main_module
from app.config import Settings, validate_required_settings


class FakePubSub:
    async def subscribe(self, _channel):
        return None

    async def get_message(self, *, ignore_subscribe_messages, timeout):
        assert ignore_subscribe_messages is False
        assert timeout is None
        return {"type": "subscribe", "channel": "radar:events:v1", "data": 1}

    async def listen(self):
        await __import__("asyncio").Event().wait()
        yield None

    async def unsubscribe(self, _channel):
        return None

    async def aclose(self):
        return None


class FakeRedis:
    def __init__(self):
        self.ping_count = 0
        self.closed = False

    async def ping(self):
        self.ping_count += 1
        return True

    def pubsub(self):
        return FakePubSub()

    async def aclose(self):
        self.closed = True


class FailedPubSub(FakePubSub):
    def __init__(self):
        self.fail_listen = asyncio.Event()

    async def listen(self):
        await self.fail_listen.wait()
        raise ConnectionError("fixture pub/sub disconnect")
        yield


class RedisWithFailedFanout(FakeRedis):
    def __init__(self):
        super().__init__()
        self.failed_pubsub = FailedPubSub()

    def pubsub(self):
        return self.failed_pubsub


@pytest.mark.asyncio
async def test_stalled_websocket_does_not_block_other_alert_receivers():
    class StalledSocket:
        def __init__(self):
            self.closed_with = None

        async def send_json(self, _message):
            await asyncio.Event().wait()

        async def close(self, code):
            self.closed_with = code

    class FastSocket:
        def __init__(self):
            self.messages = []
            self.received = asyncio.Event()

        async def send_json(self, message):
            self.messages.append(message)
            self.received.set()

    manager = main_module.ExtensionConnectionManager(send_timeout_seconds=0.05)
    stalled = StalledSocket()
    fast = FastSocket()
    manager.active_connections[stalled] = "slow-reader"
    manager.active_connections[fast] = "fast-reader"
    alert = {"event": "ANOMALY_DETECTED"}

    broadcast = asyncio.create_task(manager.broadcast_alert(alert))
    await asyncio.wait_for(fast.received.wait(), timeout=1.0)
    assert fast.messages == [alert]
    await asyncio.wait_for(broadcast, timeout=1.0)
    assert stalled.closed_with == 1011
    assert stalled not in manager.active_connections
    assert fast in manager.active_connections


@pytest.mark.asyncio
async def test_fanout_closes_pubsub_when_subscribe_fails():
    class SubscriptionErrorPubSub(FakePubSub):
        def __init__(self):
            self.closed = False

        async def subscribe(self, _channel):
            raise ConnectionError("fixture subscription failure")

        async def aclose(self):
            self.closed = True

    pubsub = SubscriptionErrorPubSub()

    class Redis:
        def pubsub(self):
            return pubsub

    with pytest.raises(ConnectionError, match="subscription failure"):
        await main_module.redis_event_fanout(Redis())
    assert pubsub.closed is True


class FakeDatabase:
    def __init__(self):
        self.pool = None
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.pool = object()
        self.connected = True

    async def disconnect(self):
        self.pool = None
        self.disconnected = True


@pytest.mark.asyncio
async def test_api_lifespan_waits_for_database_and_redis(monkeypatch):
    redis = FakeRedis()
    database = FakeDatabase()
    app = main_module.app
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "validate_required_settings", lambda _settings: None)
    monkeypatch.setattr(main_module, "parse_users", lambda _settings: {"fixture": {}})
    monkeypatch.setattr(main_module.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    async with main_module.lifespan(app):
        assert database.connected is True
        assert redis.ping_count == 1
        assert app.state.ready is True
        assert await main_module.readiness() == {"status": "READY"}

        redis.ping = fail_ping
        unavailable = await main_module.readiness()
        assert isinstance(unavailable, JSONResponse)
        assert unavailable.status_code == 503

    assert app.state.ready is False
    assert database.disconnected is True
    assert redis.closed is True


@pytest.mark.asyncio
async def test_api_stays_unready_until_event_subscription_completes(monkeypatch):
    class BlockingPubSub(FakePubSub):
        def __init__(self):
            self.subscribe_started = asyncio.Event()
            self.allow_subscribe = asyncio.Event()

        async def subscribe(self, _channel):
            self.subscribe_started.set()
            await self.allow_subscribe.wait()

    pubsub = BlockingPubSub()

    class Redis(FakeRedis):
        def pubsub(self):
            return pubsub

    redis = Redis()
    database = FakeDatabase()
    app = main_module.app
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "validate_required_settings", lambda _settings: None)
    monkeypatch.setattr(main_module, "parse_users", lambda _settings: {"fixture": {}})
    monkeypatch.setattr(main_module.aioredis, "from_url", lambda *_args, **_kwargs: redis)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_lifespan():
        async with main_module.lifespan(app):
            entered.set()
            await release.wait()

    task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(pubsub.subscribe_started.wait(), timeout=1)
        assert app.state.ready is False
        assert not entered.is_set()
        unavailable = await main_module.readiness()
        assert isinstance(unavailable, JSONResponse)
        assert unavailable.status_code == 503

        pubsub.allow_subscribe.set()
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert await main_module.readiness() == {"status": "READY"}
    finally:
        pubsub.allow_subscribe.set()
        release.set()
        await asyncio.wait_for(task, timeout=1)

    assert app.state.ready is False
    assert redis.closed is True
    assert database.disconnected is True


@pytest.mark.asyncio
async def test_api_stays_unready_until_redis_confirms_subscription(monkeypatch):
    class BlockingAckPubSub(FakePubSub):
        def __init__(self):
            self.ack_requested = asyncio.Event()
            self.allow_ack = asyncio.Event()

        async def get_message(self, *, ignore_subscribe_messages, timeout):
            self.ack_requested.set()
            await self.allow_ack.wait()
            return await super().get_message(
                ignore_subscribe_messages=ignore_subscribe_messages, timeout=timeout
            )

    pubsub = BlockingAckPubSub()

    class Redis(FakeRedis):
        def pubsub(self):
            return pubsub

    redis = Redis()
    database = FakeDatabase()
    app = main_module.app
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "validate_required_settings", lambda _settings: None)
    monkeypatch.setattr(main_module, "parse_users", lambda _settings: {"fixture": {}})
    monkeypatch.setattr(main_module.aioredis, "from_url", lambda *_args, **_kwargs: redis)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_lifespan():
        async with main_module.lifespan(app):
            entered.set()
            await release.wait()

    task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(pubsub.ack_requested.wait(), timeout=1)
        assert app.state.ready is False
        assert not entered.is_set()
        unavailable = await main_module.readiness()
        assert isinstance(unavailable, JSONResponse)
        assert unavailable.status_code == 503

        pubsub.allow_ack.set()
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert await main_module.readiness() == {"status": "READY"}
    finally:
        pubsub.allow_ack.set()
        release.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_api_startup_fails_when_event_subscription_fails(monkeypatch):
    class FailingPubSub(FakePubSub):
        def __init__(self):
            self.closed = False

        async def subscribe(self, _channel):
            raise ConnectionError("fixture subscription failure")

        async def aclose(self):
            self.closed = True

    pubsub = FailingPubSub()

    class Redis(FakeRedis):
        def pubsub(self):
            return pubsub

    redis = Redis()
    database = FakeDatabase()
    app = main_module.app
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "validate_required_settings", lambda _settings: None)
    monkeypatch.setattr(main_module, "parse_users", lambda _settings: {"fixture": {}})
    monkeypatch.setattr(main_module.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    with pytest.raises(ConnectionError, match="fixture subscription failure"):
        async with main_module.lifespan(app):
            pytest.fail("Lifespan must not yield before event subscription succeeds")

    assert app.state.ready is False
    assert pubsub.closed is True
    assert redis.closed is True
    assert database.disconnected is True


@pytest.mark.asyncio
async def test_readiness_fails_when_event_fanout_stops(monkeypatch):
    redis = RedisWithFailedFanout()
    database = FakeDatabase()
    app = main_module.app
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "validate_required_settings", lambda _settings: None)
    monkeypatch.setattr(main_module, "parse_users", lambda _settings: {"fixture": {}})
    monkeypatch.setattr(main_module.aioredis, "from_url", lambda *_args, **_kwargs: redis)

    async with main_module.lifespan(app):
        redis.failed_pubsub.fail_listen.set()
        for _ in range(10):
            if app.state.fanout_task.done():
                break
            await asyncio.sleep(0)
        assert app.state.fanout_task.done()
        unavailable = await main_module.readiness()
        assert isinstance(unavailable, JSONResponse)
        assert unavailable.status_code == 503
        assert redis.ping_count == 1

    assert redis.closed is True


def test_ingest_service_key_is_required_at_startup():
    config = Settings(
        _env_file=None,
        POSTGRES_PASSWORD="fixture-password",
        RADAR_SESSION_SECRET="fixture-session-secret-" + "x" * 32,
        RADAR_INGEST_API_KEY="",
    )
    with pytest.raises(RuntimeError, match="RADAR_INGEST_API_KEY"):
        validate_required_settings(config)


@pytest.mark.parametrize(
    ("setting", "override"),
    [
        ("POSTGRES_PASSWORD", {"POSTGRES_PASSWORD": "REPLACE_WITH_RANDOM_HEX"}),
        (
            "RADAR_SESSION_SECRET",
            {"RADAR_SESSION_SECRET": "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS"},
        ),
        (
            "RADAR_INGEST_API_KEY",
            {"RADAR_INGEST_API_KEY": "REPLACE_WITH_RANDOM_SERVER_ONLY_KEY"},
        ),
        ("NEO4J_PASSWORD", {"NEO4J_PASSWORD": "REPLACE_WITH_RANDOM_HEX"}),
        (
            "CLICKHOUSE_PASSWORD",
            {
                "CLICKHOUSE_URL": (
                    "http://radar_archive:REPLACE_WITH_RANDOM_HEX"
                    "@clickhouse_lake:8123/"
                )
            },
        ),
    ],
)
def test_example_secrets_are_rejected_at_startup(setting, override):
    values = {
        "POSTGRES_PASSWORD": "fixture-password",
        "RADAR_SESSION_SECRET": "fixture-session-secret-" + "x" * 32,
        "RADAR_INGEST_API_KEY": "fixture-ingest-key",
        "NEO4J_PASSWORD": "fixture-neo4j-password",
        "CLICKHOUSE_URL": "http://radar_archive:fixture-password@clickhouse_lake:8123/",
        **override,
    }
    config = Settings(_env_file=None, **values)
    with pytest.raises(RuntimeError, match=setting) as error:
        validate_required_settings(config)
    assert "REPLACE_" not in str(error.value)


async def fail_ping():
    raise ConnectionError("fixture Redis unavailable")
