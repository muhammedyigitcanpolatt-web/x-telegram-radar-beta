-- Manual migration for an existing, versionless historical_threat_signals table.
-- Apply 001_replacing_merge_tree.sql first if this is still the legacy schema.
-- Stop archive and AI writers, take and verify a ClickHouse backup, and keep
-- them stopped until the row-count/content checks below pass. New installs
-- already have the versioned table from clickhouse_init.sql; do not run this.
--
-- A rerun after a partial swap is deliberately rejected. Inspect the staging
-- and backup tables, then recover manually; do not drop either one blindly.
SELECT throwIf(
    countIf(name = 'historical_threat_signals'
            AND engine = 'ReplacingMergeTree') != 1
    OR countIf(name IN (
        'historical_threat_signals_versioned_stage',
        'historical_threat_signals_pre_version'
    )) != 0,
    'Migration 002 requires one source table and no staging or backup table'
)
FROM system.tables
WHERE database = 'shortmox_cold_lake';

SELECT throwIf(
    countIf(name = 'shortmox_cold_lake' AND engine = 'Atomic') != 1,
    'Migration 002 requires an Atomic ClickHouse database for EXCHANGE TABLES'
)
FROM system.databases;

SELECT throwIf(
    countIf(name = 'version') != 0
    OR countIf(name IN (
        'id', 'tweet_id', 'platform', 'channel_id', 'source_message_id',
        'username', 'raw_text', 'admiralty_code', 'confidence_score',
        'detected_at', 'payload_json'
    )) != 11,
    'Migration 002 requires the versionless schema from migration 001'
)
FROM system.columns
WHERE database = 'shortmox_cold_lake'
  AND table = 'historical_threat_signals';

CREATE TABLE shortmox_cold_lake.historical_threat_signals_versioned_stage
(
    id String,
    version UInt64 DEFAULT 1,
    tweet_id String DEFAULT '',
    platform LowCardinality(String),
    channel_id String DEFAULT '',
    source_message_id String DEFAULT '',
    username String,
    raw_text String,
    admiralty_code String DEFAULT 'F6',
    confidence_score UInt8,
    detected_at DateTime,
    payload_json String DEFAULT '{}'
)
ENGINE = ReplacingMergeTree(version)
ORDER BY id;

-- FINAL preserves the visible winner for each id in the old table. Versions
-- already discarded by the old merge engine cannot be recovered here.
INSERT INTO shortmox_cold_lake.historical_threat_signals_versioned_stage
    (id, version, tweet_id, platform, channel_id, source_message_id,
     username, raw_text, admiralty_code, confidence_score, detected_at,
     payload_json)
SELECT
    id, toUInt64(1), tweet_id, platform, channel_id, source_message_id,
    username, raw_text, admiralty_code, confidence_score, detected_at,
    payload_json
FROM shortmox_cold_lake.historical_threat_signals FINAL;

-- Count and content-digest equality gate the swap. Keep the independent
-- backup so that an operator can also inspect individual rows if needed.
SELECT throwIf(
    (SELECT count() FROM shortmox_cold_lake.historical_threat_signals FINAL)
    !=
    (SELECT count() FROM shortmox_cold_lake.historical_threat_signals_versioned_stage FINAL),
    'Migration 002 copy count mismatch; source table remains in place'
);

SELECT throwIf(
    (SELECT sum(cityHash64(tuple(
        id, tweet_id, platform, channel_id, source_message_id, username,
        raw_text, admiralty_code, confidence_score, detected_at, payload_json
    ))) FROM shortmox_cold_lake.historical_threat_signals FINAL)
    !=
    (SELECT sum(cityHash64(tuple(
        id, tweet_id, platform, channel_id, source_message_id, username,
        raw_text, admiralty_code, confidence_score, detected_at, payload_json
    ))) FROM shortmox_cold_lake.historical_threat_signals_versioned_stage FINAL),
    'Migration 002 copy digest mismatch; source table remains in place'
);

-- EXCHANGE is atomic for the default Atomic database engine. The old table
-- remains under the staging name until the following RENAME completes.
EXCHANGE TABLES
    shortmox_cold_lake.historical_threat_signals
    AND shortmox_cold_lake.historical_threat_signals_versioned_stage;

RENAME TABLE
    shortmox_cold_lake.historical_threat_signals_versioned_stage
    TO shortmox_cold_lake.historical_threat_signals_pre_version;
