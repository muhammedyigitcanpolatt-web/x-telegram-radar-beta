# app/intel/crypto.py
import re
import logging
import time
from typing import Dict

import httpx

logger = logging.getLogger("CryptoIntel")


class CryptoOSINTTracker:
    def __init__(self):
        # Wallet patterns for the supported public blockchains.
        self._cached_btc_price = 0.0
        self._last_price_fetch = 0
        self.patterns = {
            # Bitcoin (P2PKH, P2SH, Bech32)
            "BTC": r"\b(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-zA-HJ-NP-Z0-9]{39,59})\b",
            # Ethereum and Binance Smart Chain (ERC20/BEP20)
            "ETH": r"\b(0x[a-fA-F0-9]{40})\b",
            # Tron (TRC20)
            "TRX": r"\b(T[A-Za-z1-9]{33})\b",
            # Monero (XMR)
            "XMR": r"\b(4[0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b"
        }

    def extract_wallets(self, text: str) -> list[dict]:
        """Extract unique wallet candidates in pattern and occurrence order."""
        found_wallets = []
        seen_addresses = set()
        for currency, pattern in self.patterns.items():
            for match in re.finditer(pattern, text):
                address = match.group(0)
                if address not in seen_addresses:
                    seen_addresses.add(address)
                    found_wallets.append({"currency": currency, "address": address})
        return found_wallets

    async def _get_live_btc_price(self, client: httpx.AsyncClient) -> float:
        """Fetch the public BTC price and cache it for one hour."""
        current_time = time.time()
        if self._cached_btc_price > 0 and (current_time - self._last_price_fetch) < 3600:
            return self._cached_btc_price

        try:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                timeout=5.0
            )
            if resp.status_code == 200:
                self._cached_btc_price = float(resp.json()["price"])
                self._last_price_fetch = current_time
                return self._cached_btc_price
        except Exception as e:
            logger.warning(
                "[MARKET API ERROR] Could not fetch the current BTC price: %s", e
            )

        return self._cached_btc_price if self._cached_btc_price > 0 else 65000.0

    async def check_wallet_balance(self, currency: str, address: str) -> Dict[str, float | int]:
        """
        Query public blockchain APIs asynchronously for wallet activity.
        """
        result = {"balance_usd": 0.0, "total_tx": 0}

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                if currency == "BTC":
                    # Failover havuzlu API client (BlockCypher + Blockchain.info)
                    from app.intel.crypto_failover import ResilientCryptoAPIClient
                    failover = ResilientCryptoAPIClient()
                    btc_data = await failover.fetch_btc_balance_with_failover(
                        address
                    )
                    if btc_data:
                        result["total_tx"] = btc_data.get("tx_count", 0)
                        live_price = await self._get_live_btc_price(client)
                        result["balance_usd"] = (
                            btc_data.get("balance_satoshis", 0) / 100_000_000
                        ) * live_price

                elif currency == "TRX":
                    # TronGrid public account API
                    resp = await client.get(
                        f"https://api.trongrid.io/v1/accounts/{address}"
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("success") and data.get("data"):
                            account_data = data["data"][0]
                            result["balance_usd"] = account_data.get("balance", 0) / 1000000

                # ETH and XMR lookups are not implemented yet.

            except Exception as e:
                logger.warning(
                    "[OSINT API ERROR] Could not query %s wallet %s: %s",
                    currency,
                    address,
                    e,
                )

        return result
