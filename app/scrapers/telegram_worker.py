"""Read-only Telegram channel collector for explicitly configured channels."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import Settings, settings
from app.scrapers.collection import (
    _make_async_redis,
    _telegram_channels,
    collection_source_status,
    queue_source_records,
)

logger = logging.getLogger("TelegramCTI")


def _build_telegram_client(config: Settings):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session = StringSession(config.TELEGRAM_SESSION_STRING or "")
    return TelegramClient(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)


def _cursor_key(channel: str) -> str:
    digest = hashlib.sha256(channel.strip().casefold().encode("utf-8")).hexdigest()[:24]
    return f"radar:collection:telegram:cursor:{digest}"


def _message_record(message: Any, channel: str, channel_id: str, username: str) -> dict[str, Any]:
    message_id = str(message.id)
    created_at = getattr(message, "date", None)
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return {
        "tweet_id": f"telegram:{channel_id}:{message_id}",
        "source_platform": "TELEGRAM",
        "source_channel_id": channel_id,
        "source_message_id": message_id,
        "user_id": channel_id,
        "username": f"tg_{username or channel.lstrip('@') or channel_id}",
        "text": str(getattr(message, "message", None) or getattr(message, "text", "") or ""),
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "media_url": None,
    }


async def collect_configured_channels(
    *,
    config: Settings = settings,
    telegram_client_factory: Callable[[Settings], Any] = _build_telegram_client,
    redis_client_factory: Callable[[str], Any] = _make_async_redis,
    http_client_factory=None,
) -> dict[str, Any]:
    """Collect recent posts from the allowlisted channels without joining them."""
    readiness = collection_source_status("telegram", config)
    if readiness["status"] != "ready":
        return readiness

    client = telegram_client_factory(config)
    redis_client = redis_client_factory(config.REDIS_URL)
    total_fetched = total_queued = total_duplicates = 0
    connected = False
    try:
        await client.connect()
        connected = True
        if not await client.is_user_authorized():
            if not config.TELEGRAM_COLLECTION_BOT_TOKEN:
                raise RuntimeError(
                    "Telegram session is not authorized; configure an existing session or collection bot token."
                )
            await client.start(bot_token=config.TELEGRAM_COLLECTION_BOT_TOKEN)

        for channel in _telegram_channels(config):
            entity = await client.get_entity(channel)
            cursor_key = _cursor_key(channel)
            last_id = await redis_client.get(cursor_key)
            limit = config.COLLECTION_MAX_TELEGRAM_MESSAGES
            if last_id:
                message_iter = client.iter_messages(
                    entity, limit=limit, min_id=int(last_id), reverse=True
                )
            else:
                # Start with a bounded recent window; later polls continue from its newest ID.
                message_iter = client.iter_messages(entity, limit=limit)

            messages = [message async for message in message_iter]
            if not messages:
                continue
            if not last_id:
                messages.reverse()

            channel_id_value = getattr(entity, "chat_id", None)
            if channel_id_value is None:
                from telethon.utils import get_peer_id

                channel_id_value = get_peer_id(entity)
            channel_id = str(channel_id_value)
            username = getattr(entity, "username", None) or getattr(entity, "title", None) or channel
            records = [
                _message_record(message, channel, channel_id, username)
                for message in messages
                if str(getattr(message, "message", None) or getattr(message, "text", "") or "").strip()
            ]
            counts = await queue_source_records(
                records,
                config=config,
                redis_client=redis_client,
                **({"http_client_factory": http_client_factory} if http_client_factory else {}),
            )
            newest_id = max((int(message.id) for message in messages), default=0)
            if newest_id:
                await redis_client.set(cursor_key, str(newest_id))
            total_fetched += len(messages)
            total_queued += counts["queued"]
            total_duplicates += counts["duplicates"]

        logger.info(
            "Telegram collection finished: fetched=%d queued=%d duplicates=%d",
            total_fetched, total_queued, total_duplicates,
        )
        return {
            "status": "completed",
            "fetched": total_fetched,
            "queued": total_queued,
            "duplicates": total_duplicates,
        }
    finally:
        try:
            if connected:
                await client.disconnect()
        finally:
            await redis_client.aclose()


async def start_telegram_hunter():
    """Compatibility entry point: perform one configured, bounded polling pass."""
    return await collect_configured_channels()
