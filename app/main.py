import asyncio
import logging
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.database import db
from app.config import settings
from app.config import validate_required_settings
from app.auth import parse_users, resolve_session
from app.routers import auth, ingest, dashboard, tasks, review

limiter = Limiter(key_func=get_remote_address)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Shortmox_Enterprise")


class ExtensionConnectionManager:
    def __init__(self, send_timeout_seconds: float = 2.0):
        self.active_connections: dict[WebSocket, str] = {}
        self._reserved_by_user: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.send_timeout_seconds = send_timeout_seconds

    def connection_count(self, username: str) -> int:
        active = sum(1 for active_user in self.active_connections.values() if active_user == username)
        return active + self._reserved_by_user.get(username, 0)

    async def connect(self, websocket: WebSocket, username: str, limit: int) -> bool:
        async with self._lock:
            if self.connection_count(username) >= limit:
                return False
            self._reserved_by_user[username] = self._reserved_by_user.get(username, 0) + 1
        try:
            await websocket.accept()
            self.active_connections[websocket] = username
            return True
        finally:
            remaining = self._reserved_by_user[username] - 1
            if remaining:
                self._reserved_by_user[username] = remaining
            else:
                self._reserved_by_user.pop(username, None)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.pop(websocket, None)

    async def _send_alert(self, connection: WebSocket, message: dict):
        try:
            await asyncio.wait_for(
                connection.send_json(message), timeout=self.send_timeout_seconds
            )
        except Exception as exc:
            logger.warning("Dropping unhealthy WebSocket connection: %s", type(exc).__name__)
            self.disconnect(connection)
            try:
                await asyncio.wait_for(
                    connection.close(code=1011), timeout=self.send_timeout_seconds
                )
            except Exception:
                # A failed socket may also reject the close frame.
                pass

    async def broadcast_alert(self, message: dict):
        connections = list(self.active_connections)
        if connections:
            await asyncio.gather(
                *(self._send_alert(connection, message) for connection in connections)
            )


ws_manager = ExtensionConnectionManager()


async def redis_event_fanout(
    redis_client, subscribed: asyncio.Future[None] | None = None
):
    pubsub = None
    try:
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("radar:events:v1")
        # redis-py sends SUBSCRIBE before reading its acknowledgement. Wait for
        # the server to confirm registration before the API becomes ready.
        acknowledgement = await asyncio.wait_for(
            pubsub.get_message(ignore_subscribe_messages=False, timeout=None),
            timeout=10.0,
        )
        if (
            not isinstance(acknowledgement, dict)
            or acknowledgement.get("type") != "subscribe"
            or acknowledgement.get("channel") not in {
                "radar:events:v1", b"radar:events:v1"
            }
            or not isinstance(acknowledgement.get("data"), int)
            or acknowledgement["data"] < 1
        ):
            raise RuntimeError("Redis event subscription was not acknowledged")
        if subscribed is not None and not subscribed.done():
            subscribed.set_result(None)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                event = json.loads(message["data"])
                await ws_manager.broadcast_alert(event)
            except (TypeError, ValueError, KeyError):
                logger.warning("Invalid event received from Redis pub/sub")
    except BaseException as exc:
        if subscribed is not None and not subscribed.done():
            subscribed.set_exception(exc)
        raise
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe("radar:events:v1")
            finally:
                await pubsub.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[SYSTEM BOOT] Starting the Shortmox Enterprise CTI platform")
    validate_required_settings(settings)
    app.state.settings = settings
    app.state.auth_users = parse_users(settings)
    app.state.ready = False
    redis_client = None
    fanout_task = None

    try:
        await db.connect()
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        app.state.redis_client = redis_client
        subscribed = asyncio.get_running_loop().create_future()
        fanout_task = asyncio.create_task(redis_event_fanout(redis_client, subscribed))
        app.state.fanout_task = fanout_task
        await subscribed
        if fanout_task.done():
            await fanout_task
        app.state.ready = True
        yield
    finally:
        app.state.ready = False
        app.state.fanout_task = None
        logger.info("[SYSTEM HALT] Closing application resources")
        if fanout_task is not None:
            fanout_task.cancel()
            await asyncio.gather(fanout_task, return_exceptions=True)
        if redis_client is not None:
            await redis_client.aclose()
        await db.disconnect()


app = FastAPI(
    title="Shortmox CTI Enterprise API",
    description="X (Twitter) cyber threat and narcotics intelligence radar with STIX 2.1 support.",
    version="2.0.0",
    lifespan=lifespan
)

app.state.ready = False
app.state.fanout_task = None
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Radar-Ingest-Key"],
)


@app.middleware("http")
async def require_origin_for_cookie_mutations(request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.cookies.get(settings.RADAR_SESSION_COOKIE)
        and request.headers.get("origin") not in settings.allowed_origins
    ):
        return JSONResponse(status_code=403, content={"detail": "Untrusted origin"})
    return await call_next(request)

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(dashboard.router)
app.include_router(tasks.router)
app.include_router(review.router)


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ONLINE", "message": "Enterprise CTI API is running."}


@app.get("/health/ready", tags=["Health"])
async def readiness():
    fanout_task = app.state.fanout_task
    if not app.state.ready or db.pool is None or fanout_task is None or fanout_task.done():
        return JSONResponse(status_code=503, content={"status": "NOT_READY"})
    try:
        await app.state.redis_client.ping()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "NOT_READY"})
    return {"status": "READY"}


@app.websocket("/ws/signals")
async def websocket_signals(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if not origin or origin not in settings.allowed_origins:
        await websocket.close(code=4403)
        return

    redis_client = getattr(websocket.app.state, "redis_client", None)
    if redis_client is None:
        await websocket.close(code=1013)
        return
    user = await resolve_session(
        redis_client, websocket.cookies.get(settings.RADAR_SESSION_COOKIE), settings
    )
    if user is None or user.role not in {"reader", "analyst", "admin"}:
        await websocket.close(code=4401)
        return
    if not await ws_manager.connect(
        websocket, user.username, settings.RADAR_MAX_WS_PER_USER
    ):
        await websocket.close(code=4429)
        return

    recheck_seconds = max(0.1, settings.RADAR_WS_SESSION_RECHECK_SECONDS)
    next_recheck = asyncio.get_running_loop().time() + recheck_seconds
    try:
        while True:
            try:
                await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=max(0.0, next_recheck - asyncio.get_running_loop().time()),
                )
            except asyncio.TimeoutError:
                pass
            # The deadline does not reset when a client sends frequent frames;
            # logout and expiry checks therefore happen for both idle and active sockets.
            if asyncio.get_running_loop().time() >= next_recheck:
                renewed_user = await resolve_session(
                    redis_client,
                    websocket.cookies.get(settings.RADAR_SESSION_COOKIE),
                    settings,
                )
                if renewed_user is None or renewed_user.username != user.username:
                    await websocket.close(code=4401)
                    return
                next_recheck = asyncio.get_running_loop().time() + recheck_seconds
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)
