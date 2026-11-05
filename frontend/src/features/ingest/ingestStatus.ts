import { useEffect, useMemo, useState } from "react";
import { useOptionalIngestSession } from "../../app/IngestSessions";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import type { EphemeralScannStoreStatus, IngestStatusResponse } from "../../types/arena";

/** Level → BigQuery `tier_profile`; managed (L5–L6) and ephemeral ScaNN (L7–L8) levels have no BigQuery tier. */
export const TIER_FOR_LEVEL: Record<string, string | null> = {
  level1_naive: "naive",
  level2_hybrid: "hybrid",
  level3_multimodal: "multimodal",
  level4_agentic: "multimodal",
  managed_vertex: null,
  rag_engine: null,
  alloydb_scann: null,
  vertex_vector_v2: null,
};

/** Formats nullable BigQuery `ingest_runs` integer cells (`managed_vertex` / `rag_engine` rows have `chunks = null`). */
export function formatIngestRunCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
}

/** True for Level 6 (`rag_engine`, Vertex AI RAG Engine `RagCorpus`). */
export function isRagEngineLevel(levelId: string): boolean {
  return levelId === "rag_engine";
}

/** True for Levels 7–8 (`alloydb_scann` / `vertex_vector_v2`, CLI-ephemeral ScaNN benchmark stores). */
export function isEphemeralScannLevel(levelId: string): boolean {
  return levelId === "alloydb_scann" || levelId === "vertex_vector_v2";
}

/** Levels whose ingestion is a managed import (Vertex AI Search data store or RAG Engine `RagCorpus`) instead of BigQuery. */
export function isManagedLevel(levelId: string): boolean {
  return levelId === "managed_vertex" || levelId === "rag_engine";
}

/** Ephemeral ScaNN store status from `status.ephemeral_scann` for Levels 7–8. */
export function ephemeralScannFor(
  levelId: string,
  status: IngestStatusResponse | null,
): EphemeralScannStoreStatus | null {
  if (!status?.ephemeral_scann) return null;
  if (levelId === "alloydb_scann") return status.ephemeral_scann.alloydb_scann ?? null;
  if (levelId === "vertex_vector_v2") return status.ephemeral_scann.vertex_vector_v2 ?? null;
  return null;
}

export interface RagCorpusStatusView {
  readonly files: number | null;
  readonly activeFiles: number | null;
  readonly state: string | null;
  readonly dbMode: string | null;
  readonly location: string | null;
  readonly corpusDisplayName: string | null;
  readonly embeddingModel: string | null;
  readonly reason: string | null;
}

/** Level 6 `RagCorpus` status from `status.rag_engine` (falling back to numeric `counts_by_tier.rag_engine`). */
export function ragCorpusFor(status: IngestStatusResponse | null): RagCorpusStatusView | null {
  if (!status) return null;
  const re = status.rag_engine;
  if (re && typeof re === "object") {
    const files = typeof re.file_count === "number" && Number.isFinite(re.file_count) ? re.file_count : null;
    const activeFiles = typeof re.active_files === "number" && Number.isFinite(re.active_files) ? re.active_files : null;
    const state = typeof re.corpus_state === "string" && re.corpus_state.trim() ? re.corpus_state : null;
    const dbMode = typeof re.db_mode === "string" && re.db_mode.trim() ? re.db_mode : null;
    const location = typeof re.location === "string" && re.location.trim() ? re.location : null;
    const corpusDisplayName =
      typeof re.corpus_display_name === "string" && re.corpus_display_name.trim() ? re.corpus_display_name : null;
    const embeddingModel =
      typeof re.embedding_model === "string" && re.embedding_model.trim() ? re.embedding_model : null;
    const reason = re.available === false ? re.reason?.trim() || "RAG Engine corpus status not available" : null;
    return { files, activeFiles, state, dbMode, location, corpusDisplayName, embeddingModel, reason };
  }
  const legacy = status.counts_by_tier?.rag_engine;
  if (typeof legacy === "number") {
    return {
      files: legacy,
      activeFiles: null,
      state: null,
      dbMode: null,
      location: null,
      corpusDisplayName: null,
      embeddingModel: null,
      reason: null,
    };
  }
  return null;
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

/** Tier row count for a level; `managed_vertex` → document count, `rag_engine` → `RagCorpus` file count, L7–L8 → null. */
export function tierCountFor(levelId: string, status: IngestStatusResponse | null): number | null {
  if (!status) return null;
  if (isEphemeralScannLevel(levelId)) return null;
  if (isRagEngineLevel(levelId)) return ragCorpusFor(status)?.files ?? null;
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
  if (isEphemeralScannLevel(levelId)) {
    if (status.demo_mode === "replay") {
      return { blocked: false, note: null, tone: "ok", count: null };
    }
    const scann = ephemeralScannFor(levelId, status);
    if (scann) {
      if (scann.configured === true) {
        return { blocked: false, note: null, tone: "ok", count: null };
      }
      if (scann.configured === false) {
        return {
          blocked: true,
          note: scann.reason?.trim() || "Ephemeral ScaNN store is idle ($0/mo) — run CLI script to provision",
          tone: "warn",
          count: 0,
        };
      }
    }
    if (status.available === false) {
      return { blocked: false, note: status.reason || "ingest status not available", tone: "info", count: null };
    }
    return {
      blocked: false,
      note: "ephemeral ScaNN status not reported by GET /api/v1/ingest/status",
      tone: "info",
      count: null,
    };
  }
  if (status.available === false) {
    return { blocked: false, note: status.reason || "ingest status not available", tone: "info", count: null };
  }
  const count = tierCountFor(levelId, status);
  if (isRagEngineLevel(levelId)) {
    const corpus = ragCorpusFor(status);
    if (!corpus) {
      return {
        blocked: false,
        note: "RAG Engine corpus status not reported by GET /api/v1/ingest/status",
        tone: "info",
        count: null,
      };
    }
    if (corpus.reason) return { blocked: false, note: corpus.reason, tone: "info", count: corpus.files };
    if (corpus.state === "NOT_FOUND") {
      return {
        blocked: true,
        note: "RagCorpus not found — run scripts/provision_rag_engine.py and import the corpus on Ingestion",
        tone: "warn",
        count: 0,
      };
    }
    if (corpus.files === 0) {
      return { blocked: true, note: "0 files imported into the Vertex AI RAG Engine corpus", tone: "warn", count: 0 };
    }
    return { blocked: false, note: null, tone: corpus.files === null ? "info" : "ok", count: corpus.files };
  }
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

/** Header pill text: "naive tier: 5067 chunks", "data store: 45 documents · ACTIVE", or "RagCorpus: 45 files · ACTIVE (spanner_basic)". */
export function ingestPillText(levelId: string, gate: RunGate, status: IngestStatusResponse | null): string {
  if (isEphemeralScannLevel(levelId)) {
    if (status?.demo_mode === "replay") return "ephemeral benchmark · replay ($0/mo idle)";
    const scann = ephemeralScannFor(levelId, status);
    if (scann?.configured) return "ephemeral benchmark · live configured";
    return gate.note ?? "ephemeral benchmark ($0/mo idle)";
  }
  if (isRagEngineLevel(levelId)) {
    const corpus = ragCorpusFor(status);
    if (gate.count === null) return gate.note ?? "RagCorpus status";
    const modeSuffix = corpus?.dbMode ? ` (${corpus.dbMode})` : "";
    const stateSuffix = corpus?.state ? ` · ${corpus.state}${modeSuffix}` : modeSuffix;
    return `RagCorpus: ${gate.count.toLocaleString()} files${stateSuffix}`;
  }
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
