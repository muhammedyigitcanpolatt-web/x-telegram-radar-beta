"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useRadarUser } from "@/components/AuthGate";

export function OperationsPanel() {
  const user = useRadarUser();
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [collection, setCollection] = useState<any>(null);
  const [templates, setTemplates] = useState<any[]>([]);
  const [query, setQuery] = useState("");
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [collectionBusy, setCollectionBusy] = useState(false);
  const [collectionResult, setCollectionResult] = useState<any>(null);
  const [collectionError, setCollectionError] = useState("");

  useEffect(() => {
    api.ops.templates().then((response: any) => {
      setTemplates(response.templates || []);
      setSelectedTemplate((current: string) => current || response.templates?.[0]?.key || "");
      setCollection(response.collection || null);
    }).catch((error) => {
      setCollectionError(error instanceof Error ? error.message : "Could not load collection settings.");
    });
  }, []);

  if (user?.role !== "admin") return <p className="text-sm text-muted">This panel requires an admin role.</p>;

  const run = async (action: "pause" | "resume") => {
    setBusy(true);
    try {
      setResult(action === "pause" ? await api.ops.killSwitch() : await api.ops.resume());
    } catch (error) {
      setResult({ status: "ERROR", message: error instanceof Error ? error.message : "Operation failed" });
    } finally {
      setBusy(false);
    }
  };

  const runCollection = async (action: () => Promise<any>) => {
    setCollectionBusy(true);
    setCollectionError("");
    try {
      setCollectionResult(await action());
    } catch (error) {
      setCollectionResult(null);
      setCollectionError(error instanceof Error ? error.message : "Could not enqueue the collection job.");
    } finally {
      setCollectionBusy(false);
    }
  };

  const collectionEnabled = Boolean(collection?.enabled);
  const xReady = collectionEnabled && collection?.sources?.x?.status === "ready";
  const telegramReady = collectionEnabled && collection?.sources?.telegram?.status === "ready";
  const osintReady = collectionEnabled && collection?.sources?.osint?.status === "ready";

  const sourceMessage = (source: string) => {
    const state = collection?.sources?.[source];
    if (!collection) return "Loading collection settings.";
    if (!collectionEnabled) return "Collection is disabled. Set COLLECTION_ENABLED=true on the server.";
    if (state?.status === "configuration_required") return `Missing settings: ${(state.missing || []).join(", ")}.`;
    return "Ready";
  };

  return (
    <section className="max-w-2xl rounded border border-card-border bg-card p-5 space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-foreground">Operations</h2>
        <p className="mt-1 text-sm text-muted">A pause request updates shared Redis state, clears Celery queues, and requests termination of active jobs.</p>
      </div>
      <div className="flex gap-3">
        <button disabled={busy} onClick={() => void run("pause")} className="rounded bg-danger px-4 py-2 text-sm text-white disabled:opacity-50">Pause jobs</button>
        <button disabled={busy} onClick={() => void run("resume")} className="rounded border border-card-border px-4 py-2 text-sm text-foreground disabled:opacity-50">Resume jobs</button>
      </div>
      {result && <pre className="overflow-auto rounded bg-background p-3 text-xs text-muted">{JSON.stringify(result, null, 2)}</pre>}

      <div className="space-y-4 border-t border-card-border pt-4">
        <div>
          <h3 className="font-semibold text-foreground">Source collection</h3>
          <p className="mt-1 text-sm text-muted">
            Jobs enter the worker queue. The X provider is selected on the server (default: scraping); scraping needs its server-side GraphQL bearer token, while API mode needs its own bearer token. Telegram reads only configured channels accessible to the account; it never joins channels.
          </p>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1 text-sm text-muted">
            X search query
            <input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={512} className="w-full rounded border border-card-border bg-background px-3 py-2 text-foreground" placeholder='E.g. "cartel" OR "sicario"' />
          </label>
          <label className="space-y-1 text-sm text-muted">
            Campaign template
            <select value={selectedTemplate} onChange={(event) => setSelectedTemplate(event.target.value)} className="w-full rounded border border-card-border bg-background px-3 py-2 text-foreground">
              {templates.map((template) => <option key={template.key} value={template.key}>{template.label}</option>)}
            </select>
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <button disabled={!xReady || !query.trim() || collectionBusy} onClick={() => void runCollection(() => api.ops.triggerSweep(query.trim()))} className="rounded border border-card-border px-3 py-2 text-sm text-foreground disabled:opacity-50">Scan X query</button>
          <button disabled={!xReady || !selectedTemplate || collectionBusy} onClick={() => void runCollection(() => api.ops.triggerCampaign(selectedTemplate))} className="rounded border border-card-border px-3 py-2 text-sm text-foreground disabled:opacity-50">Scan template</button>
          <button disabled={!telegramReady || collectionBusy} onClick={() => void runCollection(() => api.ops.triggerTelegramSweep())} className="rounded border border-card-border px-3 py-2 text-sm text-foreground disabled:opacity-50">Scan Telegram channels</button>
          <button disabled={!xReady || collectionBusy} onClick={() => void runCollection(() => api.ops.triggerLinkSeeder("x"))} className="rounded border border-card-border px-3 py-2 text-sm text-foreground disabled:opacity-50">Scan X links</button>
          <button disabled={!osintReady || collectionBusy} onClick={() => void runCollection(() => api.ops.triggerLinkSeeder("osint"))} className="rounded border border-card-border px-3 py-2 text-sm text-foreground disabled:opacity-50">Scan public OSINT sources</button>
        </div>
        <div className="grid gap-1 text-xs text-muted sm:grid-cols-3">
          <p>X ({collection?.sources?.x?.provider || "scraping"}{collection?.sources?.x?.credential_source ? ` / ${collection.sources.x.credential_source}` : ""}): {sourceMessage("x")}</p>
          <p>Telegram: {sourceMessage("telegram")}</p>
          <p>Public OSINT: {sourceMessage("osint")}</p>
        </div>
        {collectionError && <p role="alert" className="text-sm text-danger">{collectionError}</p>}
        {collectionResult && <pre role="status" className="overflow-auto rounded bg-background p-3 text-xs text-muted">{JSON.stringify(collectionResult, null, 2)}</pre>}
      </div>
    </section>
  );
}
