-- ClickHouse cold data lake initial schema.
-- Run this query after ClickHouse is ready.

CREATE DATABASE IF NOT EXISTS shortmox_cold_lake;

CREATE TABLE IF NOT EXISTS shortmox_cold_lake.historical_threat_signals (
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
) ENGINE = ReplacingMergeTree(version)
ORDER BY id;
