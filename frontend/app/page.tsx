"use client";

import { useState } from "react";
import { Sidebar } from "@/components/dashboard/Sidebar";
import { Header } from "@/components/dashboard/Header";
import { AlertFlash } from "@/components/dashboard/AlertFlash";
import { OverviewCards } from "@/components/dashboard/OverviewCards";
import { LiveThreatFeed } from "@/components/dashboard/LiveThreatFeed";
import { CryptoNetworkGraph } from "@/components/dashboard/CryptoNetworkGraph";
import { GeoThreatMap } from "@/components/dashboard/GeoThreatMap";
import { LexiconReviewPanel } from "@/components/dashboard/LexiconReviewPanel";
import { FleetStatusGauge } from "@/components/dashboard/FleetStatusGauge";
import { SystemHealthPanel } from "@/components/dashboard/SystemHealthPanel";
import { useWebSocket } from "@/hooks/useWebSocket";
import { AuthGate, useRadarUser } from "@/components/AuthGate";
import { AlertReviewPanel } from "@/components/dashboard/AlertReviewPanel";
import { OperationsPanel } from "@/components/dashboard/OperationsPanel";
import { api } from "@/lib/api";
import { IntelligencePanel } from "@/components/dashboard/IntelligencePanels";

export default function DashboardPage() {
  return <AuthGate><Dashboard /></AuthGate>;
}

function Dashboard() {
  const [activePanel, setActivePanel] = useState("overview");
  const { alerts, connected, clearAlerts } = useWebSocket();
  const user = useRadarUser();
  const [error, setError] = useState("");

  const logout = async () => {
    try {
      await api.auth.logout();
      window.location.reload();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Sign-out failed."); }
  };

  const renderPanel = () => {
    switch (activePanel) {
      case "overview":
        return (
          <div className="space-y-4">
            <OverviewCards />
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 h-[500px]">
              <div className="lg:col-span-1 bg-card border border-card-border rounded-lg p-4">
                <LiveThreatFeed />
              </div>
              <div className="lg:col-span-2 bg-card border border-card-border rounded-lg p-4">
                <CryptoNetworkGraph />
              </div>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 h-[400px]">
              <div className="bg-card border border-card-border rounded-lg p-4">
                <GeoThreatMap />
              </div>
              <div className="bg-card border border-card-border rounded-lg p-4">
                <LexiconReviewPanel />
              </div>
            </div>
          </div>
        );
      case "live-feed":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <LiveThreatFeed />
          </div>
        );
      case "crypto":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <CryptoNetworkGraph />
          </div>
        );
      case "geo":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <GeoThreatMap />
          </div>
        );
      case "lexicon":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <LexiconReviewPanel />
          </div>
        );
      case "fleet":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <FleetStatusGauge />
          </div>
        );
      case "health":
        return (
          <div className="bg-card border border-card-border rounded-lg p-4 h-[calc(100vh-140px)]">
            <SystemHealthPanel />
          </div>
        );
      case "review":
        return <AlertReviewPanel />;
      case "dossiers":
      case "siem":
        return <IntelligencePanel mode={activePanel} />;
      case "ops":
        return <OperationsPanel />;
      default:
        return (
          <div className="bg-card border border-card-border rounded-lg p-8 text-center text-muted">
            <div className="text-4xl mb-4">🛡️</div>
            <div className="text-lg font-semibold">Radar Command Center</div>
            <div className="text-sm mt-2">Choose a panel from the sidebar.</div>
          </div>
        );
    }
  };

  return (
    <div className="flex h-screen bg-background">
      <Sidebar active={activePanel} onSelect={setActivePanel} role={user?.role || "reader"} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Header wsConnected={connected} alertCount={alerts.length} username={user?.username || ""} role={user?.role || "reader"} onLogout={logout} />
        <AlertFlash alerts={alerts} onClear={clearAlerts} />
        <main className="flex-1 overflow-auto p-4">
          {error && <p role="alert" className="text-danger mb-3">{error}</p>}
          {renderPanel()}
        </main>
      </div>
    </div>
  );
}
