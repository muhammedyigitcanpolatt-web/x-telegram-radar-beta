"use client";

import { useEffect, useState, useCallback } from "react";
import dynamic from "next/dynamic";
import type { ForceGraphProps } from "react-force-graph-2d";
import InspectorPanel from "./InspectorPanel";
import { api } from "@/lib/api";

const ForceGraph2D = dynamic<ForceGraphProps<GraphNode, GraphLink>>(() => import("react-force-graph-2d"), { ssr: false });

interface GraphNode {
  id: string;
  name: string;
  val: number;
  color: string;
  type: "account" | "wallet" | "visual";
  balanceUsd?: number;
  walletAddress?: string;
}

interface GraphLink {
  source: string;
  target: string;
  color: string;
}

export function CryptoNetworkGraph() {
  const [data, setData] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });
  const [error, setError] = useState("");
  const [actorId, setActorId] = useState<string | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);

  useEffect(() => {
    api.dashboard.cryptoIntelligence(30).then((res) => {
      const nodes: GraphNode[] = [];
      const links: GraphLink[] = [];
      const seen = new Set<string>();

      res.data.forEach((item: any) => {
        const accId = `acc_${item.associated_username || "unknown"}`;
        const walletId = `wal_${item.wallet_address}`;

        if (!seen.has(accId)) {
          seen.add(accId);
          nodes.push({ id: accId, name: item.associated_username || "Anonymous", val: 8, color: "#3b82f6", type: "account" });
        }
        if (!seen.has(walletId)) {
          seen.add(walletId);
          const balance = Number(item.balance_usd) || 0;
          nodes.push({ id: walletId, name: `${item.currency} ${item.wallet_address.slice(0, 8)}...`, val: Math.min(balance / 1000 + 4, 20), color: balance > 10000 ? "#ef4444" : "#22c55e", type: "wallet", balanceUsd: balance, walletAddress: item.wallet_address });
        }

        links.push({ source: accId, target: walletId, color: "#1e1e2e" });
      });

      setData({ nodes, links });
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "Could not load the graph."));
  }, []);

  const handleClick = useCallback((node: GraphNode) => {
    setSelected(node);
    if (node.type === "account") setActorId(node.name);
  }, []);

  return (
    <div className="h-full flex flex-col">
      <h3 className="text-sm font-semibold text-accent uppercase tracking-wider mb-2">Follow the Money — Crypto Network</h3>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
      <InspectorPanel actorId={actorId} onClose={() => setActorId(null)} />
      <div className="flex-1 relative overflow-hidden">
        <ForceGraph2D
          graphData={data}
          nodeAutoColorBy="type"
          backgroundColor="#0a0a0f"
          linkColor={() => "#1e1e2e"}
          nodeLabel="name"
          onNodeClick={handleClick}
          nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
            const label = node.name;
            const fontSize = 12 / globalScale;
            ctx.font = `${fontSize}px JetBrains Mono, monospace`;
            ctx.fillStyle = node.color;
            ctx.beginPath();
            ctx.arc(node.x, node.y, node.val, 0, 2 * Math.PI);
            ctx.fill();
            ctx.fillStyle = "#e2e8f0";
            ctx.fillText(label, node.x + node.val + 2, node.y + fontSize / 2);
          }}
        />
        {selected && (
          <div className="absolute bottom-4 left-4 bg-card border border-card-border rounded-lg p-3 max-w-xs">
            <div className="text-xs font-mono text-accent mb-1">{selected.type.toUpperCase()}</div>
            <div className="text-sm font-semibold">{selected.name}</div>
            {selected.type === "wallet" && (
              <div className="mt-1 space-y-1 text-xs text-muted">
                <div>Balance: ${(selected.balanceUsd || 0).toLocaleString("en-US")}</div>
                <div className="break-all">{selected.walletAddress}</div>
              </div>
            )}
            <button onClick={() => setSelected(null)} className="absolute top-1 right-1 text-muted hover:text-foreground text-xs">✕</button>
          </div>
        )}
      </div>
    </div>
  );
}
