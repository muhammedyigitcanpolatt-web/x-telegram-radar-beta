# app/scrapers/darkweb_scraper.py
import logging

from curl_cffi import requests as curl_requests

logger = logging.getLogger("DarkWebIntel")


class DarkWebOnionScraper:
    def __init__(self):
        # Use socks5h to resolve DNS through the Tor SOCKS5 proxy.
        self.tor_proxy = "socks5h://127.0.0.1:9050"
        self.proxies = {"http": self.tor_proxy, "https": self.tor_proxy}

    def search_onion_forum(self, onion_url: str, target_wallet: str) -> bool:
        """
        Search the specified .onion marketplace for a crypto wallet.
        """
        logger.info(f"🕵️ [DARKWEB SEARCH] Searching through Tor: {onion_url}")

        search_endpoint = f"{onion_url}/search?q={target_wallet}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        try:
            response = curl_requests.get(
                search_endpoint,
                headers=headers,
                proxies=self.proxies,
                impersonate="firefox120",
                timeout=30
            )

            if response.status_code == 200 and target_wallet in response.text:
                logger.warning(f"🎯 [DARKWEB MATCH] Wallet matched in a dark web forum: {onion_url}")
                return True

        except Exception as e:
            logger.error(f"❌ [TOR ERROR] Could not access .onion site: {e}")

        return False
