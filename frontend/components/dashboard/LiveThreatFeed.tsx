"use client";

import { useEffect, useState, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertTriangle, CheckCircle, XCircle } from "lucide-react";
import InspectorPanel from "./InspectorPanel";
import { api } from "@/lib/api";
import type { ThreatFeedItem } from "@/types";

export function LiveThreatFeed() {
  const [actorId, setActorId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [feed, setFeed] = useState<ThreatFeedItem[]>([]);
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: feed.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 120,
    overscan: 5,
  });

  useEffect(() => {
    let active = true;
    const refresh = () => api.dashboard.liveThreatFeed(50)
      .then((res) => { if (active) { setFeed(res.feed); setError(""); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Could not load the feed."); });
    void refresh();
    const iv = setInterval(() => void refresh(), 15000);
    return () => { active = false; clearInterval(iv); };
  }, []);

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-accent uppercase tracking-wider flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" />
          Live Threat Feed
        </h3>
        <span className="text-xs text-muted font-mono">{feed.length} signals</span>
      </div>

      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <InspectorPanel actorId={actorId} onClose={() => setActorId(null)} />
      <div ref={parentRef} className="flex-1 overflow-auto">
        <div style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}>
          {virtualizer.getVirtualItems().map((virtualItem) => {
            const item = feed[virtualItem.index];
            return (
              <div
                key={item.tweet_id}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualItem.start}px)`,
                }}
                className="p-3 border-b border-card-border hover:bg-card-border/50 transition-colors"
              >
                <div className="flex items-center justify-between mb-1">
                  <button className="text-xs font-mono text-accent" onClick={() => setActorId(item.username)}>@{item.username}</button>
                  <div className="flex items-center gap-2">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${item.confidence_score >= 90 ? "bg-danger/20 text-danger" : "bg-warning/20 text-warning"}`}>
                      {item.admiralty_code}
                    </span>
                    {item.pushed_to_siem ? (
                      <CheckCircle className="w-3 h-3 text-success" />
                    ) : (
                      <XCircle className="w-3 h-3 text-muted" />
                    )}
                  </div>
                </div>
                <p className="text-xs text-foreground/80 line-clamp-2">{item.raw_text}</p>
                <div className="flex items-center justify-between mt-1">
                  <span className="text-[10px] text-muted">{item.threat_category}</span>
                  <span className="text-[10px] text-muted font-mono">{new Date(item.detected_at).toLocaleTimeString("en-US")}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
