export interface ThreatFeedItem {
  tweet_id: string;
  username: string;
  raw_text: string;
  threat_category: string;
  confidence_score: number;
  admiralty_code: string;
  pushed_to_siem: boolean;
  detected_at: string;
}

export interface CryptoWallet {
  wallet_address: string;
  currency: string;
  associated_username: string;
  balance_usd: number;
  total_transactions: number;
  last_checked_at: string;
}

export interface GeoThreat {
  latitude?: number;
  longitude?: number;
  region: string;
  threats: number;
  max_confidence: number;
}

export interface LexiconTerm {
  id: number;
  term: string;
  category: string;
  confidence_score: number;
  review_status: string;
  discovered_at: string;
}

export interface FleetStatus {
  accounts: Record<string, { count: number; requests: number }>;
  proxies: Record<string, number>;
}

export interface SystemHealth {
  redis_queue_length: number;
  database_connected: boolean;
  api_status: string;
  timestamp: string;
}

export interface Dossier {
  dossier_id: string;
  codename: string;
  associated_accounts: string[];
  associated_wallets: string[];
  estimated_budget_usd: number;
  risk_level: string;
  operational_status: string;
  generated_at: string;
}

export interface WebSocketAlert {
  schema_version: 1;
  event: "ANOMALY_DETECTED" | "CARTEL_THREAT";
  event_id: string;
  source_platform: string;
  source_channel_id: string;
  source_message_id: string;
  tweet_id: string;
  username: string;
  category?: string;
  faction?: string;
  confidence?: number;
  score?: number;
  score_kind?: "anomaly_strength" | "confidence";
  text: string;
  reason?: string;
  detected_at: string;
}

export interface InspectorData {
  identity: { username: string; platform: string; aliases: string[] };
  financial: { wallets: { wallet_address: string; currency: string; balance_usd: number }[]; total_volume: number };
  operational: { top_keywords: string[]; threat_category: string; last_seen: string | null };
  scoring: { admiralty_code: string; confidence: number; risk_level: string };
}
