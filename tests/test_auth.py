import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import AuthUser, create_session, hash_password, password_hash_rounds, resolve_session, revoke_session, verify_password
from app.config import settings
from app.dependencies import require_admin, require_ingest_access
from app.routers.auth import (
    LOGIN_ATTEMPT_SCRIPT,
    LOGIN_SUCCESS_SCRIPT,
    _login_client,
    _reserve_login_attempt,
    _release_successful_login,
    _valid_credentials,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}
        self.deadlines = {}
        self._lock = threading.RLock()

    def _active(self, key):
        deadline = self.deadlines.get(key)
        if deadline is not None and deadline <= time.monotonic():
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
            self.expirations.pop(key, None)
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and await self.get(key) is not None:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        self.deadlines[key] = time.monotonic() + ex if ex is not None else None
        return True

    async def get(self, key):
        with self._lock:
            return self._active(key)

    async def delete(self, key):
        existed = key in self.values
        self.values.pop(key, None)
        self.deadlines.pop(key, None)
        self.expirations.pop(key, None)
        return int(existed)

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        self.expirations[key] = seconds
        self.deadlines[key] = time.monotonic() + seconds
        return True

    async def llen(self, _key):
        return 0

    async def eval(self, script, key_count, *args):
        with self._lock:
            if script == LOGIN_ATTEMPT_SCRIPT:
                assert key_count == 2
                client_key, username_key, client_limit, username_limit, seconds, new_gen = args
                client_count = int(self._active(client_key) or 0)
                username_entry = self._active(username_key) or {"count": 0}
                if client_count >= client_limit or username_entry["count"] >= username_limit:
                    return [0, ""]
                self.values[client_key] = client_count + 1
                if client_count == 0 or self.deadlines.get(client_key) is None:
                    self.expirations[client_key] = seconds
                    self.deadlines[client_key] = time.monotonic() + seconds
                if "generation" not in username_entry:
                    username_entry["generation"] = new_gen
                    self.expirations[username_key] = seconds
                    self.deadlines[username_key] = time.monotonic() + seconds
                username_entry["count"] += 1
                self.values[username_key] = username_entry
                return [1, username_entry["generation"]]
            if script == LOGIN_SUCCESS_SCRIPT:
                assert key_count == 1
                username_key, generation = args
                username_entry = self._active(username_key)
                if not username_entry or username_entry["generation"] != generation:
                    return 0
                username_entry["count"] -= 1
                if username_entry["count"] == 0:
                    self.values.pop(username_key)
                    self.deadlines.pop(username_key, None)
                    self.expirations.pop(username_key, None)
                return 1
        return 1


@pytest.fixture
def app_settings():
    original = {
        "RADAR_SESSION_SECRET": settings.RADAR_SESSION_SECRET,
        "RADAR_SESSION_COOKIE": settings.RADAR_SESSION_COOKIE,
        "RADAR_SESSION_TTL_SECONDS": settings.RADAR_SESSION_TTL_SECONDS,
        "RADAR_COOKIE_SECURE": settings.RADAR_COOKIE_SECURE,
        "RADAR_ALLOWED_ORIGINS": settings.RADAR_ALLOWED_ORIGINS,
        "RADAR_MAX_WS_PER_USER": settings.RADAR_MAX_WS_PER_USER,
        "RADAR_WS_SESSION_RECHECK_SECONDS": settings.RADAR_WS_SESSION_RECHECK_SECONDS,
        "RADAR_INGEST_API_KEY": settings.RADAR_INGEST_API_KEY,
        "RADAR_USERS_JSON": settings.RADAR_USERS_JSON,
    }
    settings.RADAR_SESSION_SECRET = "test-session-secret-" + "x" * 40
    settings.RADAR_SESSION_COOKIE = "radar_session"
    settings.RADAR_SESSION_TTL_SECONDS = 3600
    settings.RADAR_COOKIE_SECURE = False
    settings.RADAR_ALLOWED_ORIGINS = "http://testserver"
    settings.RADAR_MAX_WS_PER_USER = 5
    settings.RADAR_WS_SESSION_RECHECK_SECONDS = 0.05
    settings.RADAR_INGEST_API_KEY = "fixture-ingest-key"
    try:
        yield settings
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def test_password_hash_is_salted_and_verified():
    encoded = hash_password("fixture-password", rounds=100_000)
    assert encoded.startswith("pbkdf2_sha256$100000$")
    assert verify_password("fixture-password", encoded)
    assert not verify_password("incorrect", encoded)


