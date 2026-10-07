"use client";

import { useEffect, useState } from "react";
import { Activity, Database, Server, Clock } from "lucide-react";
import { api } from "@/lib/api";
import type { SystemHealth } from "@/types";

export function SystemHealthPanel() {
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const refresh = () => api.dashboard.systemHealth()
      .then((res) => { if (active) { setHealth(res.health); setError(""); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Could not load system health."); });
    void refresh();
    const iv = setInterval(() => void refresh(), 10000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  if (!health) return <div role={error ? "alert" : undefined} className="text-muted text-xs">{error || "Loading..."}</div>;

  const items = [
    { label: "Redis Queue", value: health.redis_queue_length, status: health.redis_queue_length > 50000 ? "danger" : health.redis_queue_length > 10000 ? "warning" : "success", icon: Server },
    { label: "PostgreSQL", value: health.database_connected ? "CONNECTED" : "DISCONNECTED", status: health.database_connected ? "success" : "danger", icon: Database },
    { label: "API Status", value: health.api_status, status: health.api_status === "ONLINE" ? "success" : "danger", icon: Activity },
  ];

  return (
    <div className="h-full flex flex-col">
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <h3 className="text-sm font-semibold text-accent uppercase tracking-wider mb-3 flex items-center gap-2">
        <Clock className="w-4 h-4" />
        System Health
      </h3>

      <div className="space-y-3">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <div key={item.label} className="flex items-center justify-between p-3 bg-card-border/30 rounded-lg border border-card-border">
              <div className="flex items-center gap-3">
                <Icon className={`w-4 h-4 ${item.status === "success" ? "text-success" : item.status === "warning" ? "text-warning" : "text-danger"}`} />
                <span className="text-xs text-muted">{item.label}</span>
              </div>
              <span className={`text-xs font-mono font-bold ${item.status === "success" ? "text-success" : item.status === "warning" ? "text-warning" : "text-danger"}`}>
                {typeof item.value === "number" ? item.value.toLocaleString() : item.value}
              </span>
            </div>
          );
        })}
      </div>

      <div className="mt-3 text-[10px] text-muted font-mono">
        Last updated: {health.timestamp ? new Date(health.timestamp).toLocaleTimeString("en-US") : "—"}
      </div>
    </div>
  );
}
