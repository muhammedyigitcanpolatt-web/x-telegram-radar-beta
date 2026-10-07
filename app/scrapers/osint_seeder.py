# app/scrapers/osint_seeder.py
"""
Open-source intelligence (OSINT) seeding engine.
Scans Pastebin and similar public text sources for Telegram invite links.
Adapted from the telegram_cti project.
"""
import re
import logging

from typing import Any, Callable

import httpx

logger = logging.getLogger("OSINT_Seeder")

TG_INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?P<kind>\+|joinchat/)(?P<code>[a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)

TARGET_SOURCES = [
    "https://pastebin.com/archive",
]

async def scan_osint_for_telegram_links(
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> list[str]:
    """Collect Telegram invite links from external OSINT sources."""
    discovered = set()

    async with http_client_factory(timeout=15.0) as client:
        for url in TARGET_SOURCES:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    for match in TG_INVITE_REGEX.finditer(resp.text):
                        kind = match.group("kind").lower()
                        code = match.group("code")
                        path = f"joinchat/{code}" if kind.startswith("joinchat") else f"+{code}"
                        discovered.add(f"https://t.me/{path}")
            except Exception as e:
                logger.debug(f"OSINT source error ({url}): {e}")

    if discovered:
        logger.warning(f"🎯 [OSINT SEEDER] Found {len(discovered)} new Telegram links.")
    return list(discovered)