def test_signed_redis_session_and_tamper_rejection(app_settings):
    redis = FakeRedis()
    user = AuthUser(username="analyst-a", role="analyst")
    cookie = asyncio.run(create_session(redis, user, app_settings))
    assert asyncio.run(resolve_session(redis, cookie, app_settings)) == user
    assert asyncio.run(resolve_session(redis, cookie + "x", app_settings)) is None
    assert next(iter(redis.expirations.values())) == 3600


def test_session_is_revoked_when_configured_user_role_or_membership_changes(app_settings):
    redis = FakeRedis()
    account = {"username": "analyst-a", "role": "analyst", "password_hash": "hash-a"}
    app_settings.RADAR_USERS_JSON = json.dumps([account])
    cookie = asyncio.run(create_session(redis, AuthUser("analyst-a", "analyst"), app_settings))
    assert asyncio.run(resolve_session(redis, cookie, app_settings)) is not None

    app_settings.RADAR_USERS_JSON = json.dumps([{**account, "role": "reader"}])
    assert asyncio.run(resolve_session(redis, cookie, app_settings)) is None

    app_settings.RADAR_USERS_JSON = "[]"
    assert asyncio.run(resolve_session(redis, cookie, app_settings)) is None


def test_changed_user_configuration_revokes_http_and_websocket_session(app_settings):
    from app.main import app

    redis = FakeRedis()
    app.state.redis_client = redis
    app.state.settings = app_settings
    account = {"username": "analyst-a", "role": "analyst", "password_hash": "hash-a"}
    app_settings.RADAR_USERS_JSON = json.dumps([account])
    cookie = asyncio.run(create_session(redis, AuthUser("analyst-a", "analyst"), app_settings))
    client = TestClient(app)
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, cookie)
    assert client.get("/api/v1/auth/me").status_code == 200

    app_settings.RADAR_USERS_JSON = json.dumps([{**account, "role": "reader"}])
    assert client.get("/api/v1/auth/me").status_code == 401
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/ws/signals", headers={"Origin": "http://testserver"}):
            pass
    assert rejected.value.code == 4401


def test_cookie_mutations_require_exact_allowed_origin(app_settings):
    from app.main import app

    redis = FakeRedis()
    app.state.redis_client = redis
    app.state.settings = app_settings
    cookie = asyncio.run(create_session(redis, AuthUser("admin-a", "admin"), app_settings))
    client = TestClient(app)
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, cookie)

    for path in (
        "/api/v1/ops/resume",
        "/api/v1/ops/kill-switch",
        "/api/v1/review/alert/report-1/export",
        "/api/v1/auth/logout",
    ):
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"Origin": "https://sibling.example"}).status_code == 403

    allowed = client.post(
        "/api/v1/ops/resume", headers={"Origin": "http://testserver"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["state"] == "RUNNING"

    service_client = TestClient(app)
    service_ingest = service_client.post(
        "/api/v1/ingest",
        headers={"X-Radar-Ingest-Key": app_settings.RADAR_INGEST_API_KEY},
        json={"tweet_id": "fixture-service-record"},
    )
    assert service_ingest.status_code == 200


def test_http_roles_and_ingest_service_are_separated(app_settings):
    redis = FakeRedis()
    app = FastAPI()
    app.state.redis_client = redis
    app.state.settings = app_settings

    @app.get("/admin")
    async def admin(_user=Depends(require_admin)):
        return {"ok": True}

    @app.post("/ingest")
    async def ingest(_user=Depends(require_ingest_access)):
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/admin").status_code == 401
    assert client.post("/ingest", headers={"X-Radar-Ingest-Key": "wrong"}).status_code == 403
    assert client.post("/ingest", headers={"X-Radar-Ingest-Key": "fixture-ingest-key"}).status_code == 200

    reader_cookie = asyncio.run(create_session(redis, AuthUser("reader-a", "reader"), app_settings))
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, reader_cookie)
    assert client.get("/admin").status_code == 403
    assert client.post("/ingest").status_code == 403

    admin_cookie = asyncio.run(create_session(redis, AuthUser("admin-a", "admin"), app_settings))
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, admin_cookie)
    assert client.get("/admin").status_code == 200
    assert client.post("/ingest").status_code == 200


