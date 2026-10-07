-- A durable source identity survives removal of its hot CTI row. The archive
-- writer inserts here in the same PostgreSQL transaction as its CAS delete.
-- Existing ClickHouse-only rows need a separately verified backfill before
-- this ledger can be used as the sole historical replay check.
CREATE TABLE archived_cti_source_identities (
    tweet_id TEXT PRIMARY KEY CHECK (tweet_id <> ''),
    signal_id INTEGER UNIQUE NOT NULL CHECK (signal_id > 0),
    source_platform TEXT NOT NULL CHECK (source_platform <> ''),
    source_channel_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL CHECK (source_message_id <> ''),
    raw_text_sha256 CHAR(64) NOT NULL
        CHECK (raw_text_sha256 ~ '^[0-9a-f]{64}$'),
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
