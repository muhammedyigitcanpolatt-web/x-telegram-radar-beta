# app/intel/vision.py
import asyncio
import base64
import ipaddress
import json
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings

logger = logging.getLogger("VisualIntel")

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_DEADLINE_SECONDS = 15
ALLOWED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp",
})
ALLOWED_PORTS = {"http": 80, "https": 443}


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_reserved and not address.is_multicast


def _matches_image_type(content_type: str, data: bytearray) -> bool:
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/bmp":
        return data.startswith(b"BM")
    return False


async def _lookup_addresses(host: str, port: int) -> list[str]:
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM,
        ),
        timeout=5,
    )
    return [record[4][0] for record in records]


async def _public_fetch_target(raw_url: str) -> tuple[httpx.URL, str]:
    if not isinstance(raw_url, str) or len(raw_url) > 2048 or any(
        ord(char) <= 32 or ord(char) == 127 for char in raw_url
    ):
        raise ValueError("Invalid image URL")

    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_PORTS or not parsed.hostname:
            raise ValueError("Image URL must be HTTP(S) with a host")
        if "@" in parsed.netloc:
            raise ValueError("Image URL credentials are not allowed")
        port = parsed.port
        if port is None:
            port = ALLOWED_PORTS[scheme]
        if port != ALLOWED_PORTS[scheme]:
            raise ValueError("Image URL port is not allowed")
        url = httpx.URL(raw_url)
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise ValueError("Invalid image URL") from exc

    host = url.host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = await _lookup_addresses(host, port)
    else:
        addresses = [host]

    # A hostname with any non-public answer is rejected rather than choosing a
    # different answer. The selected address is used in the actual request URL.
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise ValueError("Image URL does not resolve exclusively to public IPs")
    return url, addresses[0]


class VisualOSINTTracker:
    def __init__(self):
        self.ollama_url = f"{settings.OLLAMA_URL}/api/generate"
        # Multimodal model sized for an RTX 4060 with 8 GB of VRAM.
        self.model = "llava:7b"

    async def _download_and_encode_image(self, url: str) -> str | None:
        """Fetch a bounded public raster image into memory and encode it."""
        try:
            async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
                current_url = url
                for _ in range(MAX_REDIRECTS + 1):
                    original_url, address = await _public_fetch_target(current_url)
                    pinned_url = original_url.copy_with(host=address)
                    headers = {
                        "Host": original_url.netloc.decode("ascii"),
                        "Accept": "image/*",
                        "Accept-Encoding": "identity",
                    }
                    # Host routes the HTTP request and SNI preserves TLS certificate
                    # validation, while the connection itself targets the vetted IP.
                    extensions = {
                        "sni_hostname": original_url.raw_host.decode("ascii"),
                    }
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(5.0),
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        async with client.stream(
                            "GET", pinned_url, headers=headers, extensions=extensions,
                        ) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location:
                                    return None
                                current_url = urljoin(str(original_url), location)
                                continue
                            if response.status_code != 200:
                                return None
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if content_type not in ALLOWED_IMAGE_TYPES:
                                return None
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                return None
                            content_length = response.headers.get("content-length")
                            if content_length is not None:
                                try:
                                    if not 0 <= int(content_length) <= MAX_IMAGE_BYTES:
                                        return None
                                except ValueError:
                                    return None
                            data = bytearray()
                            async for chunk in response.aiter_raw():
                                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                                    return None
                                data.extend(chunk)
                            if data and _matches_image_type(content_type, data):
                                return base64.b64encode(data).decode("ascii")
                return None
        except (ValueError, UnicodeError, OSError, httpx.HTTPError, httpx.StreamError, TimeoutError) as exc:
            logger.warning("[IMAGE FETCH SKIPPED] %s", type(exc).__name__)
        return None

    async def analyze_image(self, image_url: str) -> dict:
        """Analyze an image with Llava and return structured threat indicators."""
        base64_image = await self._download_and_encode_image(image_url)

        default_safe_response = {
            "has_weapons": False,
            "has_narcotics": False,
            "tactical_gear": False,
            "detected_objects": [],
            "risk_score": 0
        }

        if not base64_image:
            return default_safe_response

        prompt = """
        You are a cyber threat and narcotics intelligence analyst. Examine this image.
        Are there firearms, ammunition, narcotics, masked people, tactical gear
        (vests or radios), or cartel and gang symbols?

        Respond ONLY in the following JSON format, without additional explanation:
        {
            "has_weapons": true/false,
            "has_narcotics": true/false,
            "tactical_gear": true/false,
            "detected_objects": ["weapon model", "drug type", "clothing style"],
            "risk_score": 0-100 (threat level)
        }
        """

        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "images": [base64_image]
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                response = await client.post(self.ollama_url, json=payload)
                if response.status_code == 200:
                    result = response.json().get("response", "{}")
                    return json.loads(result)
            except Exception as e:
                logger.error(f"[LLAVA ERROR] Image analysis failed: {e}")

        return default_safe_response
