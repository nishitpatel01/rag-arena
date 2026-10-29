import { useEffect, useMemo, useState } from "react";
import { useOptionalIngestSession } from "../../app/IngestSessions";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import type { IngestStatusResponse } from "../../types/arena";

/** Level → BigQuery `tier_profile`; `managed_vertex` has no tier (its status is `GET /ingest/status` → `managed_vertex`). */
export const TIER_FOR_LEVEL: Record<string, string | null> = {
  level1_naive: "naive",
  level2_hybrid: "hybrid",
  level3_multimodal: "multimodal",
  level4_agentic: "multimodal",
  managed_vertex: null,
};

/** Formats nullable BigQuery `ingest_runs` integer cells (`managed_vertex` rows have `chunks = null`). */
export function formatIngestRunCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
}

/** Levels whose ingestion is a managed import (Vertex AI Search data store) instead of a BigQuery tier. */
export function isManagedLevel(levelId: string): boolean {
  return levelId === "managed_vertex";
}

/**
 * Level 5 data store view from `status.managed_vertex` (document count + state), falling back to a numeric
 * `counts_by_tier.managed_vertex`. Null when the backend reports neither.
 */
export function managedStoreFor(status: IngestStatusResponse | null): { readonly documents: number | null; readonly state: string | null; readonly reason: string | null } | null {
  if (!status) return null;
  const mv = status.managed_vertex;
  if (mv && typeof mv === "object") {
    const documents = typeof mv.document_count === "number" && Number.isFinite(mv.document_count) ? mv.document_count : null;
    const state = typeof mv.data_store_state === "string" && mv.data_store_state.trim() ? mv.data_store_state : null;
    const reason = mv.available === false ? mv.reason?.trim() || "data store status not available" : null;
    return { documents, state, reason };
  }
  const legacy = status.counts_by_tier?.managed_vertex;
  if (typeof legacy === "number") return { documents: legacy, state: null, reason: null };
  return null;
}

/** Tier row count for a level; `managed_vertex` → data store document count (null when not reported). */
export function tierCountFor(levelId: string, status: IngestStatusResponse | null): number | null {
  if (!status) return null;
  const tier = TIER_FOR_LEVEL[levelId];
  if (tier === null || isManagedLevel(levelId)) return managedStoreFor(status)?.documents ?? null;
  if (tier === undefined) return null;
  const value = status.counts_by_tier[tier];
  return typeof value === "number" ? value : 0;
}

export interface RunGate {
  /** Run is blocked only when the status is known, available, and the tier has 0 rows. */
  readonly blocked: boolean;
  /** Human-readable note to show as a pill (reason for unavailability, or error). */
  readonly note: string | null;
  readonly tone: "ok" | "warn" | "info" | "err" | "neutral";
  readonly count: number | null;
}

/** Decide whether Run is allowed for a level given the ingest status (never blocks when `available === false`). */
export function runGateFor(levelId: string, status: IngestStatusResponse | null, error: string | null): RunGate {
  if (error) return { blocked: false, note: `ingest status unavailable: ${error}`, tone: "err", count: null };
  if (!status) return { blocked: true, note: null, tone: "neutral", count: null };
  if (status.available === false) {
    return { blocked: false, note: status.reason || "ingest status not available", tone: "info", count: null };
  }
  const count = tierCountFor(levelId, status);
  if (isManagedLevel(levelId) || TIER_FOR_LEVEL[levelId] === null) {
    const store = managedStoreFor(status);
    if (!store) return { blocked: false, note: "data store status not reported by GET /api/v1/ingest/status", tone: "info", count: null };
    if (store.reason) return { blocked: false, note: store.reason, tone: "info", count: store.documents };
    if (store.documents === 0) return { blocked: true, note: "0 documents imported into the Vertex AI Search data store", tone: "warn", count: 0 };
    return { blocked: false, note: null, tone: store.documents === null ? "info" : "ok", count: store.documents };
  }
  if (count === 0) return { blocked: true, note: "0 chunks ingested for this tier", tone: "warn", count };
  return { blocked: false, note: null, tone: "ok", count };
}

/** Header pill text: "naive tier: 5067 chunks" or "data store: 45 documents · ACTIVE". */
export function ingestPillText(levelId: string, gate: RunGate, status: IngestStatusResponse | null): string {
  if (isManagedLevel(levelId) || TIER_FOR_LEVEL[levelId] === null) {
    const store = managedStoreFor(status);
    if (gate.count === null) return gate.note ?? "data store status";
    return `data store: ${gate.count.toLocaleString()} documents${store?.state ? ` · ${store.state}` : ""}`;
  }
  if (gate.count !== null) return `${TIER_FOR_LEVEL[levelId] ?? "?"} tier: ${gate.count} chunks`;
  return gate.note ?? "ingest status";
}

export interface IngestStatusState {
  readonly status: IngestStatusResponse | null;
  readonly error: string | null;
  readonly reload: () => void;
}

export function useIngestStatus(): IngestStatusState {
  const ingestCtx = useOptionalIngestSession();
  const completedSeq = ingestCtx?.snapshot.completedSeq ?? 0;
  const ingestSession = ingestCtx?.session ?? null;
  const [status, setStatus] = useState<IngestStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    api.ingestStatus().then(
      (s) => {
        if (!cancelled) {
          ingestSession?.syncActiveJobs(s.active_jobs);
          setStatus(s);
          setError(null);
        }
      },
      (e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    );
    return () => {
      cancelled = true;
    };
  }, [nonce, completedSeq, ingestSession]);
  // Screen-share safe mode: the table FQN and any error text are masked before rendering.
  const { mask, maskValue } = useSafeMode();
  const shownStatus = useMemo(() => maskValue(status), [maskValue, status]);
  return { status: shownStatus, error: error === null ? null : mask(error), reload: () => setNonce((n) => n + 1) };
}
