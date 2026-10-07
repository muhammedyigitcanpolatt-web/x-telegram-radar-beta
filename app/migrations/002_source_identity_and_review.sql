-- Preserve the existing records while making source IDs platform/channel scoped.
ALTER TABLE raw_tweets
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE coordinated_signals
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tweet_embeddings
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE cartel_threat_signals
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS llm_analysis JSONB,
    ADD COLUMN IF NOT EXISTS visual_analysis JSONB,
    ADD COLUMN IF NOT EXISTS ai_enriched BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cartel_embeddings
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE visual_intelligence
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE enterprise_intel_reports
    ADD COLUMN IF NOT EXISTS source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
    ADD COLUMN IF NOT EXISTS source_channel_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_message_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(128);

ALTER TABLE raw_tweets ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE coordinated_signals ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE tweet_embeddings ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE cartel_threat_signals ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE cartel_embeddings ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE crypto_intelligence ALTER COLUMN first_seen_tweet_id TYPE TEXT USING first_seen_tweet_id::text;
ALTER TABLE visual_intelligence ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;
ALTER TABLE enterprise_intel_reports ALTER COLUMN tweet_id TYPE TEXT USING tweet_id::text;

UPDATE raw_tweets
SET source_platform = upper(COALESCE(
        NULLIF(raw_json->>'source_platform', ''),
        CASE WHEN left(lower(COALESCE(raw_json->>'username', '')), 3) = 'tg_'
             THEN 'TELEGRAM' ELSE source_platform END
    )),
    source_channel_id = COALESCE(
        NULLIF(raw_json->>'source_channel_id', ''),
        NULLIF(raw_json->>'channel_id', ''),
        CASE WHEN upper(COALESCE(raw_json->>'source_platform', '')) = 'TELEGRAM'
                    OR left(lower(COALESCE(raw_json->>'username', '')), 3) = 'tg_'
             THEN user_id ELSE source_channel_id END
    ),
    source_message_id = COALESCE(
        NULLIF(raw_json->>'source_message_id', ''),
        -- The original Telegram scraper stored tg_<message>_<channel> as tweet_id.
        -- Split it only when the encoded channel agrees with its legacy user_id
        -- and any channel metadata; otherwise retain the original ID for review.
        CASE WHEN tweet_id::text ~ '^tg_[0-9]+_-?[0-9]+$'
               AND substring(tweet_id::text FROM '^tg_[0-9]+_(-?[0-9]+)$') = user_id
               AND (NULLIF(raw_json->>'source_channel_id', '') IS NULL
                    OR raw_json->>'source_channel_id' = user_id)
               AND (NULLIF(raw_json->>'channel_id', '') IS NULL
                    OR raw_json->>'channel_id' = user_id)
               AND (upper(COALESCE(raw_json->>'source_platform', '')) = 'TELEGRAM'
                    OR (COALESCE(raw_json->>'source_platform', '') = ''
                        AND left(lower(COALESCE(raw_json->>'username', '')), 3) = 'tg_'))
             THEN substring(tweet_id::text FROM '^tg_([0-9]+)_-?[0-9]+$')
        END,
        tweet_id::text
    )
WHERE source_message_id = '' OR source_channel_id = '' OR source_platform = 'X_TWITTER';

CREATE TEMP TABLE source_identity_migration_map ON COMMIT DROP AS
SELECT tweet_id::text AS old_id,
       source_platform,
       source_channel_id,
       source_message_id,
       CASE WHEN source_platform = 'TELEGRAM' AND tweet_id::text NOT LIKE 'telegram:%'
            THEN 'telegram:' || source_channel_id || ':' || source_message_id
            ELSE tweet_id::text END AS new_id
FROM raw_tweets;

-- CTI can outlive raw_tweets. Add a conservative identity for legacy Telegram
-- rows so every table that refers to the old tweet ID can be migrated together.
INSERT INTO source_identity_migration_map
    (old_id, source_platform, source_channel_id, source_message_id, new_id)
SELECT c.tweet_id::text, 'TELEGRAM', 'legacy-unknown', c.tweet_id::text,
       'telegram:legacy-unknown:' || c.tweet_id::text
FROM cartel_threat_signals c
WHERE left(lower(COALESCE(c.username, '')), 3) = 'tg_'
  AND c.tweet_id NOT LIKE 'telegram:%'
  AND NOT EXISTS (
      SELECT 1 FROM source_identity_migration_map m
      WHERE m.old_id = c.tweet_id::text
  );

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM source_identity_migration_map
        GROUP BY new_id HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'Source identity migration found duplicate canonical IDs; no rows were changed';
    END IF;
    IF EXISTS (
        SELECT 1 FROM source_identity_migration_map
        GROUP BY source_platform, source_channel_id, source_message_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'Source identity migration found duplicate platform/channel/message keys; no rows were changed';
    END IF;
END $$;

UPDATE raw_tweets t
SET tweet_id = m.new_id,
    source_platform = m.source_platform,
    source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m
WHERE t.tweet_id = m.old_id AND t.tweet_id <> m.new_id;

UPDATE coordinated_signals t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE tweet_embeddings t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE cartel_threat_signals t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE cartel_embeddings t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE visual_intelligence t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE enterprise_intel_reports t SET tweet_id = m.new_id,
    source_platform = m.source_platform, source_channel_id = m.source_channel_id,
    source_message_id = m.source_message_id
FROM source_identity_migration_map m WHERE t.tweet_id = m.old_id;
UPDATE crypto_intelligence t SET first_seen_tweet_id = m.new_id
FROM source_identity_migration_map m WHERE t.first_seen_tweet_id = m.old_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_tweets_source_identity
ON raw_tweets(source_platform, source_channel_id, source_message_id);

ALTER TABLE target_lexicon ALTER COLUMN is_active SET DEFAULT FALSE;
UPDATE target_lexicon SET is_active = FALSE WHERE review_status <> 'APPROVED';

UPDATE enterprise_intel_reports SET pushed_to_siem = FALSE
WHERE review_status = 'PENDING';
