import hashlib
import ipaddress
import secrets
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import AuthUser, create_session, password_hash_rounds, revoke_session, verify_password
from app.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_SECONDS = 900
LOGIN_CLIENT_LIMIT = 30
LOGIN_ATTEMPT_SCRIPT = """
local client = KEYS[1]
local username = KEYS[2]
local client_count = tonumber(redis.call('GET', client) or '0')
local username_count = tonumber(redis.call('HGET', username, 'count') or '0')
if client_count >= tonumber(ARGV[1]) or username_count >= tonumber(ARGV[2]) then
    return {0, ''}
end
local attempts = redis.call('INCR', client)
if attempts == 1 or redis.call('TTL', client) < 0 then
    redis.call('EXPIRE', client, tonumber(ARGV[3]))
end
local generation = redis.call('HGET', username, 'generation')
if not generation then
    generation = ARGV[4]
    redis.call('HSET', username, 'generation', generation)
    redis.call('EXPIRE', username, tonumber(ARGV[3]))
end
redis.call('HINCRBY', username, 'count', 1)
if redis.call('TTL', username) < 0 then
    redis.call('EXPIRE', username, tonumber(ARGV[3]))
end
return {1, generation}
"""
LOGIN_SUCCESS_SCRIPT = """
if redis.call('HGET', KEYS[1], 'generation') ~= ARGV[1] then
    return 0
end
local count = tonumber(redis.call('HGET', KEYS[1], 'count') or '0')
if count <= 1 then
    redis.call('DEL', KEYS[1])
else
    redis.call('HINCRBY', KEYS[1], 'count', -1)
end
return 1
"""
# The decoy does not authenticate anyone. It only makes unknown usernames run
# the same PBKDF2 verifier as configured usernames.
DUMMY_PASSWORD_SALT = "00" * 16
DUMMY_PASSWORD_DIGEST = "00" * 32


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _verify_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins:
        raise HTTPException(status_code=403, detail="Untrusted origin")


def _gateway_addresses() -> set[str]:
    # Docker DNS identifies the only gateway allowed to supply a client IP.
    try:
        return {item[4][0] for item in socket.getaddrinfo("gateway", None)}
    except OSError:
        return set()


def _login_client(request: Request) -> str:
    peer = request.client.host if request.client else None
    if not peer:
        raise HTTPException(status_code=503, detail="Client identity unavailable")
    gateway_addresses = _gateway_addresses()
    public_https = any(urlparse(origin).scheme == "https" for origin in settings.allowed_origins)
    if not public_https:
        # Loopback HTTP has one shared host peer. Ignore all forwarding headers
        # so a local caller cannot manufacture fresh rate-limit buckets.
        if not gateway_addresses and "x-real-ip" in request.headers:
            raise HTTPException(status_code=503, detail="Client identity unavailable")
        return peer

    # A public deployment must reach the API through the gateway and the host
    # TLS edge. Otherwise the host peer would be a shared quota key.
    if (
        peer not in gateway_addresses
        or request.headers.get("x-forwarded-proto") != "https"
    ):
        raise HTTPException(status_code=503, detail="Client identity unavailable")
    supplied_headers = request.headers.getlist("x-real-ip")
    if len(supplied_headers) != 1:
        raise HTTPException(status_code=503, detail="Client identity unavailable")
    supplied = supplied_headers[0]
    try:
        return str(ipaddress.ip_address(supplied))
    except ValueError as exc:
        # Failing closed here prevents every visitor from sharing the gateway's
        # own bucket if its real-client header is absent or malformed.
        raise HTTPException(status_code=503, detail="Client identity unavailable") from exc


def _login_keys(username: str, client: str) -> tuple[str, str]:
    client_hash = hashlib.sha256(client.encode("utf-8")).hexdigest()
    username_hash = hashlib.sha256(f"{username}\0{client}".encode("utf-8")).hexdigest()
    return f"radar:login:client:v3:{client_hash}", f"radar:login:user:v3:{username_hash}"


async def _reserve_login_attempt(redis, client_key: str, username_key: str) -> str | None:
    # One Redis script reserves both quotas before the expensive PBKDF2 check.
    # The client quota counts every attempt, limiting arbitrary username keys
    # to LOGIN_CLIENT_LIMIT per client in each fixed window.
    accepted, generation = await redis.eval(
        LOGIN_ATTEMPT_SCRIPT,
        2,
        client_key,
        username_key,
        LOGIN_CLIENT_LIMIT,
        LOGIN_FAILURE_LIMIT,
        LOGIN_FAILURE_WINDOW_SECONDS,
        secrets.token_hex(16),
    )
    return generation if int(accepted) else None


async def _release_successful_login(redis, username_key: str, generation: str) -> None:
    # Only a matching generation can release an in-flight reservation. A stale
    # successful check cannot reduce a newer window after expiration.
    await redis.eval(LOGIN_SUCCESS_SCRIPT, 1, username_key, generation)


def _valid_credentials(password: str, username: str, users: dict) -> bool:
    configured = users.get(username)
    max_rounds = max(
        password_hash_rounds(user["password_hash"]) or 600_000
        for user in users.values()
    )
    encoded_hash = (
        configured["password_hash"] if configured else
        f"pbkdf2_sha256${max_rounds}${DUMMY_PASSWORD_SALT}${DUMMY_PASSWORD_DIGEST}"
    )
    valid_password = verify_password(password, encoded_hash)
    rounds = password_hash_rounds(encoded_hash) or max_rounds
    if rounds < max_rounds:
        # Configured users may have different work factors. Consume the missing
        # rounds so an unknown username does not take an obviously faster path.
        hashlib.pbkdf2_hmac("sha256", password.encode(), bytes(16), max_rounds - rounds)
    return bool(configured and valid_password)


@router.post("/login")
async def login(payload: LoginPayload, request: Request, response: Response):
    _verify_origin(request)
    redis = request.app.state.redis_client
    client_key, username_key = _login_keys(payload.username, _login_client(request))
    generation = await _reserve_login_attempt(redis, client_key, username_key)
    if generation is None:
        raise HTTPException(status_code=429, detail="Too many login attempts")

    users = request.app.state.auth_users
    if not _valid_credentials(payload.password, payload.username, users):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    await _release_successful_login(redis, username_key, generation)
    configured = users[payload.username]
    user = AuthUser(username=payload.username, role=configured["role"])
    token = await create_session(redis, user, settings)
    response.set_cookie(
        key=settings.RADAR_SESSION_COOKIE,
        value=token,
        max_age=settings.RADAR_SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.RADAR_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return {"username": user.username, "role": user.role}


@router.get("/me")
async def who_am_i(request: Request):
    token = request.cookies.get(settings.RADAR_SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    from app.auth import resolve_session

    user = await resolve_session(request.app.state.redis_client, token, settings)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"username": user.username, "role": user.role}


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    token = request.cookies.get(settings.RADAR_SESSION_COOKIE)
    if token:
        await revoke_session(request.app.state.redis_client, token, settings)
    response.delete_cookie(
        key=settings.RADAR_SESSION_COOKIE,
        httponly=True,
        secure=settings.RADAR_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    # Returning the injected Response bypasses FastAPI's decorator status.
    # Without an explicit code Uvicorn receives status_code=None and closes
    # the connection, which the gateway reports as 502.
    response.status_code = 204
    return response
