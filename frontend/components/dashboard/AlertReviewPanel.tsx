"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useRadarUser } from "@/components/AuthGate";

interface PendingAlert {
  report_id: string;
  target_username: string;
  source_platform: string;
  source_channel_id: string;
  source_message_id: string;
  raw_text: string;
  threat_type: string;
  confidence_score: number;
  created_at: string;
}

export function AlertReviewPanel() {
  const user = useRadarUser();
  const [alerts, setAlerts] = useState<PendingAlert[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const canReview = user?.role === "analyst" || user?.role === "admin";

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await api.review.pendingAlerts(50);
      setAlerts(result.data);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load pending reports.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const decide = async (reportId: string, decision: "APPROVED" | "REJECTED") => {
    if (!canReview || busy !== null) return;
    setBusy(reportId);
    setError("");
    setMessage("");
    try {
      const result = await api.review.reviewAlert({ report_id: reportId, decision });
      setMessage(decision === "APPROVED"
        ? `Report approved. External submission: ${result.external_submission}.`
        : "Report rejected; no external submission was made.");
      setAlerts((previous) => previous.filter((item) => item.report_id !== reportId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the decision.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-foreground">Analyst review queue</h2>
        <p className="mt-1 text-sm text-muted">No data is sent to SIEM before approval. External export is disabled by default in this deployment.</p>
      </div>
      {error && <p role="alert" className="rounded border border-danger p-3 text-sm text-danger">{error}</p>}
      {message && <p role="status" className="rounded border border-card-border p-3 text-sm text-muted">{message}</p>}
      {loading ? (
        <div className="rounded border border-card-border bg-card p-5 text-sm text-muted">Loading pending reports…</div>
      ) : !error && alerts.length === 0 ? (
        <div className="rounded border border-card-border bg-card p-5 text-sm text-muted">No pending reports.</div>
      ) : alerts.map((alert) => (
        <article key={alert.report_id} className="rounded border border-card-border bg-card p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-semibold text-foreground">@{alert.target_username} · {alert.threat_type}</div>
            <div className="text-xs text-muted">{alert.source_platform} · {alert.confidence_score}%</div>
          </div>
          <p className="whitespace-pre-wrap text-sm text-muted">{alert.raw_text}</p>
          <div className="text-xs text-muted">Source ID: {alert.source_channel_id || "—"}/{alert.source_message_id}</div>
          {canReview && (
            <div className="flex gap-2 pt-2">
              <button disabled={busy !== null} onClick={() => void decide(alert.report_id, "APPROVED")} className="rounded bg-success px-3 py-2 text-sm text-white disabled:opacity-50">{busy === alert.report_id ? "Saving…" : "Approve"}</button>
              <button disabled={busy !== null} onClick={() => void decide(alert.report_id, "REJECTED")} className="rounded border border-danger px-3 py-2 text-sm text-danger disabled:opacity-50">Reject</button>
            </div>
          )}
        </article>
      ))}
    </section>
  );
}
