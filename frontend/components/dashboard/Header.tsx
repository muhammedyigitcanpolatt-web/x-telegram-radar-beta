"use client";

import { Shield, Wifi, WifiOff, Bell, LogOut } from "lucide-react";
import { cn } from "@/lib/utils";

export function Header({ wsConnected, alertCount, username, role, onLogout }: { wsConnected: boolean; alertCount: number; username: string; role: string; onLogout: () => void }) {
  return (
    <header className="h-14 bg-card border-b border-card-border flex items-center justify-between px-6">
      <div className="flex items-center gap-4">
        <Shield className="w-5 h-5 text-accent" />
        <h1 className="text-sm font-semibold tracking-wider uppercase">
          Shortmox Radar — Command Center
        </h1>
      </div>

      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 text-xs">
          {wsConnected ? (
            <>
              <Wifi className="w-4 h-4 text-success" />
              <span className="text-success">WS LIVE</span>
            </>
          ) : (
            <>
              <WifiOff className="w-4 h-4 text-danger" />
              <span className="text-danger">WS OFFLINE</span>
            </>
          )}
        </div>

        <div className="relative">
          <Bell className="w-5 h-5 text-muted" />
          {alertCount > 0 && (
            <span className={cn("absolute -top-1 -right-1 w-4 h-4 rounded-full text-[10px] flex items-center justify-center font-bold", alertCount > 5 ? "bg-danger text-white animate-pulse" : "bg-warning text-black")}>
              {alertCount > 99 ? "99+" : alertCount}
            </span>
          )}
        </div>
        <div className="hidden sm:block text-right text-xs">
          <div className="text-foreground">{username}</div>
          <div className="text-muted">{role}</div>
        </div>
        <button onClick={onLogout} title="Sign out" className="text-muted hover:text-foreground" aria-label="Sign out">
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
