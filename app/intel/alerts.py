# app/intel/alerts.py
import logging

import httpx

from app.config import settings

logger = logging.getLogger("ZeroLatencyAlerts")


class EmergencyAlertSystem:
    def __init__(self):
        self.tg_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        self.tg_chat_id = getattr(settings, "TELEGRAM_CHAT_ID", None)

    async def broadcast_critical_intel(
        self, username: str, admiralty_code: str, threat_details: str
    ):
        """Send an immediate alert for critical A1 or B1 intelligence."""
        if not self.tg_token or not self.tg_chat_id:
            logger.warning("Telegram bot integration is disabled; alert skipped.")
            return

        message = (
            f"🚨 **CRITICAL INTELLIGENCE DETECTED** 🚨\n\n"
            f"👤 **Account:** @{username}\n"
            f"📊 **Reliability Level:** {admiralty_code}\n"
            f"🔎 **Findings:** {threat_details}\n\n"
            f"🔗 _Automatically sent by Shortmox Enterprise CTI_"
        )

        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        payload = {
            "chat_id": self.tg_chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info(
                        "📱 [ALERT] Critical threat notification sent through Telegram."
                    )
            except Exception as e:
                logger.error(f"[ALERT ERROR] Could not send message: {e}")
