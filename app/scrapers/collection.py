"""Shared opt-in, deduplicated source collection helpers."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

import httpx
from redis.asyncio import Redis

from app.auth import ingest_service_headers
from app.config import Settings, settings
from app.scrapers.x_api import XApiError
from app.scrapers.x_client import XGraphQLError
from app.scrapers.x_source import XCollectionError, search_x_posts, selected_x_provider

logger = logging.getLogger("RadarCollection")
SEEN_TTL_SECONDS = 30 * 24 * 60 * 60


def _telegram_channels(config: Settings) -> list[str]:
    return list(dict.fromkeys(
        channel.strip()
        for channel in config.TELEGRAM_TARGET_CHANNELS.split(",")
        if channel.strip()
    ))


def _x_source_summary(config: Settings) -> dict[str, str]:
    provider = selected_x_provider(config)
    summary = {"provider": provider}
    if provider == "scraping":
        summary["credential_source"] = config.X_SCRAPING_CREDENTIAL_SOURCE.strip().lower()
        summary["proxy_source"] = config.X_PROXY_SOURCE.strip().lower()
    return summary


def collection_source_status(source: str, config: Settings = settings) -> dict[str, Any]:
    """Return safe readiness details without exposing configured credentials."""
    if not config.COLLECTION_ENABLED:
        return {
            "status": "disabled",
            "reason": "Source collection is off; set COLLECTION_ENABLED=true to enable it.",
            "missing": [],
            **(_x_source_summary(config) if source == "x" else {}),
        }

    missing: list[str] = []
    if source == "x":
        provider = selected_x_provider(config)
        if provider == "scraping":
            if not config.X_GRAPHQL_BEARER_TOKEN:
                missing.append("X_GRAPHQL_BEARER_TOKEN")
            credential_source = config.X_SCRAPING_CREDENTIAL_SOURCE.strip().lower()
            proxy_source = config.X_PROXY_SOURCE.strip().lower()
            if credential_source not in {"env", "fleet"}:
                missing.append("X_SCRAPING_CREDENTIAL_SOURCE (env or fleet)")
            elif credential_source == "env":
                if not config.X_AUTH_TOKEN:
                    missing.append("X_AUTH_TOKEN")
                if not config.X_CT0:
                    missing.append("X_CT0")
            if proxy_source not in {"env", "fleet", "none"}:
                missing.append("X_PROXY_SOURCE (env, fleet, or none)")
        elif provider == "api":
            if not config.X_BEARER_TOKEN:
                missing.append("X_BEARER_TOKEN")
        else:
            missing.append("X_COLLECTION_PROVIDER (scraping or api)")
        if not config.RADAR_INGEST_API_KEY:
            missing.append("RADAR_INGEST_API_KEY")
    elif source == "telegram":
        if config.TELEGRAM_API_ID < 1:
            missing.append("TELEGRAM_API_ID")
        if not config.TELEGRAM_API_HASH:
            missing.append("TELEGRAM_API_HASH")
        if not (config.TELEGRAM_SESSION_STRING or config.TELEGRAM_COLLECTION_BOT_TOKEN):
            missing.append("TELEGRAM_SESSION_STRING or TELEGRAM_COLLECTION_BOT_TOKEN")
        if not _telegram_channels(config):
            missing.append("TELEGRAM_TARGET_CHANNELS")
        if not config.RADAR_INGEST_API_KEY:
            missing.append("RADAR_INGEST_API_KEY")
    elif source == "osint":
        pass
    else:
        raise ValueError(f"Unknown collection source: {source}")

    if missing:
        return {
            "status": "configuration_required",
            "reason": "Collection is enabled, but required source settings are missing.",
            "missing": missing,
            **(_x_source_summary(config) if source == "x" else {}),
        }
    return {
        "status": "ready",
        "reason": None,
        "missing": [],
        **(_x_source_summary(config) if source == "x" else {}),
    }


def collection_status(config: Settings = settings) -> dict[str, Any]:
    return {
        "enabled": bool(config.COLLECTION_ENABLED),
        "sources": {
            name: collection_source_status(name, config)
            for name in ("x", "telegram", "osint")
        },
    }


def _make_async_redis(redis_url: str):
    return Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)


def _seen_key(record: dict[str, Any]) -> str:
    identity = f"{record['source_platform']}:{record['tweet_id']}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"radar:collection:seen:{digest}"


async def queue_source_records(
    records: list[dict[str, Any]],
    *,
    config: Settings = settings,
    redis_client,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> dict[str, int]:
    """Send normalized source records through the authenticated ingest API."""
    if not config.RADAR_INGEST_API_KEY:
        raise RuntimeError("RADAR_INGEST_API_KEY is required to submit collected records")
    headers = {"Content-Type": "application/json", **ingest_service_headers(config)}
    queued = duplicates = 0

    async with http_client_factory(timeout=10.0) as client:
        for record in records:
            seen_key = _seen_key(record)
            claimed = await redis_client.set(
                seen_key, "pending", ex=300, nx=True
            )
            if not claimed:
                current_state = await redis_client.get(seen_key)
                if current_state in {"done", "1"}:
                    duplicates += 1
                    continue
                if current_state == "pending":
                    raise RuntimeError("A collection task is already submitting this source record")
                # The short claim may have expired between SET NX and GET.
                claimed = await redis_client.set(seen_key, "pending", ex=300, nx=True)
                if not claimed:
                    raise RuntimeError("A collection task is already submitting this source record")
            try:
                response = await client.post(
                    config.BACKEND_INGEST_URL,
                    json=record,
                    headers=headers,
                )
                response.raise_for_status()
                await redis_client.set(seen_key, "done", ex=SEEN_TTL_SECONDS)
            except Exception:
                # A failed request, full queue, or worker crash must remain retryable.
                await redis_client.delete(seen_key)
                raise
            queued += 1

    return {"queued": queued, "duplicates": duplicates}


async def collect_x_query(
    query: str,
    *,
    config: Settings = settings,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    redis_client_factory: Callable[[str], Any] = _make_async_redis,
    graphql_client_factory=None,
) -> dict[str, Any]:
    readiness = collection_source_status("x", config)
    if readiness["status"] != "ready":
        return readiness
    if not query.strip() or len(query) > 512:
        raise ValueError("Search query must contain 1–512 characters")

    redis_client = redis_client_factory(config.REDIS_URL)
    query_key = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:24]
    provider = selected_x_provider(config)
    state_prefix = f"radar:collection:x:{provider}:{query_key}"
    cursor_key = f"{state_prefix}:since_id"
    page_token_key = f"{state_prefix}:page"
    pending_highwater_key = f"{state_prefix}:highwater"
    watermark_key = f"{state_prefix}:watermark"
    try:
        since_id = await redis_client.get(cursor_key)
        if provider == "scraping":
            since_id = await redis_client.get(watermark_key)
        page_token = await redis_client.get(page_token_key)
        pending_highwater = await redis_client.get(pending_highwater_key)
        search_kwargs = {}
        if graphql_client_factory is not None:
            search_kwargs["graphql_client_factory"] = graphql_client_factory
        search_result = await search_x_posts(
            query,
            config=config,
            max_results=config.COLLECTION_MAX_RESULTS,
            max_pages=config.COLLECTION_MAX_X_PAGES,
            since_id=since_id,
            page_token=page_token,
            http_client_factory=http_client_factory,
            **search_kwargs,
        )
        records = search_result.records
        counts = await queue_source_records(
            records,
            config=config,
            redis_client=redis_client,
            http_client_factory=http_client_factory,
        )
        fetched_highwater = max(
            (record["source_message_id"] for record in records),
            key=int,
            default=None,
        )
        if search_result.next_token:
            highwater_candidates = [value for value in (pending_highwater, fetched_highwater) if value]
            highwater = max(highwater_candidates, key=int) if highwater_candidates else None
            if highwater:
                await redis_client.set(pending_highwater_key, highwater)
            await redis_client.set(page_token_key, search_result.next_token)
        else:
            highwater_candidates = [value for value in (pending_highwater, fetched_highwater) if value]
            highwater = max(highwater_candidates, key=int) if highwater_candidates else None
            if highwater and provider == "api":
                await redis_client.set(cursor_key, highwater)
            elif highwater:
                # Private GraphQL has no compatible since_id parameter. Keep only
                # a provider-local watermark; the next pass starts at Latest and dedups.
                await redis_client.set(watermark_key, highwater)
            await redis_client.delete(page_token_key, pending_highwater_key)
        logger.info(
            "X collection finished via %s: fetched=%d queued=%d duplicates=%d",
            provider, len(records), counts["queued"], counts["duplicates"],
        )
        return {
            "status": "partial" if search_result.next_token else "completed",
            "fetched": len(records),
            "pages": search_result.pages,
            "continuation_pending": bool(search_result.next_token),
            **counts,
        }
    finally:
        await redis_client.aclose()


async def collect_campaign(
    template: str,
    *,
    config: Settings = settings,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    redis_client_factory: Callable[[str], Any] = _make_async_redis,
    graphql_client_factory=None,
) -> dict[str, Any]:
    from app.intel.queries import CampaignTemplate, get_queries_by_template

    readiness = collection_source_status("x", config)
    if readiness["status"] != "ready":
        return readiness
    try:
        campaign = CampaignTemplate(template)
    except ValueError as exc:
        raise ValueError("Unknown campaign template") from exc

    results = []
    for query in get_queries_by_template(campaign):
        try:
            result = await collect_x_query(
                query,
                config=config,
                http_client_factory=http_client_factory,
                redis_client_factory=redis_client_factory,
                graphql_client_factory=graphql_client_factory,
            )
        except Exception as exc:
            logger.warning("Campaign collection stopped after source error: %s", type(exc).__name__)
            return {
                "status": "partial" if results else "failed",
                "campaign": campaign.value,
                "completed_queries": len(results),
                "results": results,
                "error": str(exc) if isinstance(exc, (XApiError, XCollectionError, XGraphQLError, ValueError)) else "X collection failed.",
            }
        results.append(result)
    return {
        "status": "completed",
        "campaign": campaign.value,
        "completed_queries": len(results),
        "results": results,
    }
