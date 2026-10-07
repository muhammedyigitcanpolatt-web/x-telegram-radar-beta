"use client";

import { createContext, FormEvent, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

export type RadarRole = "reader" | "analyst" | "admin";
export interface RadarUser { username: string; role: RadarRole }

const AuthContext = createContext<RadarUser | null>(null);
export const useRadarUser = () => useContext(AuthContext);

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<RadarUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const expired = () => setUser(null);
    window.addEventListener("radar-session-expired", expired);
    api.auth.me().then(setUser).catch(() => setUser(null)).finally(() => setChecking(false));
    return () => window.removeEventListener("radar-session-expired", expired);
  }, []);

  const value = useMemo(() => user, [user]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      setUser(await api.auth.login(username, password));
      setPassword("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed.");
    } finally {
      setSubmitting(false);
    }
  };

  if (checking) return <div className="min-h-screen bg-background p-8 text-muted">Checking session…</div>;
  if (user) return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;

  return (
    <main className="min-h-screen bg-background flex items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl border border-card-border bg-card p-6 space-y-4">
        <div>
          <h1 className="text-xl font-semibold text-foreground">Radar sign-in</h1>
          <p className="mt-1 text-sm text-muted">Sign in with your account to continue.</p>
        </div>
        <label className="block text-sm text-muted">
          Username
          <input autoComplete="username" required value={username} onChange={(e) => setUsername(e.target.value)} className="mt-1 w-full rounded border border-card-border bg-background px-3 py-2 text-foreground" />
        </label>
        <label className="block text-sm text-muted">
          Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="mt-1 w-full rounded border border-card-border bg-background px-3 py-2 text-foreground" />
        </label>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button disabled={submitting} className="w-full rounded bg-accent px-3 py-2 font-semibold text-white disabled:opacity-50">
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
