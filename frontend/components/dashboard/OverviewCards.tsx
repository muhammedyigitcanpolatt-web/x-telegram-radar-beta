"use client";

import { useEffect, useState } from "react";
import { Shield, Wallet, Users, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";

export function OverviewCards() {
  const [stats, setStats] = useState<{
    anomaly_detections: number;
    illicit_funds: number;
    armed_threats: number;
    detected_campaigns: number;
  } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([
      api.dashboard.enterpriseOverview(),
      api.dashboard.overview(),
    ]).then(([enterprise, overview]) => {
      if (!active) return;
      setStats({
        anomaly_detections: overview.metrics?.total_processed_signals ?? 0,
        illicit_funds: enterprise.metrics?.traced_illicit_funds_usd || 0,
        armed_threats: enterprise.metrics?.armed_visual_threats_detected || 0,
        detected_campaigns: overview.metrics?.active_manipulation_campaigns || 0,
      });
      setError("");
    }).catch((cause) => {
      if (active) setError(cause instanceof Error ? cause.message : "Could not load the overview.");
    });
    return () => { active = false; };
  }, []);

  const cards = [
    { label: "Anomaly Detections", value: stats?.anomaly_detections.toLocaleString() ?? "—", icon: Shield, color: "text-accent" },
    { label: "Tracked Illicit Funds", value: stats ? `$${stats.illicit_funds.toLocaleString()}` : "—", icon: Wallet, color: "text-warning" },
    { label: "Armed Threats", value: stats?.armed_threats.toString() ?? "—", icon: AlertTriangle, color: "text-danger" },
    { label: "Detected Campaigns", value: stats?.detected_campaigns.toString() ?? "—", icon: Users, color: "text-success" },
  ];

  return (
    <div>
      {error && <p role="alert" className="mb-2 text-xs text-danger">{error}</p>}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {cards.map((card) => {
          const Icon = card.icon;
          return (
            <div key={card.label} className="bg-card border border-card-border rounded-lg p-4 flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-card-border flex items-center justify-center">
                <Icon className={`w-5 h-5 ${card.color}`} />
              </div>
              <div>
                <div className="text-lg font-bold font-mono">{card.value}</div>
                <div className="text-[10px] text-muted uppercase tracking-wider">{card.label}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
