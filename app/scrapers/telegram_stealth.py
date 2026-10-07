# app/scrapers/telegram_stealth.py
import asyncio
import random
import logging

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SendReadHistoryRequest
from app.config import settings
from app.auth import ingest_service_headers

logger = logging.getLogger("StealthTelegramCTI")


class TelegramGhostScraper:
    def __init__(self, session_name: str, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name

        # OPSEC: Kurumsal MTProto Proxy (opsiyonel)
        self.proxy = (
            settings.MTPROTO_HOST,
            settings.MTPROTO_PORT,
            settings.MTPROTO_SECRET
        ) if hasattr(settings, "MTPROTO_HOST") and settings.MTPROTO_HOST else None

        self.client = TelegramClient(
            self.session_name,
            self.api_id,
            self.api_hash,
            proxy=self.proxy,
            connection_retries=5,
            retry_delay=10
        )

    async def simulate_human_behavior(self, chat_entity, message_id: int):
        """
        Add a delay before marking a channel message as read.
        """
        view_delay = random.lognormvariate(1.5, 0.5)
        await asyncio.sleep(view_delay)

        try:
            await self.client(SendReadHistoryRequest(
                peer=chat_entity,
                max_id=message_id
            ))
            logger.debug(f"🤫 [TG OPSEC] Marked message ID {message_id} as read after a delay.")
        except FloodWaitError as e:
            logger.warning(f"⚠️ [TG FLOOD] Telegram requested a {e.seconds}-second pause.")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"[TG ERROR] Message interaction failed: {e}")

    async def start_stealth_hunting(self, target_channels: list[str], backend_ingest_url: str):
        """Start listening to target channels."""
        await self.client.start()
        logger.info("🛡️ [GHOST MODE] Telegram channel listener started.")

        @self.client.on(events.NewMessage(chats=target_channels))
        async def incoming_message_handler(event):
            raw_text = event.raw_text
            chat = await event.get_input_chat()
            sender = await event.get_sender()
            username = getattr(sender, "username", "Private_Actor")

            # Run delayed interactions in the background without blocking ingestion.
            asyncio.create_task(self.simulate_human_behavior(chat, event.id))

            payload = {
                "tweet_id": f"telegram:{event.chat_id}:{event.id}",
                "source_platform": "TELEGRAM",
                "source_channel_id": str(event.chat_id),
                "source_message_id": str(event.id),
                "user_id": str(event.chat_id),
                "username": f"tg_{username}",
                "text": raw_text,
                "created_at": event.date.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "media_url": None
            }

            import httpx
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                try:
                    headers = ingest_service_headers(settings)
                    await http_client.post(backend_ingest_url, json=payload, headers=headers)
                except Exception as e:
                    logger.error(f"❌ [TG PIPELINE ERROR] Could not ingest message: {e}")

        await self.client.run_until_disconnected()
