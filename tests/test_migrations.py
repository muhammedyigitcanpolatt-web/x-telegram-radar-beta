from pathlib import Path
import re


def test_legacy_telegram_cti_identity_is_added_to_shared_migration_map():
    sql = Path("app/migrations/002_source_identity_and_review.sql").read_text()
    orphan_map = sql.index("INSERT INTO source_identity_migration_map")
    raw_update = sql.index("UPDATE raw_tweets t")
    assert orphan_map < raw_update
    assert "FROM cartel_threat_signals c" in sql[orphan_map:raw_update]
    assert "'telegram:legacy-unknown:' || c.tweet_id::text" in sql[orphan_map:raw_update]

    for table in (
        "coordinated_signals",
        "tweet_embeddings",
        "cartel_threat_signals",
        "cartel_embeddings",
        "visual_intelligence",
        "enterprise_intel_reports",
        "crypto_intelligence",
    ):
        assert f"UPDATE {table} t" in sql
        assert "source_identity_migration_map m" in sql

    assert "UPDATE cartel_threat_signals\nSET source_platform" not in sql


def test_legacy_telegram_scraper_id_requires_consistent_channel_before_splitting():
    sql = Path("app/migrations/002_source_identity_and_review.sql").read_text()
    raw_update = sql.split("UPDATE raw_tweets\nSET ", 1)[1].split("\nWHERE source_message_id", 1)[0]
    message_expression = raw_update.split("source_message_id = COALESCE(", 1)[1]

    assert message_expression.index("NULLIF(raw_json->>'source_message_id', '')") < (
        message_expression.index("CASE WHEN tweet_id::text")
    )
    assert "tweet_id::text ~ '^tg_[0-9]+_-?[0-9]+$'" in message_expression
    assert (
        "substring(tweet_id::text FROM '^tg_[0-9]+_(-?[0-9]+)$') = user_id"
        in message_expression
    )
    assert "raw_json->>'source_channel_id' = user_id" in message_expression
    assert "raw_json->>'channel_id' = user_id" in message_expression
    assert "raw_json->>'source_platform', '')) = 'TELEGRAM'" in message_expression
    assert "left(lower(COALESCE(raw_json->>'username', '')), 3) = 'tg_'" in message_expression
    assert (
        "THEN substring(tweet_id::text FROM '^tg_([0-9]+)_-?[0-9]+$')"
        in message_expression
    )
    assert re.search(r"END,\s+tweet_id::text\s+\)$", message_expression)


def test_source_identity_collisions_are_checked_before_any_id_update():
    sql = Path("app/migrations/002_source_identity_and_review.sql").read_text()
    assert (
        sql.index("CREATE TEMP TABLE source_identity_migration_map")
        < sql.index("duplicate canonical IDs")
        < sql.index("duplicate platform/channel/message keys")
        < sql.index("UPDATE raw_tweets t")
    )


def test_siem_retry_migration_backfills_existing_delivery_state():
    sql = Path("app/migrations/003_siem_export_retry.sql").read_text()
    assert "siem_export_status VARCHAR(16) NOT NULL DEFAULT 'NOT_SENT'" in sql
    assert "CASE WHEN pushed_to_siem THEN 'SENT' ELSE 'NOT_SENT' END" in sql
    assert "review_status = 'APPROVED' AND pushed_to_siem = FALSE" in sql


def test_signal_kind_migration_preserves_legacy_rows_and_deduplicates_new_rows():
    sql = Path("app/migrations/004_coordinated_signal_kind.sql").read_text()
    assert re.search(
        r"ALTER TABLE coordinated_signals\s+ADD COLUMN IF NOT EXISTS signal_kind VARCHAR\(32\);",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS \w+\s+"
        r"ON coordinated_signals\(tweet_id, signal_kind\)\s+"
        r"WHERE signal_kind IS NOT NULL;",
        sql,
        re.IGNORECASE,
    )
    assert not re.search(
        r"\b(?:UPDATE|DELETE FROM|TRUNCATE)\s+coordinated_signals\b",
        sql,
        re.IGNORECASE,
    )


def test_cold_write_version_sequence_reserves_legacy_version_and_never_cycles():
    sql = Path("app/migrations/005_cold_write_version.sql").read_text()
    assert re.search(
        r"CREATE SEQUENCE public\.cold_write_version_seq\s+"
        r"AS BIGINT\s+START WITH 2\s+INCREMENT BY 1\s+MINVALUE 2\s+"
        r"MAXVALUE 9223372036854775807\s+CACHE 1\s+NO CYCLE;",
        sql,
        re.IGNORECASE,
    )
    assert not re.search(r"\b(?:ALTER|UPDATE|DELETE|TRUNCATE)\b", sql, re.IGNORECASE)


def test_archived_cti_identity_migration_keeps_one_source_and_signal_key():
    sql = Path("app/migrations/006_archived_cti_source_identities.sql").read_text()
    assert "CREATE TABLE archived_cti_source_identities" in sql
    assert "tweet_id TEXT PRIMARY KEY" in sql
    assert "signal_id INTEGER UNIQUE NOT NULL" in sql
    for column in ("source_platform", "source_channel_id", "source_message_id"):
        assert f"{column} TEXT NOT NULL" in sql
    assert "raw_text_sha256 CHAR(64) NOT NULL" in sql
    assert "archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert not re.search(r"\b(?:UPDATE|DELETE|TRUNCATE)\b", statements, re.IGNORECASE)


def test_cold_lake_new_install_and_manual_migration_use_versioned_replacement():
    initial = Path("app/intel/clickhouse_init.sql").read_text()
    migration = Path(
        "app/intel/clickhouse_migrations/002_versioned_replacing_merge_tree.sql"
    ).read_text()

    for sql in (initial, migration):
        assert "version UInt64 DEFAULT 1" in sql
        assert "ENGINE = ReplacingMergeTree(version)" in sql
        assert "ORDER BY id" in sql

    assert "FROM shortmox_cold_lake.historical_threat_signals FINAL" in migration
    assert "id, toUInt64(1), tweet_id" in migration
    assert "EXCHANGE TABLES" in migration
    assert "historical_threat_signals_pre_version" in migration
    assert not re.search(r"\b(?:DROP|TRUNCATE)\s+TABLE\b", migration, re.IGNORECASE)
