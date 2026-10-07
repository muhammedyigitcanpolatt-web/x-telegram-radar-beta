"use client";

import { useEffect, useState } from "react";
import { Server, Activity, Ban, Clock } from "lucide-react";
import { api } from "@/lib/api";
import type { FleetStatus } from "@/types";

export function FleetStatusGauge() {
  const [status, setStatus] = useState<FleetStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const refresh = () => api.dashboard.fleetStatus()
      .then((res) => { if (active) { setStatus(res); setError(""); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Could not load fleet status."); });
    void refresh();
    const iv = setInterval(() => void refresh(), 30000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  if (!status) return <div role={error ? "alert" : undefined} className="text-muted text-xs">{error || "Loading..."}</div>;

  const accounts = status.accounts || {};
  const proxies = status.proxies || {};
  const totalAcc = Object.values(accounts).reduce((sum: number, a: any) => sum + (a.count || 0), 0);
  const activeAcc = accounts.ACTIVE?.count || 0;
  const bannedAcc = accounts.BANNED?.count || 0;
  const cooldownAcc = accounts.COOL_DOWN?.count || 0;

  return (
    <div className="h-full flex flex-col">
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <h3 className="text-sm font-semibold text-accent uppercase tracking-wider mb-3 flex items-center gap-2">
        <Server className="w-4 h-4" />
        Account Fleet Status
      </h3>

      <div className="grid grid-cols-2 gap-3 mb-3">
        <div className="bg-card-border/30 rounded-lg p-3 text-center border border-card-border">
          <div className="text-2xl font-bold text-success">{activeAcc}</div>
          <div className="text-[10px] text-muted uppercase">Active Accounts</div>
        </div>
        <div className="bg-card-border/30 rounded-lg p-3 text-center border border-card-border">
          <div className="text-2xl font-bold text-danger">{bannedAcc}</div>
          <div className="text-[10px] text-muted uppercase">Banned</div>
        </div>
        <div className="bg-card-border/30 rounded-lg p-3 text-center border border-card-border">
          <div className="text-2xl font-bold text-warning">{cooldownAcc}</div>
          <div className="text-[10px] text-muted uppercase">Cooldown</div>
        </div>
        <div className="bg-card-border/30 rounded-lg p-3 text-center border border-card-border">
          <div className="text-2xl font-bold text-accent">{proxies.ACTIVE || 0}</div>
          <div className="text-[10px] text-muted uppercase">Active Proxies</div>
        </div>
      </div>

      <div className="flex-1 space-y-2 overflow-auto">
        {Object.entries(accounts).map(([statusName, data]: [string, any]) => (
          <div key={statusName} className="flex items-center justify-between text-xs py-1 border-b border-card-border">
            <div className="flex items-center gap-2">
              {statusName === "ACTIVE" && <Activity className="w-3 h-3 text-success" />}
              {statusName === "BANNED" && <Ban className="w-3 h-3 text-danger" />}
              {statusName === "COOL_DOWN" && <Clock className="w-3 h-3 text-warning" />}
              <span className="text-muted">{statusName}</span>
            </div>
            <div className="flex items-center gap-3 font-mono">
              <span>{data.count} accounts</span>
              <span className="text-muted">{data.requests?.toLocaleString() || 0} req</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