def test_login_issues_http_only_session_cookie(app_settings):
    from app.routers.auth import router

    redis = FakeRedis()
    password_hash = hash_password("fixture-password", rounds=100_000)
    app = FastAPI()
    app.state.redis_client = redis
    app.state.auth_users = {"analyst-a": {"password_hash": password_hash, "role": "analyst"}}
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": "analyst-a", "password": "fixture-password"},
    )
    assert response.status_code == 200
    assert response.json() == {"username": "analyst-a", "role": "analyst"}
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "radar_session" in client.cookies
    assert client.get("/api/v1/auth/me").json()["username"] == "analyst-a"

    logout = client.post(
        "/api/v1/auth/logout", headers={"Origin": "http://testserver"}
    )
    assert logout.status_code == 204
    assert "radar_session" not in client.cookies
    assert client.get("/api/v1/auth/me").status_code == 401

    bad = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": "analyst-a", "password": "wrong"},
    )
    assert bad.status_code == 401


def test_login_failures_are_scoped_to_client_and_user_with_fixed_expiry(app_settings):
    from app.routers.auth import router

    redis = FakeRedis()
    password_hash = hash_password("fixture-password", rounds=100_000)
    app = FastAPI()
    app.state.redis_client = redis
    app.state.auth_users = {
        "user-a": {"password_hash": password_hash, "role": "reader"},
        "user-b": {"password_hash": password_hash, "role": "reader"},
    }
    app.include_router(router)
    client = TestClient(app)

    first = client.post("/api/v1/auth/login", json={"username": "user-a", "password": "wrong"})
    assert first.status_code == 401
    rate_key = next(key for key in redis.values if key.startswith("radar:login:user:v3:"))
    first_deadline = redis.deadlines[rate_key]

    for _ in range(9):
        assert client.post(
            "/api/v1/auth/login", json={"username": "user-a", "password": "wrong"}
        ).status_code == 401
    assert redis.deadlines[rate_key] == first_deadline
    assert redis.expirations[rate_key] == 900
    assert client.post(
        "/api/v1/auth/login", json={"username": "user-a", "password": "wrong"}
    ).status_code == 429

    other_user = client.post(
        "/api/v1/auth/login",
        json={"username": "user-b", "password": "fixture-password"},
    )
    assert other_user.status_code == 200
    assert other_user.json()["username"] == "user-b"


@pytest.mark.asyncio
async def test_login_counter_expiry_boundary_cannot_create_permanent_lock():
    class BoundaryRedis(FakeRedis):
        async def set(self, *_args, **_kwargs):
            raise AssertionError("failure counter must not use a separate SET")

        async def incr(self, *_args):
            raise AssertionError("failure counter must not use a separate INCR")

    redis = BoundaryRedis()
    client_key = "radar:login:client:v3:fixture-boundary"
    username_key = "radar:login:user:v3:fixture-boundary"
    for key, value in ((client_key, 29), (username_key, {"count": 9, "generation": "old"})):
        redis.values[key] = value
        redis.deadlines[key] = time.monotonic() - 1
        redis.expirations[key] = 900

    generation = await _reserve_login_attempt(redis, client_key, username_key)
    assert generation is not None
    assert redis.values[client_key] == 1
    assert redis.values[username_key]["count"] == 1
    assert redis.expirations[client_key] == redis.expirations[username_key] == 900
    assert redis.deadlines[client_key] > time.monotonic()

    first_deadline = redis.deadlines[client_key]
    assert await _reserve_login_attempt(redis, client_key, username_key) == generation
    assert redis.deadlines[client_key] == first_deadline
    await _release_successful_login(redis, username_key, "old")
    assert redis.values[username_key]["count"] == 2
    await _release_successful_login(redis, username_key, generation)
    assert redis.values[username_key]["count"] == 1


