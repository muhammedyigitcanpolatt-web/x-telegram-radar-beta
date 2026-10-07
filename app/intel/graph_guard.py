"""
app/intel/graph_guard.py
Graph density and poisoning guard

Rejects implausibly dense account-to-wallet relationships before they reach
the graph database.
"""

import logging
from app.database import db

logger = logging.getLogger("GraphGuard")


class GraphAntiPoisonFilter:
    def __init__(self, max_allowed_wallets_per_account: int = 15):
        # Maximum plausible number of unique wallets linked in one message.
        self.max_wallets = max_allowed_wallets_per_account

    async def assert_graph_sanity(
        self, username: str, wallets: list
    ) -> bool:
        """
        Check an account's data density before writing it to Neo4j.
        """
        wallet_count = len(wallets)

        # First, reject an excessive number of wallets in one message.
        if wallet_count > self.max_wallets:
            await self._quarantine_malicious_actor(
                username,
                wallet_count,
                f"Excessive wallet sharing in one message: {wallet_count} wallets.",
            )
            return False

        # Then check historical graph density.
        async with db.pool.acquire() as conn:
            historical_count = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT wallet_address)
                FROM crypto_intelligence
                WHERE associated_username = $1;
                """,
                username,
            )

            total_density = (historical_count or 0) + wallet_count
            if total_density > 30:
                await self._quarantine_malicious_actor(
                    username,
                    total_density,
                    "Suspected cumulative graph poisoning based on historical density.",
                )
                return False

        return True

    async def _quarantine_malicious_actor(
        self, username: str, count: int, context: str
    ):
        """
        Quarantine a suspicious account before it reaches the main graph.
        """
        logger.critical(
            f"⚠️ [GRAPH POISONING BLOCKED] Account @{username} "
            f"exceeded graph density limits: {count}"
        )
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO graph_quarantine_box
                (actor_username, wallet_count_attempted, incident_context)
                VALUES ($1, $2, $3)
                ON CONFLICT (actor_username) DO UPDATE
                SET wallet_count_attempted = $2,
                    incident_context = $3,
                    isolated_at = NOW();
                """,
                username,
                count,
                context,
            )
