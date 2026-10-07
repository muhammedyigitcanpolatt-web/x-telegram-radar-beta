"""Find public Telegram invite links in recent X posts via the selected provider."""

from __future__ import annotations

import re
from typing import Any, Callable

import httpx

from app.config import Settings, settings
from app.scrapers.collection import collection_source_status
from app.scrapers.x_source import search_x_posts

SEARCH_QUERY = '("t.me/joinchat" OR "t.me/+") has:links'
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?P<kind>joinchat/|\+)(?P<code>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


class XTelegramLinkSeeder:
    def __init__(
        self,
        config: Settings = settings,
        http_client_factory: Callable[..., Any] = httpx.AsyncClient,
        graphql_client_factory=None,
    ):
        self.config = config
        self.http_client_factory = http_client_factory
        self.graphql_client_factory = graphql_client_factory

    async def scan_for_telegram_link_records(self) -> list[dict[str, str]]:
        readiness = collection_source_status("x", self.config)
        if readiness["status"] != "ready":
            raise RuntimeError(readiness["reason"] or ", ".join(readiness["missing"]))

        search_kwargs = {}
        if self.graphql_client_factory is not None:
            search_kwargs["graphql_client_factory"] = self.graphql_client_factory
        search_result = await search_x_posts(
            SEARCH_QUERY,
            config=self.config,
            max_results=self.config.COLLECTION_MAX_RESULTS,
            max_pages=self.config.COLLECTION_MAX_X_PAGES,
            http_client_factory=self.http_client_factory,
            **search_kwargs,
        )
        found: dict[str, dict[str, str]] = {}
        for post in search_result.records:
            candidates = [post.get("text", ""), *post.get("expanded_urls", [])]
            for candidate in candidates:
                for match in INVITE_RE.finditer(candidate):
                    kind = match.group("kind").lower()
                    code = match.group("code")
                    path = f"joinchat/{code}" if kind.startswith("joinchat") else f"+{code}"
                    link = f"https://t.me/{path}"
                    found[link] = {"url": link, "source_id": post["tweet_id"]}
        return list(found.values())

    async def scan_for_telegram_links(self) -> list[str]:
        """Backward-compatible URL-only result for existing integrations."""
        records = await self.scan_for_telegram_link_records()
        return [record["url"] for record in records]
