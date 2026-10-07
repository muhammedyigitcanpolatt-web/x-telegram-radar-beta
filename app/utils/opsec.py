# app/utils/opsec.py
import asyncio
import logging
import random
import re

logger = logging.getLogger("GhostProtocol")


def obfuscate_intel(text: str) -> str:
    """Mask sensitive intelligence data in logs."""
    # Mask Tron wallets: T1abc...xyz.
    text = re.sub(
        r"(T[A-Za-z1-9]{5})([A-Za-z1-9]{20,})([A-Za-z1-9]{4})",
        r"\1...\3",
        text
    )
    # Mask Bitcoin wallets.
    text = re.sub(
        r"\b(1[a-zA-Z0-9]{5})[a-zA-Z0-9]{20,}([a-zA-Z0-9]{4})\b",
        r"\1...\2",
        text
    )
    # Hide authentication tokens.
    text = re.sub(r"auth_token=[a-zA-Z0-9]+", "auth_token=***", text)
    text = re.sub(r"ct0=[a-zA-Z0-9]+", "ct0=***", text)
    return text


def secure_headers_cleanup(headers: dict) -> None:
    """Clear sensitive request headers from memory after use."""
    try:
        headers.pop("Authorization", None)
        headers.pop("Cookie", None)
        headers.pop("X-Csrf-Token", None)
    except Exception:
        pass


async def ghost_sleep(base_seconds: int = 20):
    """
    Add a randomized delay using a log-normal distribution.
    """
    mu = 2.0
    sigma = 0.8
    sleep_time = random.lognormvariate(mu, sigma) * (base_seconds / 10)
    sleep_time = max(5.0, min(sleep_time, 120.0))
    logger.debug(f"🤫 [OPSEC] Waiting {sleep_time:.2f} seconds.")
    await asyncio.sleep(sleep_time)