def test_unknown_usernames_have_bounded_rate_limit_state(app_settings, monkeypatch):
    from app.routers.auth import router

    monkeypatch.setattr("app.routers.auth.verify_password", lambda *_args: False)
    redis = FakeRedis()
    app = FastAPI()
    app.state.redis_client = redis
    app.state.auth_users = {"known": {"password_hash": hash_password("valid", rounds=100_000), "role": "reader"}}
    app.include_router(router)
    client = TestClient(app)

    for index in range(30):
        result = client.post("/api/v1/auth/login", json={"username": f"invented-{index}", "password": "wrong"})
        assert result.status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "invented-30", "password": "wrong"}).status_code == 429
    assert len([key for key in redis.values if key.startswith("radar:login:user:v3:")]) == 30


def test_concurrent_guesses_cannot_pass_atomic_reservation(app_settings, monkeypatch):
    from app.routers.auth import router

    verified = []
    verified_lock = threading.Lock()

    def slow_rejection(*_args):
        with verified_lock:
            verified.append(1)
        time.sleep(0.05)
        return False

    monkeypatch.setattr("app.routers.auth.verify_password", slow_rejection)
    redis = FakeRedis()
    app = FastAPI()
    app.state.redis_client = redis
    app.state.auth_users = {"known": {"password_hash": hash_password("valid", rounds=100_000), "role": "reader"}}
    app.include_router(router)

    def guess(_index):
        with TestClient(app) as client:
            return client.post("/api/v1/auth/login", json={"username": "known", "password": "wrong"}).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(guess, range(20)))
    assert sorted(statuses) == [401] * 10 + [429] * 10
    assert len(verified) == 10


def test_login_only_accepts_client_ip_from_identified_gateway(monkeypatch):
    monkeypatch.setattr("app.routers.auth._gateway_addresses", lambda: {"10.0.0.3"})

    def request(peer, real_ip):
        headers = [(b"x-forwarded-for", b"198.51.100.90")]
        if real_ip is not None:
            headers.append((b"x-real-ip", real_ip.encode()))
        return Request({"type": "http", "headers": headers, "client": (peer, 1234)})

    assert _login_client(request("10.0.0.3", "198.51.100.10")) == "198.51.100.10"
    assert _login_client(request("10.0.0.4", "198.51.100.10")) == "10.0.0.4"
    for header in (None, "forged.example", "198.51.100.10, 198.51.100.11"):
        with pytest.raises(HTTPException) as error:
            _login_client(request("10.0.0.3", header))
        assert error.value.status_code == 503

    monkeypatch.setattr("app.routers.auth._gateway_addresses", lambda: set())
    with pytest.raises(HTTPException) as error:
        _login_client(request("10.0.0.3", "198.51.100.10"))
    assert error.value.status_code == 503


def test_unknown_usernames_use_decoy_password_work(app_settings, monkeypatch):
    import app.routers.auth as auth_router

    users = {
        "lower-cost": {"password_hash": hash_password("valid", rounds=100_000), "role": "reader"},
        "higher-cost": {"password_hash": hash_password("valid", rounds=200_000), "role": "reader"},
    }
    verified_rounds = []
    padding_rounds = []

    def inspect_verification(_password, encoded_hash):
        verified_rounds.append(password_hash_rounds(encoded_hash))
        return False

    monkeypatch.setattr(auth_router, "verify_password", inspect_verification)
    monkeypatch.setattr(auth_router.hashlib, "pbkdf2_hmac", lambda _algo, _password, _salt, rounds: padding_rounds.append(rounds))

    assert not _valid_credentials("wrong", "lower-cost", users)
    assert not _valid_credentials("wrong", "unknown", users)
    assert verified_rounds == [100_000, 200_000]
    assert padding_rounds == [100_000]


