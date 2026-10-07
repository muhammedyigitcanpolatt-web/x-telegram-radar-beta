"""
app/intel/crypto_failover.py
Third-party cryptocurrency API failover pool

Uses secondary providers when a blockchain API is rate-limited or unavailable.
"""

import logging
import httpx
from typing import Optional, Dict, Any

logger = logging.getLogger("CryptoFailoverPool")


class ResilientCryptoAPIClient:
    def __init__(self):
        # Primary and backup external APIs.
        self.btc_endpoints = [
            "https://api.blockcypher.com/v1/btc/main/addrs/{address}/balance",
            "https://blockchain.info/rawaddr/{address}",
        ]
        self.trx_endpoints = [
            "https://api.trongrid.io/v1/accounts/{address}",
        ]

    async def fetch_btc_balance_with_failover(
        self, address: str
    ) -> Optional[Dict[str, Any]]:
        """
        Try backup APIs when the primary API fails.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            for index, url_template in enumerate(self.btc_endpoints):
                target_url = url_template.format(address=address)
                try:
                    logger.info(
                        f"⚙️ [CRYPTO API] Fetching data from "
                        f"provider #{index + 1}..."
                    )
                    resp = await client.get(target_url)

                    if resp.status_code == 200:
                        data = resp.json()
                        # Normalize different provider response schemas.
                        if "final_balance" in data:
                            # Blockchain.info response schema.
                            return {
                                "balance_satoshis": data["final_balance"],
                                "tx_count": data["n_tx"],
                            }
                        else:
                            # Blockcypher response schema.
                            return {
                                "balance_satoshis": data.get("balance", 0),
                                "tx_count": data.get("n_tx", 0),
                            }

                    logger.warning(
                        f"⚠️ [API POOL WARNING] Havuz #{index + 1} "
                        f"failed (status: {resp.status_code}). "
                        f"Trying the next provider..."
                    )
                except Exception as e:
                    logger.error(
                        f"Network error at provider #{index + 1}: {e}"
                    )

        # Return a safe fallback if every external provider fails.
        logger.critical(
            "☠️ [CRITICAL ALARM] All crypto intelligence API providers "
            "failed; fallback mode is active."
        )
        return {
            "balance_satoshis": 0,
            "tx_count": 0,
            "is_fallback": True,
        }

    async def fetch_trx_balance_with_failover(
        self, address: str
    ) -> Optional[Dict[str, Any]]:
        """
        Use a backup TRX API when TronGrid fails.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            for index, url_template in enumerate(self.trx_endpoints):
                target_url = url_template.format(address=address)
                try:
                    resp = await client.get(target_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("success") and data.get("data"):
                            account_data = data["data"][0]
                            return {
                                "balance_trx": account_data.get(
                                    "balance", 0
                                ),
                                "tx_count": 0,
                            }
                except Exception as e:
                    logger.error(
                        f"TRX provider #{index + 1} failed: {e}"
                    )

        return {"balance_trx": 0, "tx_count": 0, "is_fallback": True}
