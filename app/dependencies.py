import hmac
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request

from app.auth import AuthUser, ROLE_ADMIN, ROLE_ANALYST, current_user
from app.config import settings


def require_roles(*allowed_roles: str) -> Callable:
    async def dependency(user: AuthUser = Depends(current_user)) -> AuthUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return dependency


async def require_ingest_access(
    request: Request,
    ingest_key: str | None = Header(default=None, alias="X-Radar-Ingest-Key"),
) -> AuthUser:
    if settings.RADAR_INGEST_API_KEY and ingest_key is not None:
        if hmac.compare_digest(ingest_key, settings.RADAR_INGEST_API_KEY):
            return AuthUser(username="ingest-service", role=ROLE_ANALYST)
        raise HTTPException(status_code=403, detail="Invalid ingest credential")

    user = await current_user(request)
    if user.role not in (ROLE_ANALYST, ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="Writer role required")
    return user


require_reader = require_roles("reader", "analyst", ROLE_ADMIN)
require_analyst = require_roles(ROLE_ANALYST, ROLE_ADMIN)
require_admin = require_roles(ROLE_ADMIN)
