# app/intel/dossier_engine.py
import uuid
import json
import logging

from app.intel.graph_db import Neo4jIntelligence
from app.database import db

logger = logging.getLogger("DossierEngine")


class AutomatedThreatProfiler:
    def __init__(self):
        self.graph = Neo4jIntelligence()

    async def run_clustering_pipeline(self):
        """
        Find indirect links in Neo4j and group accounts sharing wallets or
        weapon and visual threat indicators into dossiers.
        """
        query = """
        MATCH (a:Account)-[:TRANSACTS_WITH]->(w:Wallet)<-[:TRANSACTS_WITH]-(a2:Account)
        WHERE a.username <> a2.username
        WITH w, collect(DISTINCT a.username) as accounts, w.currency as currency, w.address as addr
        WHERE size(accounts) >= 2
        RETURN accounts, addr, currency;
        """

        async with self.graph._driver.session() as session:
            try:
                records = await session.run(query)
                async for record in records:
                    accounts_cluster = record["accounts"]
                    wallet_address = record["addr"]
                    currency = record["currency"]
                    await self._generate_or_update_dossier(accounts_cluster, wallet_address, currency)
            except Exception as e:
                logger.error(f"[DOSSIER CLUSTERING ERROR] Graph analysis failed: {e}")

    async def _generate_or_update_dossier(self, accounts: list[str], wallet: str, currency: str):
        """Combine clustered data with financial intelligence and save it."""
        async with db.pool.acquire() as conn:
            wallet_stats = await conn.fetchrow("""
                SELECT balance_usd, total_transactions FROM crypto_intelligence
                WHERE wallet_address = $1;
            """, wallet)

            budget = float(wallet_stats["balance_usd"]) if wallet_stats else 0.00

            existing_dossier = await conn.fetchrow("""
                SELECT dossier_id, codename FROM threat_actor_dossiers
                WHERE associated_wallets @> $1::jsonb;
            """, json.dumps([wallet]))

            if existing_dossier:
                await conn.execute("""
                    UPDATE threat_actor_dossiers
                    SET associated_accounts = (
                        SELECT jsonb_agg(DISTINCT x)
                        FROM jsonb_array_elements(associated_accounts || $1::jsonb) x
                    ),
                        estimated_budget_usd = estimated_budget_usd + $2
                    WHERE dossier_id = $3;
                """, json.dumps(accounts), budget, existing_dossier["dossier_id"])
                logger.info(f"🔄 [DOSSIER UPDATE] Updated {existing_dossier['codename']}.")
            else:
                dossier_id = f"TA-2026-{uuid.uuid4().hex[:4].upper()}"
                codename = f"DIGITAL_CARTEL_CELL_{uuid.uuid4().hex[:6].upper()}"
                risk_level = "CRITICAL" if budget > 100000 else "HIGH"

                await conn.execute("""
                    INSERT INTO threat_actor_dossiers
                    (dossier_id, codename, associated_accounts, associated_wallets, estimated_budget_usd, risk_level)
                    VALUES ($1, $2, $3, $4, $5, $6);
                """, dossier_id, codename, json.dumps(accounts), json.dumps([wallet]), budget, risk_level)

                logger.warning(
                    f"🚨 [DOSSIER GENERATED] New threat actor: {codename} | "
                    f"Accounts: {len(accounts)} | Budget: ${budget:,.2f} | Risk: {risk_level}"
                )
