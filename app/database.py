import asyncpg
from app.archived_cti_identity import (
    ARCHIVED_CTI_LOCK_ID,
    assert_hot_cti_source_identity,
    is_archived_cti_source,
)
from app.config import settings, validate_required_settings
from app.source_identity import normalize_source_record

class DatabaseManager:
    def __init__(self):
        self.pool = None
        self._vector_bootstrap_pending = []

    async def connect(self):
        if self.pool is None:
            validate_required_settings(settings)
            pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=1,
                max_size=10,
                setup=self._setup_connection,
            )
            self.pool = pool
            try:
                await self.init_db()
            except BaseException:
                await self.disconnect()
                raise

    async def disconnect(self):
        pool, self.pool = self.pool, None
        self._vector_bootstrap_pending.clear()
        if pool is not None:
            await pool.close()

    async def _setup_connection(self, conn):
        """Register pgvector on new connections, including first-run bootstrap."""
        if not await self._register_vector(conn, allow_missing_extension=True):
            self._vector_bootstrap_pending.append(conn)

    @staticmethod
    async def _register_vector(conn, allow_missing_extension=False):
        try:
            from pgvector.asyncpg import register_vector
            await register_vector(conn)
        except asyncpg.UndefinedObjectError:
            if not allow_missing_extension:
                raise
            return False
        except ValueError as exc:
            # pgvector 0.4+ reports a not-yet-installed type as ValueError rather
            # than asyncpg.UndefinedObjectError. Only suppress that exact bootstrap
            # condition; codec and connection errors must still fail startup.
            if not allow_missing_extension or str(exc) != "unknown type: public.vector":
                raise
            return False
        return True

    async def init_db(self):
        """Apply ordered SQL migrations once; migration files are the schema source of truth."""
        from pathlib import Path

        migration_dir = Path(__file__).with_name("migrations")
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(128) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            lock_id = 0x52414441524D
            await conn.execute("SELECT pg_advisory_lock($1)", lock_id)
            try:
                for migration_path in sorted(migration_dir.glob("*.sql")):
                    version = migration_path.stem
                    applied = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = $1)",
                        version,
                    )
                    if applied:
                        continue
                    migration_sql = migration_path.read_text(encoding="utf-8")
                    async with conn.transaction():
                        await conn.execute(migration_sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations(version) VALUES($1)", version
                        )
                # Pool setup runs before migrations on a fresh database, when the
                # vector type does not exist yet. Install its codec on those same
                # connections after migration 001 created the extension.
                for pending_conn in self._vector_bootstrap_pending:
                    await self._register_vector(pending_conn)
                self._vector_bootstrap_pending.clear()
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)


class RadarRepository:
    def __init__(self, pg_pool):
        self.pool = pg_pool

    async def get_top_bot_suspects(self, limit: int = 20):
        """
        List low-trust accounts with the highest suspicious signal volume.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT
                    username,
                    trust_score,
                    last_analyzed
                FROM target_users
                WHERE trust_score < 50
                ORDER BY trust_score ASC
                LIMIT $1;
            """, limit)

    async def find_coordinated_campaigns(self, min_cluster_size: int = 5):
        """
        Group coordinated campaigns by shared text hash and signal volume.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT
                    text_hash,
                    COUNT(tweet_id) as total_tweets,
                    MAX(detected_at) as last_seen
                FROM coordinated_signals
                GROUP BY text_hash
                HAVING COUNT(tweet_id) >= $1
                ORDER BY total_tweets DESC;
            """, min_cluster_size)

    async def save_cartel_threat(self, tweet_id: str, username: str, raw_text: str,
                                  text_hash: str, threat_category: str,
                                  location_context: str, confidence_score: int,
                                  source_platform: str = "X_TWITTER",
                                  source_channel_id: str = "",
                                  source_message_id: str | None = None):
        """Save a cartel CTI signal using parameterized queries."""
        source_message_id = source_message_id or tweet_id
        source = normalize_source_record({
            "tweet_id": tweet_id,
            "source_platform": source_platform,
            "source_channel_id": source_channel_id,
            "source_message_id": source_message_id,
            "text": raw_text,
        })
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", ARCHIVED_CTI_LOCK_ID)
            try:
                if await is_archived_cti_source(conn, source):
                    return
                await assert_hot_cti_source_identity(conn, source)
                await conn.execute("""
                    INSERT INTO cartel_threat_signals
                    (tweet_id, source_platform, source_channel_id, source_message_id,
                     username, raw_text, text_hash, threat_category, location_context,
                     confidence_score, detected_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                    ON CONFLICT (tweet_id) DO NOTHING;
                """, source["tweet_id"], source["source_platform"],
                     source["source_channel_id"], source["source_message_id"],
                     username, raw_text, text_hash, threat_category, location_context,
                     confidence_score)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", ARCHIVED_CTI_LOCK_ID)

db = DatabaseManager()
