# app/scrapers/darkweb_stealth.py
import time
import logging

from curl_cffi import requests as curl_requests
from stem import Signal
from stem.control import Controller
from app.config import settings

logger = logging.getLogger("StealthDarkWebCTI")


class TorZeroLeakScraper:
    def __init__(self):
        self.tor_proxy = settings.TOR_SOCKS_PROXY
        self.control_port = settings.TOR_CONTROL_PORT
        self.control_password = settings.TOR_CONTROL_PASSWORD
        self.request_count = 0

    def _rotate_tor_circuit(self) -> bool:
        """Request a new Tor circuit through a configured local control port."""
        if not self.control_port or not self.control_password:
            logger.error("Tor control port or password is not configured.")
            return False
        try:
            with Controller.from_port(port=self.control_port) as controller:
                controller.authenticate(password=self.control_password)
                controller.signal(Signal.NEWNYM)
                logger.info("Tor circuit rotation requested.")
                time.sleep(3)
            return True
        except Exception as exc:
            logger.error("Tor circuit rotation failed (%s).", type(exc).__name__)
            return False

    def _assert_zero_leak(self) -> bool:
        """Confirm that a configured proxy exits through Tor before a request."""
        if not self.tor_proxy or not self.tor_proxy.startswith("socks5h://"):
            logger.error("A socks5h Tor proxy is required for this collector.")
            return False
        proxies = {"http": self.tor_proxy, "https": self.tor_proxy}
        try:
            resp = curl_requests.get(
                "https://check.torproject.org/api/ip",
                proxies=proxies,
                timeout=10
            )
            if resp.status_code == 200 and resp.json().get("IsTor") is True:
                return True
        except Exception as exc:
            logger.critical("Tor proxy verification failed (%s).", type(exc).__name__)
        return False

    async def search_onion_target(self, onion_url: str, keyword: str) -> str | None:
        """Search a configured onion target after confirming the Tor proxy."""
        self.request_count += 1

        # Request a fresh circuit every third operation.
        if self.request_count % 3 == 0 and not self._rotate_tor_circuit():
            raise RuntimeError("Tor circuit rotation failed; collection stopped.")

        # Check the proxy before the target request.
        if not self._assert_zero_leak():
            raise RuntimeError("Tor proxy verification failed; collection stopped.")

        search_url = f"{onion_url}/search?q={keyword}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept-Language": "en-US,en;q=0.5"
        }
        proxies = {"http": self.tor_proxy, "https": self.tor_proxy}

        try:
            response = curl_requests.get(
                search_url,
                headers=headers,
                proxies=proxies,
                impersonate="firefox120",
                timeout=40
            )

            # Recheck the proxy after the target request.
            if not self._assert_zero_leak():
                raise RuntimeError("Tor proxy verification failed after the request.")

            if response.status_code == 200:
                return response.text

        except Exception as exc:
            logger.error("Onion target request failed (%s).", type(exc).__name__)

        return None
