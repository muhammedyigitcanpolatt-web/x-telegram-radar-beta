"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { WebSocketAlert } from "@/types";

function sameOriginWebSocketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/signals`;
}

export function useWebSocket() {
  const [alerts, setAlerts] = useState<WebSocketAlert[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let stopped = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;

    const connect = () => {
      if (stopped) return;
      const ws = new WebSocket(sameOriginWebSocketUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        delay = 1000;
        setConnected(true);
        window.dispatchEvent(new CustomEvent("shortmox-radar-connection", { detail: true }));
      };
      ws.onclose = (event) => {
        setConnected(false);
        window.dispatchEvent(new CustomEvent("shortmox-radar-connection", { detail: false }));
        if (event.code === 4401 || event.code === 4403 || event.code === 1008) {
          if (!stopped) window.dispatchEvent(new Event("radar-session-expired"));
          return;
        }
        if (!stopped) {
          reconnectTimer = setTimeout(connect, delay);
          delay = Math.min(delay * 2, 15000);
        }
      };
      ws.onmessage = (message) => {
        try {
          const event: WebSocketAlert = JSON.parse(message.data);
          if (event.schema_version === 1 && (event.event === "ANOMALY_DETECTED" || event.event === "CARTEL_THREAT")) {
            window.dispatchEvent(new CustomEvent("shortmox-radar-alert", { detail: event }));
            setAlerts((previous) => {
              if (previous.some((item) => item.event_id === event.event_id)) return previous;
              return [event, ...previous].slice(0, 100);
            });
          }
        } catch {
          // Ignore non-JSON or unsupported event versions.
        }
      };
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, []);

  const clearAlerts = useCallback(() => setAlerts([]), []);
  return { alerts, connected, clearAlerts };
}
