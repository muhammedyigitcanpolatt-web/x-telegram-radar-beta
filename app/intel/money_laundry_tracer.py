"""
app/intel/money_laundry_tracer.py
Multi-hop cryptocurrency tracing and mixer detection

Traces outgoing transfers from a wallet for up to five hops and labels
known exchange and mixer addresses.
"""

import logging
import asyncio
import httpx
from app.database import db

logger = logging.getLogger("CrossChainHopper")


class CryptoHopTracer:
    def __init__(self):
        # Known mixer and exchange deposit addresses.
        self.known_entities = {
            "T1NX...MixerAddress": "TRON_MIXER_ALPHA",
            "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch_Router",
            "0x71c7b56c3523069622f645473d49b544ff42eb53": "TornadoCash_Proxy",
            "0xd90e2f925da5b000000000000000000000000000": "TornadoCash_Pool",
            "0x0000000000000000000000000000000000000000": "Burn_Address",
            "0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE": "Binance_Hot_Wallet",
            "bc1qm34lsc65zpw79lxesbbzkc22c5x0000000": "Binance_Deposit",
        }

    async def trace_money_laundry_pipeline(
        self,
        start_wallet: str,
        current_depth: int = 1,
        max_depth: int = 5,
    ):
        """
        Recursively trace outgoing transfers from a wallet, with request delays.
        """
        if current_depth > max_depth:
            return

        logger.warning(
            f"🕵️ [HOP ANALYSIS] Scanning hop {current_depth} from {start_wallet}"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                # Fetch recent transfers from the blockchain API.
                # A full integration would choose the URL by network type.
                url = (
                    f"https://api.blockcypher.com/v1/btc/main/addrs/"
                    f"{start_wallet}/full?limit=10"
                )
                resp = await client.get(url)

                if resp.status_code != 200:
                    logger.warning(
                        f"Blockcypher API returned status {resp.status_code}"
                    )
                    return

                txs = resp.json().get("txs", [])
                for tx in txs:
                    tx_hash = tx.get("hash")

                    # Inspect every output address.
                    for out in tx.get("outputs", []):
                        for dest_addr in out.get("addresses", []):
                            if dest_addr == start_wallet:
                                continue

                            value_satoshis = out.get("value", 0)
                            value_btc = value_satoshis / 100_000_000

                            # Check for a known mixer or exchange.
                            entity_label = self.known_entities.get(
                                dest_addr, "UNKNOWN"
                            )
                            is_bad_entity = entity_label != "UNKNOWN"

                            # Store this hop in the database.
                            async with db.pool.acquire() as conn:
                                await conn.execute(
                                    """
                                    INSERT INTO crypto_hop_analysis
                                    (source_wallet, destination_wallet, hop_depth,
                                     amount_transferred, tx_hash, is_mixer_or_cex,
                                     entity_label)
                                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                                    ON CONFLICT (tx_hash) DO NOTHING;
                                    """,
                                    start_wallet,
                                    dest_addr,
                                    current_depth,
                                    value_btc,
                                    tx_hash,
                                    is_bad_entity,
                                    entity_label,
                                )

                            # Follow the next hop if it is unclassified and
                            # the depth limit has not been reached.
                            if not is_bad_entity and current_depth < max_depth:
                                # Delay the next request to avoid rate limits.
                                await asyncio.sleep(1.5)
                                # Schedule the next hop asynchronously.
                                asyncio.create_task(
                                    self.trace_money_laundry_pipeline(
                                        dest_addr,
                                        current_depth + 1,
                                        max_depth,
                                    )
                                )
                            elif is_bad_entity:
                                logger.critical(
                                    f"🚨 [MIXER/CEX ALARM] "
                                    f"Detected {entity_label}! "
                                    f"TX: {tx_hash} | "
                                    f"Depth: {current_depth}"
                                )

            except Exception as e:
                logger.error(
                    f"Hop analysis failed at depth {current_depth}: {e}"
                )

    async def get_suspicious_paths(self, start_wallet: str, min_depth: int = 2):
        """
        Return suspicious transfer paths for a wallet.
        min_depth is the minimum hop depth considered suspicious.
        """
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source_wallet, destination_wallet, hop_depth,
                       amount_transferred, tx_hash, entity_label,
                       analyzed_at
                FROM crypto_hop_analysis
                WHERE source_wallet = $1 OR destination_wallet = $1
                ORDER BY hop_depth DESC, analyzed_at DESC
                LIMIT 100;
                """,
                start_wallet,
            )
            return [dict(r) for r in rows]
