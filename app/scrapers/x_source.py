"""Provider selection for bounded X collection; no automatic provider fallback."""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from app.config import Settings, settings
from app.scrapers.x_api import XApiError, XSearchResult, search_recent_posts
from app.scrapers.x_client import XGraphQLClient, XGraphQLError

logger = logging.getLogger("XCollectionSource")


class XCollectionError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def selected_x_provider(config: Settings = settings) -> str:
    return config.X_COLLECTION_PROVIDER.strip().lower()


def _graphql_error_for_status(status_code: int) -> XCollectionError:
    messages = {
        401: "X scraping credentials were rejected (HTTP 401).",
        403: "X denied the scraping request (HTTP 403).",
        429: "X rate limited the scraping request (HTTP 429).",
    }
    return XCollectionError(
        messages.get(status_code, f"X GraphQL request failed with HTTP {status_code}."),
        status_code,
    )


def _selected_scraping_sources(config: Settings) -> tuple[dict[str, Any], str | None, Any | None]:
    """Resolve one account and optional configured proxy; never rotate on a request."""
    credential_source = config.X_SCRAPING_CREDENTIAL_SOURCE.strip().lower()
    proxy_source = config.X_PROXY_SOURCE.strip().lower()
    if credential_source not in {"env", "fleet"}:
        raise XCollectionError("X_SCRAPING_CREDENTIAL_SOURCE must be 'env' or 'fleet'.")
    if proxy_source not in {"env", "fleet", "none"}:
        raise XCollectionError("X_PROXY_SOURCE must be 'env', 'fleet', or 'none'.")
    needs_commander = credential_source == "fleet" or proxy_source == "fleet"
    commander = None
    if needs_commander:
        from app.scrapers.puppet_master import FleetCommander

        commander = FleetCommander()
    if credential_source == "env":
        account = {"auth_token": config.X_AUTH_TOKEN, "ct0": config.X_CT0}
    else:
        account = None
    return {"credential_source": credential_source, "account": account}, (
        config.X_PROXY_URL if proxy_source == "env" else None
    ), commander


async def search_x_posts(
    query: str,
    *,
    config: Settings = settings,
    max_results: int = 25,
    max_pages: int = 5,
    since_id: str | None = None,
    page_token: str | None = None,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
    graphql_client_factory: Callable[..., XGraphQLClient] = XGraphQLClient,
) -> XSearchResult:
    """Call exactly the configured provider and normalize both responses equally."""
    provider = selected_x_provider(config)
    if provider == "api":
        return await search_recent_posts(
            query,
            config.X_BEARER_TOKEN,
            max_results=max_results,
            max_pages=max_pages,
            since_id=since_id,
            page_token=page_token,
            http_client_factory=http_client_factory,
        )
    if provider != "scraping":
        raise XCollectionError("X_COLLECTION_PROVIDER must be 'scraping' or 'api'.")
    if not query.strip() or len(query) > 512:
        raise ValueError("X search query must contain 1–512 characters")
    if not 10 <= max_results <= 100:
        raise ValueError("COLLECTION_MAX_RESULTS must be between 10 and 100")
    if not 1 <= max_pages <= 20:
        raise ValueError("COLLECTION_MAX_X_PAGES must be between 1 and 20")

    sources, proxy_url, commander = _selected_scraping_sources(config)
    credential_source = sources["credential_source"]
    account = sources["account"]
    if credential_source == "fleet":
        account = await commander.checkout_healthy_account()
        if not account:
            raise XCollectionError("No healthy account is currently available in x_account_fleet.")
    if config.X_PROXY_SOURCE.strip().lower() == "fleet":
        proxy_url = await commander.get_random_proxy()
    if not account.get("auth_token") or not account.get("ct0"):
        raise XCollectionError("X scraping requires both auth_token and ct0 credentials.")
    client = graphql_client_factory(
        auth_token=account["auth_token"],
        ct0=account["ct0"],
        graphql_bearer_token=config.X_GRAPHQL_BEARER_TOKEN,
        proxy_url=proxy_url or None,
    )
    records: list[dict[str, Any]] = []
    cursor = page_token
    seen_cursors = {cursor} if cursor else set()
    pages = 0
    while pages < max_pages:
        payload, status_code = await client.fetch_search_timeline(
            query, count=max_results, cursor=cursor
        )
        if status_code != 200:
            if commander is not None and credential_source == "fleet" and status_code in (401, 403, 429):
                account_id = account.get("id")
                if account_id is not None:
                    await commander.report_casualty(account_id, status_code)
            raise _graphql_error_for_status(status_code)
        try:
            page_records, next_cursor = client.parse_graphql_page(payload)
        except XGraphQLError:
            raise
        except Exception as exc:
            logger.warning("X GraphQL response parsing failed (%s).", type(exc).__name__)
            raise XGraphQLError("X GraphQL returned a malformed timeline response.") from None
        reached_watermark = False
        if since_id:
            fresh_records = []
            for record in page_records:
                try:
                    if int(record["source_message_id"]) <= int(since_id):
                        reached_watermark = True
                        break
                except (KeyError, TypeError, ValueError):
                    # Non-numeric fixture/provider IDs cannot be compared as X IDs.
                    fresh_records.append(record)
                    continue
                fresh_records.append(record)
            page_records = fresh_records
        records.extend(page_records)
        pages += 1
        if reached_watermark:
            cursor = None
            break
        if not next_cursor:
            cursor = None
            break
        if next_cursor in seen_cursors:
            raise XGraphQLError("X GraphQL repeated a pagination cursor; cursor was not advanced.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return XSearchResult(records=records, next_token=cursor, pages=pages)
