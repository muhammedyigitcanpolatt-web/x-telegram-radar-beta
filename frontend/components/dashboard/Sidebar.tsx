"use client";

import { useState } from "react";
import { Radar, Shield, Globe, Brain, Server, Users, Wallet, FileText, Activity, AlertTriangle, Menu, X, ClipboardCheck, Power } from "lucide-react";
import { cn } from "@/lib/utils";

const navItems = [
  { id: "overview", label: "Operations Overview", icon: Radar },
  { id: "live-feed", label: "Live Threat Feed", icon: AlertTriangle },
  { id: "crypto", label: "Crypto Network", icon: Wallet },
  { id: "geo", label: "Geographic Map", icon: Globe },
  { id: "lexicon", label: "AI Lexicon", icon: Brain },
  { id: "fleet", label: "Fleet Status", icon: Server },
  { id: "dossiers", label: "Threat Actors", icon: Users },
  { id: "siem", label: "SIEM Status", icon: FileText },
  { id: "health", label: "System Health", icon: Activity },
];

export function Sidebar({ active, onSelect, role }: { active: string; onSelect: (id: string) => void; role: string }) {
  const [collapsed, setCollapsed] = useState(false);
  const visibleItems = [
    ...navItems,
    { id: "review", label: "Analyst Review", icon: ClipboardCheck },
    ...(role === "admin" ? [{ id: "ops", label: "Operations", icon: Power }] : []),
  ];

  return (
    <aside className={cn("h-screen bg-card border-r border-card-border flex flex-col transition-all duration-300", collapsed ? "w-16" : "w-64")}>
      <div className="flex items-center justify-between p-4 border-b border-card-border">
        {!collapsed && (
          <div className="flex items-center gap-2">
            <Shield className="w-5 h-5 text-accent" />
            <span className="font-bold text-sm tracking-wider">SHORTMOX</span>
          </div>
        )}
        <button onClick={() => setCollapsed(!collapsed)} className="text-muted hover:text-foreground">
          {collapsed ? <Menu className="w-5 h-5" /> : <X className="w-5 h-5" />}
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          const isActive = active === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={cn(
                "w-full flex items-center gap-3 px-4 py-3 text-sm transition-colors",
                isActive ? "bg-primary/10 text-accent border-r-2 border-accent" : "text-muted hover:text-foreground hover:bg-card-border",
                collapsed && "justify-center px-2"
              )}
            >
              <Icon className="w-5 h-5 shrink-0" />
              {!collapsed && <span>{item.label}</span>}
            </button>
          );
        })}
      </nav>

      {!collapsed && (
        <div className="p-4 border-t border-card-border text-xs text-muted">
          <div className="flex items-center gap-2 mb-1">
            <div className="w-2 h-2 rounded-full bg-success animate-pulse" />
            <span>Radar Node Active</span>
          </div>
          <div className="font-mono opacity-50">PUBLIC BETA</div>
        </div>
      )}
    </aside>
  );
}
