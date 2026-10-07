"""Canonical source identity helpers shared by ingest and analysis workers."""

from __future__ import annotations

import re
from typing import Any

TELEGRAM_ID_PATTERN = re.compile(r"^telegram:(.+):([^:]+)$", re.IGNORECASE)


def normalize_source_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with platform, channel, message, and stable tweet IDs set."""
    if not isinstance(payload, dict):
        raise ValueError("Ingest payload must be an object")

    record = dict(payload)
    platform = str(record.get("source_platform") or "X_TWITTER").strip().upper()
    if platform in {"X", "TWITTER"}:
        platform = "X_TWITTER"
    if not platform or len(platform) > 32:
        raise ValueError("source_platform must be a non-empty value up to 32 characters")

    channel_value = record.get("source_channel_id") or record.get("channel_id") or ""
    channel_id = "" if channel_value is None else str(channel_value).strip()
    message_value = record.get("source_message_id")
    if message_value is None or str(message_value).strip() == "":
        message_value = record.get("tweet_id")
    message_id = "" if message_value is None else str(message_value).strip()

    if platform == "TELEGRAM":
        canonical = TELEGRAM_ID_PATTERN.match(message_id)
        if canonical:
            if not channel_id:
                channel_id = canonical.group(1)
            message_id = canonical.group(2)
        if not channel_id or not message_id:
            raise ValueError("Telegram ingest requires source_channel_id and source_message_id")
        if ":" in channel_id or ":" in message_id:
            raise ValueError("Telegram channel and message IDs cannot contain ':'")
        tweet_id = f"telegram:{channel_id}:{message_id}"
    else:
        if not message_id:
            raise ValueError("Ingest requires tweet_id or source_message_id")
        tweet_id = message_id

    record.update({
        "source_platform": platform,
        "source_channel_id": channel_id,
        "source_message_id": message_id,
        "tweet_id": tweet_id,
    })
    return record
