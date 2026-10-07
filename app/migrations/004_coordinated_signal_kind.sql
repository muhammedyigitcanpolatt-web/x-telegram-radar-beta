-- Historical rows have no reliable detector type. Keep them untouched and
-- enforce one durable row per source and detector for newly typed signals.
ALTER TABLE coordinated_signals
    ADD COLUMN IF NOT EXISTS signal_kind VARCHAR(32);

CREATE UNIQUE INDEX IF NOT EXISTS uq_coordinated_signals_tweet_kind
ON coordinated_signals(tweet_id, signal_kind)
WHERE signal_kind IS NOT NULL;
