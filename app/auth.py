import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import Settings

ROLE_READER = "reader"
ROLE_ANALYST = "analyst"
ROLE_ADMIN = "admin"
VALID_ROLES = {ROLE_READER, ROLE_ANALYST, ROLE_ADMIN}
SESSION_KEY_PREFIX = "radar:session:"


def ingest_service_headers(config: Settings) -> dict[str, str]:
    if not config.RADAR_INGEST_API_KEY:
        raise RuntimeError("RADAR_INGEST_API_KEY is not configured")
    return {"X-Radar-Ingest-Key": config.RADAR_INGEST_API_KEY}


@dataclass(frozen=True)
class AuthUser:
    username: str
    role: str


def parse_users(config: Settings) -> dict[str, dict[str, str]]:
    try:
        users = json.loads(config.RADAR_USERS_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RADAR_USERS_JSON must contain a JSON array") from exc
    if not isinstance(users, list) or not users:
        raise RuntimeError("At least one RADAR_USERS_JSON user is required")

    result: dict[str, dict[str, str]] = {}
    for record in users:
        if not isinstance(record, dict):
            raise RuntimeError("Every configured radar user must be an object")
        username = record.get("username")
        password_hash = record.get("password_hash")
        role = record.get("role")
        if not isinstance(username, str) or not username.strip():
            raise RuntimeError("Every configured radar user needs a username")
        if username in result:
            raise RuntimeError(f"Duplicate radar user: {username}")
        if role not in VALID_ROLES:
            raise RuntimeError(f"Invalid role for radar user {username}")
        if password_hash_rounds(password_hash) is None:
            raise RuntimeError(f"User {username} needs a PBKDF2-SHA256 password hash")
        result[username] = {"password_hash": password_hash, "role": role}
    return result


def password_hash_rounds(encoded_hash: str) -> int | None:
    """Validate a stored PBKDF2 hash before it reaches the login path."""
    try:
        algorithm, rounds_text, salt, expected = encoded_hash.split("$", 3)
        rounds = int(rounds_text)
        if (
            algorithm != "pbkdf2_sha256"
            or not 100_000 <= rounds <= 2_000_000
            or len(bytes.fromhex(salt)) < 16
            or len(bytes.fromhex(expected)) != 32
        ):
            return None
        return rounds
    except (AttributeError, TypeError, ValueError):
        return None


def verify_password(password: str, encoded_hash: str) -> bool:
    rounds = password_hash_rounds(encoded_hash)
    if rounds is None:
        return False
    _algorithm, _rounds_text, salt, expected = encoded_hash.split("$", 3)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds)
    return hmac.compare_digest(actual.hex(), expected)


def hash_password(password: str, rounds: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def _session_key(token: str) -> str:
    return SESSION_KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def _signed_token(token: str, config: Settings) -> str:
    signature = hmac.new(
        config.RADAR_SESSION_SECRET.encode(), token.encode(), hashlib.sha256
    ).hexdigest()
    return f"{token}.{signature}"


def _configured_users_version(config: Settings) -> str:
    """Bind sessions to the configured account list, roles, and password hashes."""
    users = json.loads(config.RADAR_USERS_JSON)
    if not isinstance(users, list):
        raise ValueError("RADAR_USERS_JSON must be an array")
    canonical = json.dumps(
        sorted(users, key=lambda item: item.get("username", "") if isinstance(item, dict) else ""),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(
        config.RADAR_SESSION_SECRET.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()


def _verified_token(cookie_value: str | None, config: Settings) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    token, supplied_signature = cookie_value.rsplit(".", 1)
    expected = _signed_token(token, config).rsplit(".", 1)[1]
    return token if hmac.compare_digest(supplied_signature, expected) else None


async def create_session(redis, user: AuthUser, config: Settings) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(
        _session_key(token),
        json.dumps({
            "username": user.username,
            "role": user.role,
            "users_version": _configured_users_version(config),
        }),
        ex=config.RADAR_SESSION_TTL_SECONDS,
    )
    return _signed_token(token, config)


async def revoke_session(redis, cookie_value: str, config: Settings) -> None:
    token = _verified_token(cookie_value, config)
    if token:
        await redis.delete(_session_key(token))


async def resolve_session(redis, cookie_value: str | None, config: Settings) -> AuthUser | None:
    token = _verified_token(cookie_value, config)
    if not token:
        return None
    value = await redis.get(_session_key(token))
    if not value:
        return None
    try:
        user = json.loads(value)
        if user.get("users_version") != _configured_users_version(config):
            return None
        if user.get("role") not in VALID_ROLES or not user.get("username"):
            return None
        return AuthUser(username=user["username"], role=user["role"])
    except (TypeError, ValueError, AttributeError):
        return None


async def current_user(request: Request) -> AuthUser:
    redis = getattr(request.app.state, "redis_client", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Session service unavailable")
    user = await resolve_session(
        redis,
        request.cookies.get(request.app.state.settings.RADAR_SESSION_COOKIE),
        request.app.state.settings,
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user
