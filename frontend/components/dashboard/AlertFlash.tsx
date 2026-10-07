"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import type { WebSocketAlert } from "@/types";

export function AlertFlash({ alerts, onClear }: { alerts: WebSocketAlert[]; onClear: () => void }) {
  const [visible, setVisible] = useState(false);
  const latest = alerts[0];
  const isAnomalyStrength = latest?.score_kind === "anomaly_strength";
  const score = isAnomalyStrength ? latest?.score ?? 0 : latest?.confidence ?? latest?.score ?? 0;

  useEffect(() => {
    if (latest && score >= 80) {
      setVisible(true);
      const t = setTimeout(() => setVisible(false), 8000);
      return () => clearTimeout(t);
    }
  }, [latest, score]);

  if (!visible || !latest) return null;

  return (
    <div className="fixed top-16 left-1/2 -translate-x-1/2 z-50 w-[600px] max-w-[90vw]">
      <div className="bg-danger/90 backdrop-blur border border-danger/50 rounded-lg p-4 pulse-alert">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <AlertTriangle className="w-6 h-6 text-white" />
            <div>
              <div className="font-bold text-white text-sm">
                {isAnomalyStrength ? "STRONG ANOMALY SIGNAL" : "HIGH-CONFIDENCE RADAR SIGNAL"}
              </div>
              <div className="text-white/80 text-xs mt-1 font-mono">
                @{latest.username} | {latest.category || latest.event} | {isAnomalyStrength ? `Anomaly strength: ${score}/100` : `Confidence: ${score}%`}
              </div>
              <div className="text-white/70 text-xs mt-1 line-clamp-2">{latest.text}</div>
            </div>
          </div>
          <button onClick={() => setVisible(false)} className="text-white/60 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
