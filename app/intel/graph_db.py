# app/intel/graph_db.py
import logging

from neo4j import AsyncGraphDatabase

from app.config import settings

logger = logging.getLogger("GraphIntel")


class Neo4jIntelligence:
    _driver = None  # Shared class-level driver.

    def __init__(self):
        self.uri = settings.NEO4J_URI
        self.auth = (settings.NEO4J_USER, settings.NEO4J_PASSWORD)

        if Neo4jIntelligence._driver is None:
            Neo4jIntelligence._driver = AsyncGraphDatabase.driver(self.uri, auth=self.auth)

    async def map_threat_network(
        self, username: str, wallets: list[dict], has_weapon_visual: bool
    ):
        """Link accounts, crypto wallets, and threat indicators in the graph."""
        async with self._driver.session() as session:
            try:
                # Create the primary actor node.
                await session.run(
                    "MERGE (a:Account {username: $username})",
                    username=username
                )

                # Create crypto wallet relationships.
                for wallet in wallets:
                    await session.run("""
                        MATCH (a:Account {username: $username})
                        MERGE (w:Wallet {address: $address, currency: $currency})
                        MERGE (a)-[r:TRANSACTS_WITH]->(w)
                        SET r.last_seen = timestamp()
                    """, username=username, address=wallet["address"], currency=wallet["currency"])

                # Link armed threat indicators.
                if has_weapon_visual:
                    await session.run("""
                        MATCH (a:Account {username: $username})
                        MERGE (t:ThreatIndicator {type: 'Armed_Visual'})
                        MERGE (a)-[r:EXHIBITS_THREAT]->(t)
                        SET r.last_seen = timestamp()
                    """, username=username)

                logger.info(f"🕸️ [GRAPH DB] Added @{username} to the Neo4j intelligence graph.")
            except Exception as e:
                logger.error(f"[NEO4J ERROR] Could not update graph: {e}")
