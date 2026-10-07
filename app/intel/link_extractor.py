# app/intel/link_extractor.py
import re
import logging

from app.database import db

logger = logging.getLogger("LinkExtractor")


class TelegramLinkHunter:
    def __init__(self):
        # Match supported Telegram invite and channel link formats.
        self.tg_pattern = re.compile(
            r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)"
            r"/(?:\+[a-zA-Z0-9_-]+|joinchat/[a-zA-Z0-9_-]+|[a-zA-Z0-9_]{5,128})",
            re.IGNORECASE
        )

    async def sniff_and_store_links(
        self, raw_text: str, source_platform: str, source_id: str, pg_pool=None
    ) -> int:
        """Find Telegram links in text and store unseen links in the database."""
        found_links = self.tg_pattern.findall(raw_text)
        inserted_count = 0

        if not found_links:
            return 0

        pool = pg_pool or db.pool
        async with pool.acquire() as conn:
            for link in set(found_links):
                try:
                    if not link.startswith("https://"):
                        clean_link = "https://" + link.replace("http://", "")
                    else:
                        clean_link = link

                    result = await conn.execute("""
                        INSERT INTO discovered_telegram_links (invite_url, source_platform, source_context_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (invite_url) DO NOTHING;
                    """, clean_link, source_platform, source_id)

                    if "INSERT 0 1" in result:
                        inserted_count += 1
                        logger.warning(f"🎯 [LINK DISCOVERED] New Telegram channel link: {clean_link}")
                except Exception as e:
                    logger.error(f"Could not save link: {e}")

        return inserted_count
