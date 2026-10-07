"use client";

import { useEffect, useState } from "react";
import { Brain, CheckCircle, XCircle, Clock } from "lucide-react";
import { api } from "@/lib/api";
import type { LexiconTerm } from "@/types";
import { useRadarUser } from "@/components/AuthGate";

export function LexiconReviewPanel() {
  const user = useRadarUser();
  const canReview = user?.role === "analyst" || user?.role === "admin";
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [terms, setTerms] = useState<LexiconTerm[]>([]);
  const [metrics, setMetrics] = useState({ waiting: 0, approved: 0 });

  useEffect(() => {
    api.dashboard.lexiconOverview(20).then((res) => {
      setTerms(res.recent_discoveries);
      setMetrics({
        waiting: res.metrics.waiting_analyst_approval,
        approved: res.metrics.active_in_production,
      });
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "Could not load the lexicon."));
  }, []);

  const handleReview = async (termId: number, decision: "APPROVED" | "REJECTED") => {
    if (!canReview || busy !== null) return;
    setBusy(termId);
    setError("");
    try {
      await api.review.reviewLexicon({ term_id: termId, decision });
      setTerms((prev) => prev.map((term) => term.id === termId ? { ...term, review_status: decision } : term));
      setMetrics((prev) => ({ waiting: Math.max(0, prev.waiting - 1), approved: prev.approved + (decision === "APPROVED" ? 1 : 0) }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the decision.");
    } finally { setBusy(null); }

  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-accent uppercase tracking-wider flex items-center gap-2">
          <Brain className="w-4 h-4" />
          AI Lexicon Review
        </h3>
        <div className="flex gap-3 text-xs">
          <span className="flex items-center gap-1 text-warning">
            <Clock className="w-3 h-3" /> {metrics.waiting} Pending
          </span>
          <span className="flex items-center gap-1 text-success">
            <CheckCircle className="w-3 h-3" /> {metrics.approved} Active
          </span>
        </div>
      </div>

      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <div className="flex-1 overflow-auto space-y-2">
        {terms.map((term) => (
          <div key={term.id} className="bg-card-border/30 rounded p-3 border border-card-border">
            <div className="flex items-center justify-between mb-1">
              <span className="font-mono text-sm font-bold text-foreground">{term.term}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${term.review_status === "PENDING" ? "bg-warning/20 text-warning" : term.review_status === "APPROVED" ? "bg-success/20 text-success" : "bg-danger/20 text-danger"}`}>
                {term.review_status}
              </span>
            </div>
            <div className="text-xs text-muted mb-2">{term.category} | Confidence: {term.confidence_score}%</div>
            {term.review_status === "PENDING" && canReview && (
              <div className="flex gap-2">
                <button disabled={busy !== null} onClick={() => void handleReview(term.id, "APPROVED")} className="flex-1 bg-success/20 hover:bg-success/30 text-success text-xs py-1 rounded flex items-center justify-center gap-1 transition-colors">
                  <CheckCircle className="w-3 h-3" /> Approve
                </button>
                <button disabled={busy !== null} onClick={() => void handleReview(term.id, "REJECTED")} className="flex-1 bg-danger/20 hover:bg-danger/30 text-danger text-xs py-1 rounded flex items-center justify-center gap-1 transition-colors">
                  <XCircle className="w-3 h-3" /> Reject
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
