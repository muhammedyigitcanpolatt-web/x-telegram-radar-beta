# app/intel/persona_architect.py
import os
import random
import logging
from typing import Optional

from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.channels import JoinChannelRequest

from app.config import settings
from app.database import db

logger = logging.getLogger("PersonaArchitect")


SAFE_WARMING_CHANNELS = [
    "https://t.me/ReutersScienceNews",
    "https://t.me/spotify_music_telegram",
    "https://t.me/NationalGeographic",
    "https://t.me/techcrunch",
    "https://t.me/bbcturkce",
]


class PersonaArchitectEngine:
    def __init__(self):
        self.api_id = getattr(settings, "TELEGRAM_API_ID", 123456)
        self.api_hash = getattr(settings, "TELEGRAM_API_HASH", "mock_hash")

    async def groom_new_persona(
        self,
        session_name: str,
        first_name: str,
        last_name: str,
        bio: str,
        photo_path: Optional[str] = None,
    ):
        """Update a Telegram account profile and store its persona details."""
        client = TelegramClient(session_name, self.api_id, self.api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error(f"❌ [PERSONA ERROR] Session {session_name} is not authorized.")
            return

        try:
            await client(UpdateProfileRequest(
                first_name=first_name,
                last_name=last_name,
                about=bio
            ))

            if photo_path and os.path.exists(photo_path):
                file = await client.upload_file(photo_path)
                await client(UploadProfilePhotoRequest(file=file))

            async with db.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO telegram_persona_pool
                    (session_name, first_name, last_name, bio_text, account_status)
                    VALUES ($1, $2, $3, $4, 'WARMING')
                    ON CONFLICT (session_name) DO UPDATE
                    SET first_name = $2, last_name = $3, bio_text = $4,
                        account_status = 'WARMING', last_warmed_at = NOW();
                """, session_name, first_name, last_name, bio)

            logger.info(f"🎭 [PERSONA GROOMED] {session_name} -> {first_name} {last_name}")
        except Exception as e:
            logger.error(f"Persona setup failed: {e}")
        finally:
            await client.disconnect()

    async def execute_warming_cycle(self, session_name: str):
        """Join a selected public channel and update the account's warming score."""
        client = TelegramClient(session_name, self.api_id, self.api_hash)
        await client.connect()

        try:
            target_channel = random.choice(SAFE_WARMING_CHANNELS)
            await client(JoinChannelRequest(target_channel))
            logger.info(f"🔥 [WARMING] {session_name} joined public channel: {target_channel}")

            async with db.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE telegram_persona_pool
                    SET warming_score = LEAST(warming_score + 25, 100),
                        last_warmed_at = NOW()
                    WHERE session_name = $1
                    RETURNING warming_score, account_status;
                """, session_name)

                if row and row["warming_score"] >= 100 and row["account_status"] != "READY":
                    await conn.execute("""
                        UPDATE telegram_persona_pool
                        SET account_status = 'READY'
                        WHERE session_name = $1;
                    """, session_name)
                    logger.warning(f"👑 [PERSONA READY] {session_name} reached the readiness score.")
        except Exception as e:
            logger.error(f"Account warming failed: {e}")
        finally:
            await client.disconnect()
