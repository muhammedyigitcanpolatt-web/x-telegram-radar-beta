# app/scrapers/puppet_master.py
import logging
from datetime import datetime, timedelta

from app.database import db
from app.utils.opsec import ghost_sleep

logger = logging.getLogger("PuppetMaster")


class FleetCommander:
    """Manage the health and rotation of X account and proxy pools."""

    async def checkout_healthy_account(self) -> dict | None:
        """Return the healthy, non-quarantined account idle the longest."""
        async with db.pool.acquire() as conn:
            # Restore accounts whose cooldown has expired.
            await conn.execute("""
                UPDATE x_account_fleet
                SET status = 'ACTIVE', locked_until = NULL
                WHERE status = 'COOL_DOWN' AND locked_until < NOW()
            """)

            # Select and lock the active account with the oldest last_used_at.
            account = await conn.fetchrow("""
                SELECT id, auth_token, ct0
                FROM x_account_fleet
                WHERE status = 'ACTIVE'
                ORDER BY last_used_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED;
            """)

            if account:
                await conn.execute(
                    "UPDATE x_account_fleet SET last_used_at = NOW(), total_requests = total_requests + 1 WHERE id = $1",
                    account["id"]
                )
                return dict(account)
            else:
                logger.error(
                    "🚨 [CRITICAL] No healthy account remains available in the pool."
                )
                return None

    async def report_casualty(self, account_id: int, status_code: int):
        """Report and quarantine rate-limited or banned accounts."""
        async with db.pool.acquire() as conn:
            if status_code == 429:  # Rate Limit
                cooldown_time = datetime.utcnow() + timedelta(minutes=15)
                await conn.execute("""
                    UPDATE x_account_fleet
                    SET status = 'COOL_DOWN', locked_until = $1
                    WHERE id = $2
                """, cooldown_time, account_id)
                logger.warning(
                    f"🚑 [MEDIC] Account ID {account_id} entered a 15-minute cooldown."
                )

            elif status_code in (401, 403):  # Invalid token or banned account.
                await conn.execute(
                    "UPDATE x_account_fleet SET status = 'BANNED' WHERE id = $1", account_id
                )
                logger.error(f"💀 [KIA] Account ID {account_id} was permanently blocked (BANNED).")

    async def get_random_proxy(self) -> str | None:
        """Get a proxy from the active pool for distributed collection."""
        async with db.pool.acquire() as conn:
            proxy = await conn.fetchrow(
                "SELECT proxy_url FROM proxy_fleet WHERE status = 'ACTIVE' ORDER BY RANDOM() LIMIT 1;"
            )
            return proxy["proxy_url"] if proxy else None

    async def get_fleet_health_summary(self) -> dict:
        """Provide current account and proxy status for the dashboard."""
        async with db.pool.acquire() as conn:
            account_stats = await conn.fetch("""
                SELECT status, COUNT(*) as count, SUM(total_requests) as total_requests
                FROM x_account_fleet
                GROUP BY status;
            """)
            proxy_stats = await conn.fetch("""
                SELECT status, COUNT(*) as count FROM proxy_fleet GROUP BY status;
            """)

            return {
                "accounts": {
                    r["status"]: {"count": r["count"], "requests": r["total_requests"] or 0}
                    for r in account_stats
                },
                "proxies": {r["status"]: r["count"] for r in proxy_stats}
            }
