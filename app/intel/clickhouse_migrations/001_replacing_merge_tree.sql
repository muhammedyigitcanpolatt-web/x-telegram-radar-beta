-- Manual migration for a pre-existing cold-lake table.
-- Take and verify an external ClickHouse backup first. Run once with no archive
-- task running; the legacy table is retained under the name below.
CREATE TABLE shortmox_cold_lake.historical_threat_signals_v2
(
    id String,
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
ENGINE = ReplacingMergeTree()
ORDER BY id;

INSERT INTO shortmox_cold_lake.historical_threat_signals_v2
    (id, tweet_id, platform, channel_id, source_message_id, username,
     raw_text, admiralty_code, confidence_score, detected_at, payload_json)
SELECT
    id,
    '',
    argMax(platform, detected_at),
    '',
    '',
    argMax(username, detected_at),
    argMax(raw_text, detected_at),
    argMax(admiralty_code, detected_at),
    argMax(confidence_score, detected_at),
    max(detected_at),
    toJSONString(map(
        'id', id,
        'platform', argMax(platform, detected_at),
        'username', argMax(username, detected_at),
        'raw_text', argMax(raw_text, detected_at),
        'admiralty_code', argMax(admiralty_code, detected_at),
        'confidence_score', toString(argMax(confidence_score, detected_at)),
        'detected_at', toString(max(detected_at))
    ))
FROM shortmox_cold_lake.historical_threat_signals
GROUP BY id;

RENAME TABLE
    shortmox_cold_lake.historical_threat_signals
        TO shortmox_cold_lake.historical_threat_signals_legacy,
    shortmox_cold_lake.historical_threat_signals_v2
        TO shortmox_cold_lake.historical_threat_signals;
