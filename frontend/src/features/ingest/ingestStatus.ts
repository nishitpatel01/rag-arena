import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { IngestStatusResponse } from "../../types/arena";

/** Level → BigQuery `tier_profile`; `managed_vertex` has no tier (status via optional key). */
export const TIER_FOR_LEVEL: Record<string, string | null> = {
  level1_naive: "naive",
  level2_hybrid: "hybrid",
  level3_multimodal: "multimodal",
  level4_agentic: "multimodal",
  managed_vertex: null,
};

/** Tier row count for a level; `managed_vertex` reads an optional `managed_vertex` key (else treated as ready). */
export function tierCountFor(levelId: string, status: IngestStatusResponse | null): number | null {
  if (!status) return null;
  const tier = TIER_FOR_LEVEL[levelId];
  const key = tier ?? "managed_vertex";
  const value = status.counts_by_tier[key];
  if (typeof value === "number") return value;
  return tier === null ? 1 : 0;
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
  if (count === 0) return { blocked: true, note: "0 chunks ingested for this tier", tone: "warn", count };
  return { blocked: false, note: null, tone: "ok", count };
}

export interface IngestStatusState {
  readonly status: IngestStatusResponse | null;
  readonly error: string | null;
  readonly reload: () => void;
}

export function useIngestStatus(): IngestStatusState {
  const [status, setStatus] = useState<IngestStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    api.ingestStatus().then(
      (s) => {
        if (!cancelled) {
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
  }, [nonce]);
  return { status, error, reload: () => setNonce((n) => n + 1) };
}
