"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Dossier } from "@/types";

interface Report {
  report_id: string;
  target_username: string;
  confidence_score: number;
  admiralty_code: string;
  pushed_to_siem: boolean;
  created_at: string;
}

export function IntelligencePanel({ mode }: { mode: "dossiers" | "siem" }) {
  const [dossiers, setDossiers] = useState<Dossier[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    const load = async () => {
      try {
        if (mode === "dossiers") {
          const result = await api.dashboard.threatActorDossiers(50);
          if (active) setDossiers(result.dossiers);
        } else {
          const result = await api.dashboard.siemPushStatus(50);
          if (active) setReports(result.reports);
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "Could not load records.");
      } finally { if (active) setLoading(false); }
    };
    void load();
    return () => { active = false; };
  }, [mode]);

  return <section className="space-y-4">
    <h2 className="text-lg font-semibold">{mode === "dossiers" ? "Threat actors" : "SIEM export status"}</h2>
    {loading && <p className="text-muted text-sm">Loading…</p>}
    {error && <p role="alert" className="text-danger text-sm">{error}</p>}
    {!loading && !error && (mode === "dossiers" ? dossiers : reports).length === 0 && <p className="text-muted text-sm">No records yet.</p>}
    {mode === "dossiers" ? dossiers.map((dossier) => <article key={dossier.dossier_id} className="rounded border border-card-border bg-card p-4 space-y-2">
      <h3 className="font-semibold">{dossier.codename}</h3>
      <p className="text-sm text-muted">Risk: {dossier.risk_level} · Status: {dossier.operational_status}</p>
      <p className="text-sm break-all">Accounts: {Array.isArray(dossier.associated_accounts) ? dossier.associated_accounts.join(", ") : String(dossier.associated_accounts)}</p>
      <p className="text-sm break-all">Wallets: {Array.isArray(dossier.associated_wallets) ? dossier.associated_wallets.join(", ") : String(dossier.associated_wallets)}</p>
    </article>) : reports.map((report) => <article key={report.report_id} className="rounded border border-card-border bg-card p-4 space-y-2">
      <h3 className="font-semibold">@{report.target_username}</h3>
      <p className="text-sm text-muted">{report.admiralty_code} · Confidence {report.confidence_score}%</p>
      <p className="text-sm">{report.pushed_to_siem ? "Sent" : "Not sent"}</p>
      <p className="text-xs text-muted">{new Date(report.created_at).toLocaleString("en-US")}</p>
    </article>)}
  </section>;
}