def test_websocket_rejects_anonymous_and_accepts_signed_user(app_settings):
    from app.main import app, ws_manager

    redis = FakeRedis()
    app.state.redis_client = redis
    app.state.settings = app_settings
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/ws/signals", headers={"Origin": "http://testserver"}):
            pass
    assert rejected.value.code == 4401

    cookie = asyncio.run(create_session(redis, AuthUser("reader-a", "reader"), app_settings))
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, cookie)
    with client.websocket_connect(
        "/ws/signals",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert ws_manager.connection_count("reader-a") == 1
        asyncio.run(revoke_session(redis, cookie, app_settings))
        with pytest.raises(WebSocketDisconnect) as revoked:
            websocket.receive_text()
        assert revoked.value.code == 4401
    assert ws_manager.connection_count("reader-a") == 0

    client.cookies.delete(app_settings.RADAR_SESSION_COOKIE)
    with pytest.raises(WebSocketDisconnect) as rejected_origin:
        with client.websocket_connect(
            "/ws/signals",
            headers={"Origin": "https://attacker.invalid"},
        ):
            pass
    assert rejected_origin.value.code == 4403

    app_settings.RADAR_SESSION_TTL_SECONDS = 0.1
    expiring_cookie = asyncio.run(create_session(redis, AuthUser("reader-b", "reader"), app_settings))
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, expiring_cookie)
    with client.websocket_connect(
        "/ws/signals",
        headers={"Origin": "http://testserver"},
    ) as websocket:
        assert ws_manager.connection_count("reader-b") == 1
        time.sleep(0.15)
        with pytest.raises(WebSocketDisconnect) as expired:
            websocket.receive_text()
    assert expired.value.code == 4401
    assert ws_manager.connection_count("reader-b") == 0


def test_websocket_rechecks_revoked_session_while_client_keeps_sending(app_settings):
    from app.main import app, ws_manager

    app_settings.RADAR_WS_SESSION_RECHECK_SECONDS = 0.02
    redis = FakeRedis()
    app.state.redis_client = redis
    app.state.settings = app_settings
    user = AuthUser("active-reader", "reader")
    cookie = asyncio.run(create_session(redis, user, app_settings))
    client = TestClient(app)
    client.cookies.set(app_settings.RADAR_SESSION_COOKIE, cookie)
    revoked = threading.Event()

    def revoke_after_activity():
        time.sleep(0.12)
        asyncio.run(revoke_session(redis, cookie, app_settings))
        revoked.set()

    revoker = threading.Thread(target=revoke_after_activity, daemon=True)
    revoker.start()
    with client.websocket_connect(
        "/ws/signals", headers={"Origin": "http://testserver"}
    ) as websocket:
        deadline = time.monotonic() + 1.0
        while not revoked.is_set() and time.monotonic() < deadline:
            try:
                websocket.send_text("keepalive")
            except (RuntimeError, WebSocketDisconnect):
                break
            time.sleep(0.002)
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
        assert closed.value.code == 4401
    revoker.join(timeout=1)
    assert ws_manager.connection_count("active-reader") == 0


@pytest.mark.asyncio
async def test_websocket_connection_slot_is_reserved_before_accept():
    from app.main import ExtensionConnectionManager

    manager = ExtensionConnectionManager()
    accept_started = asyncio.Event()
    release_accept = asyncio.Event()

    class SlowWebSocket:
        def __init__(self):
            self.accepted = False

        async def accept(self):
            accept_started.set()
            await release_accept.wait()
            self.accepted = True

    first = SlowWebSocket()
    second = SlowWebSocket()
    first_task = asyncio.create_task(manager.connect(first, "reader-a", 1))
    await asyncio.wait_for(accept_started.wait(), timeout=1)
    assert manager.connection_count("reader-a") == 1
    assert await manager.connect(second, "reader-a", 1) is False
    assert second.accepted is False

    release_accept.set()
    assert await first_task is True
    assert manager.connection_count("reader-a") == 1
    manager.disconnect(first)
    assert manager.connection_count("reader-a") == 0

    class FailedWebSocket:
        async def accept(self):
            raise ConnectionError("fixture handshake failure")

    with pytest.raises(ConnectionError):
        await manager.connect(FailedWebSocket(), "reader-a", 1)
    assert manager.connection_count("reader-a") == 0


def test_browser_bundle_contains_no_shared_service_key():
    from pathlib import Path

    source = Path("frontend/lib/api.ts").read_text()
    assert "NEXT_PUBLIC_API_KEY" not in source
    assert "X-Radar-API-Key" not in source
    assert "dev_secret_key" not in source
