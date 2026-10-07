"use client";

import { useEffect, useState } from "react";
import { Eye, Shield, X } from "lucide-react";
import { api } from "@/lib/api";

import type { InspectorData } from "@/types";

export default function InspectorPanel({ actorId, onClose }: { actorId: string | null; onClose: () => void }) {
  const [data, setData] = useState<InspectorData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!actorId) { setData(null); return; }
    setLoading(true);
    let active = true;
    setData(null);
    setError("");
    api.dashboard.inspector(actorId)
      .then((res) => { if (active) setData(res); })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Could not load data."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [actorId]);

  if (!actorId) return null;

  return (
    <aside className="fixed top-0 right-0 h-full w-full max-w-[400px] bg-slate-950 border-l border-slate-800 z-50 shadow-2xl flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-slate-800">
        <h2 className="text-sm font-bold text-white flex items-center gap-2">
          <Eye className="w-4 h-4 text-accent" />
          ACTOR PROFILE
        </h2>
        <button onClick={onClose} className="text-slate-400 hover:text-white"><X className="w-4 h-4" /></button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto p-4 space-y-4">
        {error && <p role="alert" className="text-danger">{error}</p>}
        {loading && <div className="text-xs text-slate-500">Analyzing...</div>}
        {!loading && !data && <div className="text-xs text-slate-500">No data found.</div>}

        {data && (
          <>
            {/* Identity */}
            <Section title="IDENTITY & PLATFORM" icon={<Shield className="w-3 h-3" />}>
              <Field label="Account" value={`@${data.identity.username}`} />
              <Field label="Platform" value={data.identity.platform} />
              <Field label="Aliases" value={data.identity.aliases.join(", ") || "—"} />
            </Section>

            {/* Financial */}
            <Section title="FINANCIAL TRAIL" icon={<Eye className="w-3 h-3" />}>
              {data.financial.wallets.map((w) => (
                <div key={w.wallet_address} className="mb-2 p-2 bg-slate-900 rounded border border-slate-800">
                  <div className="font-mono text-xs text-emerald-400 truncate">{w.wallet_address}</div>
                  <div className="text-xs text-slate-400">{w.currency} — ${w.balance_usd.toLocaleString()}</div>
                </div>
              ))}
              <Field label="Total Volume" value={`$${data.financial.total_volume.toLocaleString()}`} />
            </Section>

            {/* Operational */}
            <Section title="OPERATIONAL ACTIVITY" icon={<Eye className="w-3 h-3" />}>
              <Field label="Category" value={data.operational.threat_category} />
              <Field label="Last Seen" value={data.operational.last_seen || "—"} />
              <div className="flex flex-wrap gap-1 mt-1">
                {data.operational.top_keywords.map((k) => (
                  <span key={k} className="text-[10px] bg-slate-800 text-slate-300 px-1.5 py-0.5 rounded">{k}</span>
                ))}
              </div>
            </Section>

            {/* Scoring */}
            <Section title="RELIABILITY RATINGS" icon={<Shield className="w-3 h-3" />}>
              <div className="grid grid-cols-2 gap-2">
                <StatBox label="Admiralty" value={data.scoring.admiralty_code} color="text-accent" />
                <StatBox label="Confidence" value={`%${data.scoring.confidence}`} color="text-emerald-400" />
                <StatBox label="Risk" value={data.scoring.risk_level} color="text-red-400" />
              </div>
            </Section>
          </>
        )}
      </div>

      <p className="p-4 border-t border-slate-800 text-xs text-slate-400">Use the Analyst Review panel to decide on reports.</p>
    </aside>
  );
}

function Section({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="border border-slate-800 rounded-lg p-3 bg-slate-900/50">
      <div className="flex items-center gap-2 text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-2">
        {icon}
        {title}
      </div>
      {children}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-xs py-0.5">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-200 font-medium">{value}</span>
    </div>
  );
}

function StatBox({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="bg-slate-900 rounded p-2 text-center border border-slate-800">
      <div className="text-[10px] text-slate-500 uppercase">{label}</div>
      <div className={`text-sm font-bold ${color}`}>{value}</div>
    </div>
  );
}
