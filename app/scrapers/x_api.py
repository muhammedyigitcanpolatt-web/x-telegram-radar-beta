"""Official X API v2 client used by opt-in collection tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from app.source_identity import normalize_source_record

X_RECENT_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"


def normalize_x_query(query: str) -> str:
    """Translate template `AND` separators to the API's implicit conjunction."""
    parts = re.split(r'("(?:[^"\\]|\\.)*")', query.strip())
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\bAND\b", " ", parts[index], flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class XApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class XSearchResult:
    records: list[dict[str, Any]]
    next_token: str | None
    pages: int


async def search_recent_posts(
    query: str,
    bearer_token: str,
    *,
    max_results: int = 25,
    max_pages: int = 5,
    since_id: str | None = None,
    page_token: str | None = None,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> XSearchResult:
    """Search bounded pages of recent public X posts and preserve continuation state."""
    query = normalize_x_query(query)
    if not query or len(query) > 512:
        raise ValueError("X search query must contain 1–512 characters")
    if not bearer_token:
        raise ValueError("X_BEARER_TOKEN is required for X collection")
    if not 10 <= max_results <= 100:
        raise ValueError("COLLECTION_MAX_RESULTS must be between 10 and 100")
    if not 1 <= max_pages <= 20:
        raise ValueError("COLLECTION_MAX_X_PAGES must be between 1 and 20")

    raw_posts = []
    users: dict[str, dict[str, Any]] = {}
    token = page_token
    seen_tokens = {token} if token else set()
    page_count = 0
    async with http_client_factory(timeout=20.0) as client:
        while page_count < max_pages:
            params: dict[str, str | int] = {
                "query": query,
                "max_results": max_results,
                "tweet.fields": "author_id,created_at,entities",
                "expansions": "author_id",
                "user.fields": "username,created_at",
            }
            if since_id:
                params["since_id"] = since_id
            if token:
                params["next_token"] = token

            response = await client.get(
                X_RECENT_SEARCH_URL,
                headers={"Authorization": f"Bearer {bearer_token}"},
                params=params,
            )
            if response.status_code == 401:
                raise XApiError("X API rejected the bearer token or app authorization.", 401)
            if response.status_code == 403:
                raise XApiError("X Recent Search is not available to the configured app.", 403)
            if response.status_code == 429:
                raise XApiError("X API rate limit reached; retry after the provider window resets.", 429)
            if response.status_code >= 400:
                raise XApiError(
                    f"X API request failed with HTTP {response.status_code}.",
                    response.status_code,
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise XApiError("X API returned an unexpected response shape.")

            raw_posts.extend(payload.get("data") or [])
            includes = payload.get("includes") or {}
            if not isinstance(includes, dict):
                includes = {}
            users.update({
                str(user.get("id")): user
                for user in (includes.get("users") or [])
                if isinstance(user, dict) and user.get("id") is not None
            })
            page_count += 1
            next_token = (payload.get("meta") or {}).get("next_token")
            if not isinstance(next_token, str) or not next_token:
                token = None
                break
            if next_token in seen_tokens:
                raise XApiError("X API repeated a pagination token; cursor was not advanced.")
            seen_tokens.add(next_token)
            token = next_token

    records = []
    for post in raw_posts:
        if not isinstance(post, dict) or post.get("id") is None:
            continue
        post_id = str(post["id"])
        author_id = str(post.get("author_id") or "")
        user = users.get(author_id, {})
        username = user.get("username") or (f"x_{author_id}" if author_id else "unknown")
        entities = post.get("entities") or {}
        urls = entities.get("urls", []) if isinstance(entities, dict) else []
        expanded_urls = []
        for url in urls or []:
            if not isinstance(url, dict):
                continue
            expanded_url = url.get("unwound_url") or url.get("expanded_url") or url.get("url")
            if isinstance(expanded_url, str) and expanded_url:
                expanded_urls.append(expanded_url)
        record = normalize_source_record({
            "source_platform": "X_TWITTER",
            "source_channel_id": author_id,
            "source_message_id": post_id,
            "tweet_id": post_id,
            "user_id": author_id,
            "username": username,
            "text": str(post.get("text") or ""),
            "created_at": post.get("created_at"),
            "account_created_at": user.get("created_at"),
        })
        record["expanded_urls"] = expanded_urls
        records.append(record)
    return XSearchResult(records=records, next_token=token, pages=page_count)
