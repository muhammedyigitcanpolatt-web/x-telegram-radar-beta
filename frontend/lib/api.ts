import type { InspectorData } from "@/types";
const API_BASE = "/api/v1";

async function fetcher<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    credentials: "same-origin",
    headers,
  });
  if (!res.ok) {
    let detail = `API error: ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch { /* keep status text */ }
    if (res.status === 401 && endpoint !== "/auth/login") window.dispatchEvent(new Event("radar-session-expired"));
    throw new Error(typeof detail === "string" ? detail : "Request could not be processed.");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  auth: {
    me: () => fetcher<{ username: string; role: "reader" | "analyst" | "admin" }>("/auth/me"),
    login: (username: string, password: string) => fetcher<{ username: string; role: "reader" | "analyst" | "admin" }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
    logout: () => fetcher<void>("/auth/logout", { method: "POST" }),
  },
  dashboard: {
    inspector: (actorId: string) => fetcher<InspectorData>(`/dashboard/inspector/${encodeURIComponent(actorId)}`),
    overview: () => fetcher<{ metrics: Record<string, number> }>("/dashboard/overview"),
    enterpriseOverview: () => fetcher<{ metrics: Record<string, number> }>("/dashboard/enterprise-overview"),
    liveThreatFeed: (limit = 20) => fetcher<{ feed: any[] }>(`/dashboard/live-threat-feed?limit=${limit}`),
    cryptoIntelligence: (limit = 20) => fetcher<{ data: any[] }>(`/dashboard/crypto-intelligence?limit=${limit}`),
    geoThreatMap: (limit = 30) => fetcher<{ geo_data: any[] }>(`/dashboard/geo-threat-map?limit=${limit}`),
    lexiconOverview: (limit = 50) => fetcher<{ metrics: any; recent_discoveries: any[] }>(`/dashboard/lexicon-overview?limit=${limit}`),
    fleetStatus: () => fetcher<{ accounts: any; proxies: any }>("/dashboard/fleet-status"),
    systemHealth: () => fetcher<{ health: any }>("/dashboard/system-health"),
    threatActorDossiers: (limit = 20) => fetcher<{ dossiers: any[] }>(`/dashboard/threat-actor-dossiers?limit=${limit}`),
    admiraltyDistribution: () => fetcher<{ distribution: Record<string, number> }>("/dashboard/admiralty-distribution"),
    siemPushStatus: (limit = 20) => fetcher<{ summary: any; reports: any[] }>(`/dashboard/siem-push-status?limit=${limit}`),
    activeCampaigns: (limit = 10) => fetcher<{ campaigns: any[] }>(`/dashboard/active-campaigns?limit=${limit}`),
    telegramLinks: (limit = 50) => fetcher<{ summary: any; links: any[] }>(`/dashboard/telegram-links?limit=${limit}`),
  },
  ops: {
    templates: () => fetcher<{ templates: any[]; collection: any }>("/ops/templates"),
    triggerSweep: (query: string) => fetcher<any>(`/ops/trigger-sweep?query=${encodeURIComponent(query)}`, { method: "POST" }),
    triggerCampaign: (template: string) => fetcher<any>(`/ops/trigger-campaign?template=${encodeURIComponent(template)}`, { method: "POST" }),
    triggerTelegramSweep: () => fetcher<any>("/ops/trigger-telegram-sweep", { method: "POST" }),
    triggerLinkSeeder: (source: "x" | "osint") => fetcher<any>(`/ops/trigger-link-seeder?source=${source}`, { method: "POST" }),
    killSwitch: () => fetcher<any>("/ops/kill-switch", { method: "POST" }),
    resume: () => fetcher<any>("/ops/resume", { method: "POST" }),
  },
  review: {
    pendingLexicon: (limit = 50) => fetcher<{ data: any[] }>(`/review/pending-lexicon?limit=${limit}`),
    pendingAlerts: (limit = 20) => fetcher<{ data: any[] }>(`/review/pending-alerts?limit=${limit}`),
    reviewLexicon: (payload: { term_id: number; decision: "APPROVED" | "REJECTED"; notes?: string }) => fetcher<any>("/review/lexicon", { method: "POST", body: JSON.stringify(payload) }),
    reviewAlert: (payload: { report_id: string; decision: "APPROVED" | "REJECTED" }) => fetcher<any>("/review/alert", { method: "POST", body: JSON.stringify(payload) }),
  },
};
