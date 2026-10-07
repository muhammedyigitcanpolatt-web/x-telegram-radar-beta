"""Durable source identity checks shared by all hot CTI writers."""

import hashlib
import json


ARCHIVED_CTI_LOCK_ID = 0x5241444152


async def is_archived_cti_source(conn, source: dict) -> bool:
    """Acknowledge an exact archived replay; reject conflicting source-ID reuse."""
    archived = await conn.fetchrow(
        """SELECT tweet_id, source_platform, source_channel_id,
                  source_message_id, raw_text_sha256
           FROM archived_cti_source_identities WHERE tweet_id = $1""",
        source["tweet_id"],
    )
    if archived is None:
        return False
    expected = {
        "tweet_id": source["tweet_id"],
        "source_platform": source["source_platform"],
        "source_channel_id": source["source_channel_id"],
        "source_message_id": source["source_message_id"],
        "raw_text_sha256": hashlib.sha256(
            str(source.get("text", "")).encode("utf-8")
        ).hexdigest(),
    }
    if any(archived[key] != value for key, value in expected.items()):
        raise RuntimeError(
            f"Archived CTI source identity conflicts with replay: {source['tweet_id']}"
        )
    return True


async def assert_hot_cti_source_identity(conn, source: dict) -> None:
    """Reject reuse of a source ID while its original CTI row is still hot."""
    hot = await conn.fetchrow(
        """SELECT tweet_id, source_platform, source_channel_id,
                  source_message_id, raw_text
           FROM cartel_threat_signals WHERE tweet_id = $1""",
        source["tweet_id"],
    )
    if hot is None:
        return
    expected = {
        "tweet_id": source["tweet_id"],
        "source_platform": source["source_platform"],
        "source_channel_id": source["source_channel_id"],
        "source_message_id": source["source_message_id"],
        "raw_text": str(source.get("text", "")),
    }
    if any(hot[key] != value for key, value in expected.items()):
        raise RuntimeError(
            f"Hot CTI source identity conflicts with replay: {source['tweet_id']}"
        )


async def assert_raw_source_identity(conn, source: dict) -> None:
    """Keep retry and concurrent ingest data tied to the original raw source."""
    raw = await conn.fetchrow(
        """SELECT tweet_id, source_platform, source_channel_id,
                  source_message_id, raw_json
           FROM raw_tweets WHERE tweet_id = $1""",
        source["tweet_id"],
    )
    if raw is None:
        return
    original = raw["raw_json"]
    if isinstance(original, str):
        try:
            original = json.loads(original)
        except ValueError as exc:
            raise RuntimeError(
                f"Raw source payload is invalid: {source['tweet_id']}"
            ) from exc
    if not isinstance(original, dict):
        raise RuntimeError(f"Raw source payload is invalid: {source['tweet_id']}")
    expected = {
        "tweet_id": source["tweet_id"],
        "source_platform": source["source_platform"],
        "source_channel_id": source["source_channel_id"],
        "source_message_id": source["source_message_id"],
    }
    if (
        any(raw[key] != value for key, value in expected.items())
        or str(original.get("text", "")) != str(source.get("text", ""))
    ):
        raise RuntimeError(
            f"Raw source identity conflicts with replay: {source['tweet_id']}"
        )
