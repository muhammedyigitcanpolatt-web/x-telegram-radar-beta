                CREATE TABLE IF NOT EXISTS target_users (
                    user_id VARCHAR(64) PRIMARY KEY,
                    username VARCHAR(128) NOT NULL,
                    trust_score INT DEFAULT 100,
                    raw_user_meta JSONB DEFAULT '{}',
                    last_analyzed TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS raw_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    user_id VARCHAR(64) NOT NULL,
                    raw_json JSONB NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    captured_at TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS coordinated_signals (
                    id SERIAL PRIMARY KEY,
                    text_hash VARCHAR(64) NOT NULL,
                    tweet_id TEXT NOT NULL,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    username VARCHAR(128),
                    cluster_size INT NOT NULL,
                    raw_data JSONB DEFAULT '{}',
                    detected_at TIMESTAMP DEFAULT NOW()
                );

                -- Indexes for low-latency searches on NVMe storage
                CREATE INDEX IF NOT EXISTS idx_tweets_user_id ON raw_tweets(user_id);
                CREATE INDEX IF NOT EXISTS idx_tweets_raw_json_path ON raw_tweets USING gin (raw_json);
                CREATE INDEX IF NOT EXISTS idx_signals_hash ON coordinated_signals(text_hash);

                -- pgvector: semantic search and vector similarity support
                CREATE EXTENSION IF NOT EXISTS vector;

                CREATE TABLE IF NOT EXISTS tweet_embeddings (
                    tweet_id TEXT PRIMARY KEY,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    text_hash VARCHAR(64),
                    embedding vector(768),
                    created_at TIMESTAMP DEFAULT NOW()
                );

                -- HNSW index for fast cosine similarity searches
                CREATE INDEX IF NOT EXISTS tweet_embeddings_hnsw_idx
                ON tweet_embeddings USING hnsw (embedding vector_cosine_ops);

                -- Cartel and cyber threat intelligence signals (CTI)
                CREATE TABLE IF NOT EXISTS cartel_threat_signals (
                    signal_id SERIAL PRIMARY KEY,
                    tweet_id TEXT UNIQUE,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    username VARCHAR(128),
                    raw_text TEXT,
                    text_hash VARCHAR(64),
                    threat_category VARCHAR(64),
                    location_context VARCHAR(256),
                    confidence_score INT,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    llm_analysis JSONB,
                    visual_analysis JSONB,
                    ai_enriched BOOLEAN DEFAULT FALSE
                );

                -- Cartel-focused semantic vectors
                CREATE TABLE IF NOT EXISTS cartel_embeddings (
                    tweet_id TEXT PRIMARY KEY,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    embedding vector(768),
                    created_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS cartel_hnsw_idx
                ON cartel_embeddings USING hnsw (embedding vector_cosine_ops);

                -- Financial intelligence: cryptocurrency wallet tracking
                CREATE TABLE IF NOT EXISTS crypto_intelligence (
                    wallet_address VARCHAR(128) PRIMARY KEY,
                    currency VARCHAR(16),
                    associated_username VARCHAR(128),
                    first_seen_tweet_id TEXT,
                    balance_usd DECIMAL(15, 2) DEFAULT 0.00,
                    total_transactions INT DEFAULT 0,
                    threat_faction VARCHAR(64),
                    last_checked_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_crypto_currency ON crypto_intelligence(currency);

                -- Multimodal visual intelligence (Llava / RTX 4060)
                CREATE TABLE IF NOT EXISTS visual_intelligence (
                    tweet_id TEXT PRIMARY KEY,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    image_url TEXT,
                    has_weapons BOOLEAN DEFAULT FALSE,
                    has_narcotics BOOLEAN DEFAULT FALSE,
                    tactical_gear BOOLEAN DEFAULT FALSE,
                    detected_objects JSONB DEFAULT '{}',
                    risk_score INT DEFAULT 0,
                    analyzed_at TIMESTAMP DEFAULT NOW()
                );

                -- Enterprise STIX 2.1 / SIEM archive reports
                CREATE TABLE IF NOT EXISTS enterprise_intel_reports (
                    report_id VARCHAR(128) PRIMARY KEY,
                    tweet_id TEXT,
                    source_platform VARCHAR(32) NOT NULL DEFAULT 'X_TWITTER',
                    source_channel_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    target_username VARCHAR(128),
                    confidence_score INT,
                    admiralty_code VARCHAR(2),
                    threat_type VARCHAR(64),
                    stix_payload JSONB,
                    pushed_to_siem BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    review_status VARCHAR(32) DEFAULT 'PENDING',
                    reviewed_by VARCHAR(128),
                    reviewed_at TIMESTAMP
                );

                -- Puppet Master: X account fleet
                CREATE TABLE IF NOT EXISTS x_account_fleet (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(128),
                    auth_token TEXT NOT NULL,
                    ct0 TEXT NOT NULL,
                    status VARCHAR(32) DEFAULT 'ACTIVE',
                    total_requests INT DEFAULT 0,
                    locked_until TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT NOW()
                );

                -- Puppet Master: proxy fleet
                CREATE TABLE IF NOT EXISTS proxy_fleet (
                    id SERIAL PRIMARY KEY,
                    proxy_url TEXT UNIQUE NOT NULL,
                    country_code VARCHAR(4),
                    status VARCHAR(32) DEFAULT 'ACTIVE',
                    failure_count INT DEFAULT 0
                );

                -- Self-learning lexicon: newly discovered slang and coded terms
                CREATE TABLE IF NOT EXISTS target_lexicon (
                    id SERIAL PRIMARY KEY,
                    term TEXT UNIQUE NOT NULL,
                    category VARCHAR(64) DEFAULT 'General',
                    source VARCHAR(32) DEFAULT 'AI_DISCOVERED',
                    confidence_score INT DEFAULT 0,
                    is_active BOOLEAN DEFAULT FALSE,
                    discovered_at TIMESTAMP DEFAULT NOW(),
                    last_used_at TIMESTAMP,
                    review_status VARCHAR(32) DEFAULT 'PENDING',
                    reviewed_by VARCHAR(128),
                    review_notes TEXT
                );

                -- Analyst audit log (veto and approval decisions)
                CREATE TABLE IF NOT EXISTS analyst_audit_logs (
                    log_id SERIAL PRIMARY KEY,
                    analyst_username VARCHAR(128) NOT NULL,
                    action_type VARCHAR(64) NOT NULL,
                    target_id VARCHAR(128) NOT NULL,
                    notes TEXT,
                    timestamp TIMESTAMP DEFAULT NOW()
                );

                -- Dashboard indexes for high query volume
                CREATE INDEX IF NOT EXISTS idx_reports_confidence_status
                ON enterprise_intel_reports(confidence_score DESC, review_status);

                CREATE INDEX IF NOT EXISTS idx_lexicon_review_status
                ON target_lexicon(review_status) WHERE review_status = 'PENDING';

                -- Automated threat actor dossiers
                CREATE TABLE IF NOT EXISTS threat_actor_dossiers (
                    dossier_id VARCHAR(128) PRIMARY KEY,
                    codename VARCHAR(64) UNIQUE NOT NULL,
                    associated_accounts JSONB DEFAULT '[]',
                    associated_wallets JSONB DEFAULT '[]',
                    estimated_budget_usd DECIMAL(15, 2) DEFAULT 0.00,
                    risk_level VARCHAR(32) DEFAULT 'HIGH',
                    operational_status VARCHAR(32) DEFAULT 'ACTIVE',
                    generated_at TIMESTAMP DEFAULT NOW()
                );

                -- Discovered Telegram operation links (honeypot protection)
                CREATE TABLE IF NOT EXISTS discovered_telegram_links (
                    id SERIAL PRIMARY KEY,
                    invite_url TEXT UNIQUE NOT NULL,
                    source_platform VARCHAR(64),
                    source_context_id VARCHAR(128),
                    discovery_status VARCHAR(32) DEFAULT 'PENDING_ANALYSIS',
                    found_at TIMESTAMP DEFAULT NOW()
                );

                -- Telegram persona pool (account warming / OPSEC)
                CREATE TABLE IF NOT EXISTS telegram_persona_pool (
                    id SERIAL PRIMARY KEY,
                    session_name VARCHAR(128) UNIQUE NOT NULL,
                    first_name VARCHAR(64),
                    last_name VARCHAR(64),
                    bio_text TEXT,
                    warming_score INT DEFAULT 0,
                    account_status VARCHAR(32) DEFAULT 'NEW',
                    last_warmed_at TIMESTAMP
                );

                -- Cryptocurrency hop analysis and suspicious fund flows
                CREATE TABLE IF NOT EXISTS crypto_hop_analysis (
                    id SERIAL PRIMARY KEY,
                    source_wallet TEXT NOT NULL,
                    destination_wallet TEXT NOT NULL,
                    hop_depth INT NOT NULL,
                    amount_transferred DECIMAL(18, 8),
                    tx_hash TEXT UNIQUE NOT NULL,
                    is_mixer_or_cex BOOLEAN DEFAULT FALSE,
                    entity_label VARCHAR(128) DEFAULT 'UNKNOWN',
                    analyzed_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_crypto_hops_source
                ON crypto_hop_analysis(source_wallet);
                CREATE INDEX IF NOT EXISTS idx_crypto_hops_dest
                ON crypto_hop_analysis(destination_wallet);
                CREATE INDEX IF NOT EXISTS idx_crypto_hops_entity
                ON crypto_hop_analysis(entity_label)
                WHERE is_mixer_or_cex = TRUE;

                -- Stylometric fingerprints
                CREATE TABLE IF NOT EXISTS actor_stylometry_fingerprints (
                    username VARCHAR(128) PRIMARY KEY,
                    avg_sentence_length REAL DEFAULT 0.0,
                    punctuation_density REAL DEFAULT 0.0,
                    emoji_ratio REAL DEFAULT 0.0,
                    uppercase_ratio REAL DEFAULT 0.0,
                    slang_count INT DEFAULT 0,
                    raw_vector REAL[]
                );

                CREATE INDEX IF NOT EXISTS idx_stylometry_vector
                ON actor_stylometry_fingerprints USING gin(raw_vector);

                -- IMINT: geospatial imagery and location estimates
                CREATE TABLE IF NOT EXISTS imint_geo_analysis (
                    id SERIAL PRIMARY KEY,
                    report_id VARCHAR(128) NOT NULL,
                    image_hash VARCHAR(64) UNIQUE NOT NULL,
                    detected_terrain_features TEXT[],
                    predicted_state VARCHAR(128) DEFAULT 'Unknown',
                    predicted_latitude DECIMAL(9, 6),
                    predicted_longitude DECIMAL(9, 6),
                    confidence_score REAL DEFAULT 0.0,
                    analyzed_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_imint_report
                ON imint_geo_analysis(report_id);

                -- COMINT: audio intelligence and accent fingerprints
                CREATE TABLE IF NOT EXISTS comint_audio_profiles (
                    id SERIAL PRIMARY KEY,
                    report_id VARCHAR(128) NOT NULL,
                    audio_hash VARCHAR(64) UNIQUE NOT NULL,
                    transcript_text TEXT,
                    detected_accent VARCHAR(64),
                    speaker_voiceprint_id VARCHAR(128),
                    processed_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_comint_voiceprint
                ON comint_audio_profiles(speaker_voiceprint_id);

                -- Predictive early warnings and anomaly detection
                CREATE TABLE IF NOT EXISTS predictive_early_warnings (
                    id SERIAL PRIMARY KEY,
                    target_cluster VARCHAR(128) NOT NULL,
                    current_signal_volume INT,
                    baseline_average REAL,
                    z_score_value REAL,
                    threat_probability_percentage REAL,
                    warning_status VARCHAR(32) DEFAULT 'ACTIVE_ALERT',
                    detected_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_warnings_status
                ON predictive_early_warnings(warning_status)
                WHERE warning_status = 'ACTIVE_ALERT';

                -- Graph poisoning and honeypot quarantine
                CREATE TABLE IF NOT EXISTS graph_quarantine_box (
                    id SERIAL PRIMARY KEY,
                    actor_username VARCHAR(128) UNIQUE NOT NULL,
                    wallet_count_attempted INT,
                    incident_context TEXT,
                    isolated_at TIMESTAMP DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_quarantine_actor
                ON graph_quarantine_box(actor_username);
