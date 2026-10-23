/**
 * Pure reducer: append-only event log → per-engine stage timeline.
 * No timers, no inference; every field comes from an event (spec §7.2, P1).
 * Shared by battle streams (run_id) and ingest job streams (job_id).
 */

import type {
  BattleEvent,
  EngineTimeline,
  IngestEvent,
  RetrievedChunkText,
  StageState,
  StreamInterruptedEvent,
} from "../../types/arena";
import { parseTs } from "../../lib/format";
import { MAX_AGENT_TURNS, normalizeAgentTurn } from "./agentTimelineModel";

export type TimelineEvent = BattleEvent | IngestEvent | StreamInterruptedEvent;

export const STREAM_INTERRUPTED_ERROR_TYPE = "StreamInterrupted";

export function interruptionMessage(reason: string): string {
  return `Stream ended before completion (${reason}) — server restarted or connection lost`;
}

/** Why a stream ended, from what the transport observed (user abort wins over a transport error). */
export function streamEndReason(end: { readonly userAborted: boolean; readonly error: string | null }): string {
  if (end.userAborted) return "stopped by user";
  if (end.error) return `network error: ${end.error}`;
  return "server closed the stream";
}

export function streamInterruptedEvent(runId: string | null, reason: string): StreamInterruptedEvent {
  return { event: "stream_interrupted", run_id: runId, reason, ts: new Date().toISOString() };
}

export interface TimelineMap {
  readonly [engine: string]: EngineTimeline;
}

export interface StreamState {
  readonly runId: string | null;
  /** From `run_started.demo_mode` (live | record | replay | …); null before the run starts. */
  readonly demoMode: string | null;
  /** Catalog ids from `run_started` — the models this run actually executed (not the dropdown state). */
  readonly synthesisModel: string | null;
  readonly judgeModel: string | null;
  /** `run_started.question_id` (golden question key; null for ad-hoc questions). */
  readonly questionId: string | null;
  /** True once any frame carried `replayed: true`. */
  readonly replayed: boolean;
  readonly events: readonly TimelineEvent[];
  readonly timelines: TimelineMap;
  readonly warnings: readonly string[];
  readonly complete: boolean;
  /** A terminal server event arrived (battle_complete, job_complete, job_failed). */
  readonly terminated: boolean;
  /** Set by the client-only `stream_interrupted` event; the reason is shown as an error pill. */
  readonly interrupted: { readonly reason: string } | null;
  readonly enginesCompleted: readonly string[];
  readonly enginesFailed: readonly string[];
}

export const IDLE_STAGE: StageState = {
  status: "idle",
  label: null,
  service: null,
  startedAt: null,
  durationMs: null,
  request: null,
  payload: null,
  error: null,
  skipReason: null,
};

export const INITIAL_STREAM_STATE: StreamState = {
  runId: null,
  demoMode: null,
  synthesisModel: null,
  judgeModel: null,
  questionId: null,
  replayed: false,
  events: [],
  timelines: {},
  warnings: [],
  complete: false,
  terminated: false,
  interrupted: null,
  enginesCompleted: [],
  enginesFailed: [],
};

function emptyTimeline(engine: string): EngineTimeline {
  return { engine, stages: {}, stageOrder: [], agentTurns: [], answer: null, evaluation: null, error: null };
}

function getTimeline(state: StreamState, engine: string): EngineTimeline {
  return state.timelines[engine] ?? emptyTimeline(engine);
}

function withTimeline(state: StreamState, tl: EngineTimeline): StreamState {
  return { ...state, timelines: { ...state.timelines, [tl.engine]: tl } };
}

function setStage(tl: EngineTimeline, stageId: string, patch: Partial<StageState>): EngineTimeline {
  const prev = tl.stages[stageId] ?? IDLE_STAGE;
  const stageOrder = tl.stageOrder.includes(stageId) ? tl.stageOrder : [...tl.stageOrder, stageId];
  return { ...tl, stageOrder, stages: { ...tl.stages, [stageId]: { ...prev, ...patch } } };
}

function idOf(ev: TimelineEvent): string | null {
  if ("run_id" in ev && typeof ev.run_id === "string") return ev.run_id;
  if ("job_id" in ev && typeof ev.job_id === "string") return ev.job_id;
  return null;
}

/**
 * True when the stream reached an end the server announced: a terminal event, or `engine_error`
 * for every engine of the run. Only then may the transport end without `stream_interrupted`.
 */
export function isStreamTerminated(state: StreamState): boolean {
  if (state.terminated) return true;
  const tls = Object.values(state.timelines);
  return tls.length > 0 && tls.every((tl) => tl.error !== null);
}

/** Fail every `active` stage (never `done`, no invented duration); idle/skipped/done stay as they are. */
function interruptStream(state: StreamState, ev: StreamInterruptedEvent): StreamState {
  if (state.events.length === 0 || state.interrupted !== null || isStreamTerminated(state)) return state;
  const error = { type: STREAM_INTERRUPTED_ERROR_TYPE, message: interruptionMessage(ev.reason) };
  const timelines: Record<string, EngineTimeline> = {};
  for (const [engine, tl] of Object.entries(state.timelines)) {
    const stages: Record<string, StageState> = {};
    for (const [id, st] of Object.entries(tl.stages)) stages[id] = st.status === "active" ? { ...st, status: "failed", error } : st;
    timelines[engine] = { ...tl, stages };
  }
  return { ...state, events: [...state.events, ev], timelines, interrupted: { reason: ev.reason } };
}

/** Apply one event. Unknown event types are appended to the log but otherwise ignored. */
export function reduceStreamEvent(state: StreamState, ev: TimelineEvent): StreamState {
  if (ev.event === "stream_interrupted") return interruptStream(state, ev);
  const base: StreamState = {
    ...state,
    events: [...state.events, ev],
    runId: state.runId ?? idOf(ev),
    replayed: state.replayed || ("replayed" in ev && ev.replayed === true),
  };

  switch (ev.event) {
    case "run_started":
      return {
        ...base,
        demoMode: ev.demo_mode,
        synthesisModel: typeof ev.synthesis_model === "string" ? ev.synthesis_model : null,
        judgeModel: typeof ev.judge_model === "string" ? ev.judge_model : null,
        questionId: typeof ev.question_id === "string" && ev.question_id.length > 0 ? ev.question_id : null,
        timelines: Object.fromEntries(ev.engines.map((e) => [e, state.timelines[e] ?? emptyTimeline(e)])),
      };
    case "job_started":
      return withTimeline(base, getTimeline(base, ev.level));
    case "stage_started": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(
        base,
        setStage(tl, ev.stage, {
          status: "active",
          label: ev.label,
          service: ev.service,
          startedAt: parseTs(ev.ts),
          request: ev.request ?? {},
          durationMs: null,
          error: null,
          skipReason: null,
        })
      );
    }
    case "stage_completed": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(
        base,
        setStage(tl, ev.stage, { status: "done", durationMs: ev.duration_ms, payload: ev.payload ?? {} })
      );
    }
    case "stage_failed": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(
        base,
        setStage(tl, ev.stage, {
          status: "failed",
          durationMs: ev.duration_ms,
          error: { type: ev.error_type, message: ev.error },
        })
      );
    }
    case "stage_skipped": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(base, setStage(tl, ev.stage, { status: "skipped", skipReason: ev.reason }));
    }
    case "agent_turn": {
      const tl = getTimeline(base, ev.engine);
      if (tl.agentTurns.length >= MAX_AGENT_TURNS) return base;
      const turn = normalizeAgentTurn(ev as unknown as Record<string, unknown>, tl.agentTurns.length);
      return withTimeline(base, { ...tl, agentTurns: [...tl.agentTurns, turn] });
    }
    case "answer_complete": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(base, {
        ...tl,
        answer: {
          answer: ev.answer,
          citations: ev.citations ?? [],
          retrievedChunks: retrievedChunkTexts(ev.retrieved_chunks),
          latencyMs: ev.latency_ms,
          costUsd: ev.cost_usd,
          usage: ev.usage,
          flags: ev.flags,
        },
      });
    }
    case "eval_complete": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(base, {
        ...tl,
        evaluation: {
          metrics: ev.metrics,
          judgeModel: ev.judge_model,
          referenceAnswerUsed: ev.reference_answer_used,
          durationMs: ev.duration_ms,
          judgeProvider: ev.judge_provider ?? null,
          judgeLocation: ev.judge_location ?? null,
          inputTokens: ev.input_tokens ?? null,
          outputTokens: ev.output_tokens ?? null,
          judgeCostUsd: ev.judge_cost_usd ?? null,
          runCostUsd: ev.run_cost_usd ?? null,
          rationale: ev.rationale ?? null,
        },
      });
    }
    case "engine_error": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(base, {
        ...tl,
        error: { type: ev.error_type, message: ev.error, failedStage: ev.failed_stage },
      });
    }
    case "stream_warning":
      return { ...base, warnings: [...base.warnings, ev.message] };
    case "battle_complete":
      return {
        ...base,
        complete: true,
        terminated: true,
        enginesCompleted: ev.engines_completed ?? [],
        enginesFailed: ev.engines_failed ?? [],
      };
    case "job_complete":
      return { ...base, complete: true, terminated: true };
    case "job_failed": {
      const engine = ev.engine ?? Object.keys(base.timelines)[0] ?? null;
      const failed = { ...base, terminated: true };
      if (!engine) return failed;
      const tl = getTimeline(base, engine);
      return withTimeline(failed, { ...tl, error: { type: ev.error_type, message: ev.error, failedStage: ev.failed_stage } });
    }
    case "doc_progress":
    case "embed_progress":
      return base;
    default:
      return base;
  }
}

export function reduceStreamEvents(events: readonly TimelineEvent[]): StreamState {
  return events.reduce(reduceStreamEvent, INITIAL_STREAM_STATE);
}

/** Bound on retrieved chunks kept per answer (server sends ≤ a handful). */
export const MAX_RETRIEVED_CHUNKS = 50;

/** `retrieved_chunks[]` → {chunk_id, content} rows with non-empty strings (others dropped); bounded. */
export function retrievedChunkTexts(raw: unknown): RetrievedChunkText[] {
  if (!Array.isArray(raw)) return [];
  const out: RetrievedChunkText[] = [];
  for (const item of raw.slice(0, MAX_RETRIEVED_CHUNKS)) {
    if (typeof item !== "object" || item === null) continue;
    const { chunk_id, content } = item as Record<string, unknown>;
    if (typeof chunk_id === "string" && chunk_id.length > 0 && typeof content === "string" && content.length > 0) out.push({ chunk_id, content });
  }
  return out;
}

/** The stage currently `active` for an engine (first in stream order), or null. */
export function activeStageId(tl: EngineTimeline | undefined): string | null {
  if (!tl) return null;
  for (const id of tl.stageOrder) {
    if (tl.stages[id]?.status === "active") return id;
  }
  return null;
}

/** Progress summary for ingest jobs, derived from doc/embed progress events. */
export interface IngestProgress {
  readonly docs: readonly DocProgressRow[];
  readonly embed: { done: number; total: number; batchMs: number } | null;
  readonly countsByTier: Record<string, number> | null;
  readonly indexStates: Record<string, string> | null;
}

/** `tables` / `diagrams` are null unless a doc_progress event for the document reported them (Level 3). */
export type DocProgressRow = {
  doc_name: string;
  pages: number;
  chunks: number;
  tables: number | null;
  diagrams: number | null;
  /** Level 3: Gemini vision page outcomes from doc_progress step="gemini_vision" (null when never reported). */
  visionOk?: number | null;
  visionFailed?: number | null;
};

const optCount = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);
const maxOpt = (a: number | null, b: number | null): number | null => (a === null ? b : b === null ? a : Math.max(a, b));

/** One row per document: later doc_progress events (e.g. the chunk stage) update the parse-time row. */
export function upsertDocProgress(rows: DocProgressRow[], next: DocProgressRow): void {
  const idx = rows.findIndex((r) => r.doc_name === next.doc_name);
  if (idx === -1) {
    if (rows.length < 1000) rows.push(next);
    return;
  }
  const prev = rows[idx];
  rows[idx] = {
    doc_name: prev.doc_name,
    pages: Math.max(prev.pages, next.pages),
    chunks: Math.max(prev.chunks, next.chunks),
    tables: maxOpt(prev.tables, next.tables),
    diagrams: maxOpt(prev.diagrams, next.diagrams),
    ...(prev.visionOk !== undefined || next.visionOk !== undefined ? { visionOk: sumOpt(prev.visionOk, next.visionOk) } : {}),
    ...(prev.visionFailed !== undefined || next.visionFailed !== undefined ? { visionFailed: sumOpt(prev.visionFailed, next.visionFailed) } : {}),
  };
}

const sumOpt = (a: number | null | undefined, b: number | null | undefined): number | null =>
  (a ?? null) === null && (b ?? null) === null ? null : (a ?? 0) + (b ?? 0);

export function deriveIngestProgress(events: readonly TimelineEvent[]): IngestProgress {
  const docs: DocProgressRow[] = [];
  let embed: IngestProgress["embed"] = null;
  let countsByTier: Record<string, number> | null = null;
  let indexStates: Record<string, string> | null = null;
  for (const ev of events) {
    if (ev.event === "doc_progress") {
      const row: DocProgressRow = { doc_name: ev.doc_name, pages: ev.pages, chunks: ev.chunks, tables: optCount(ev.tables), diagrams: optCount(ev.diagrams) };
      // Level 3 vision sub-step: one event per transcribed page with status ok | failed.
      if (ev.step === "gemini_vision") {
        row.visionOk = ev.status === "ok" ? 1 : 0;
        row.visionFailed = ev.status === "failed" ? 1 : 0;
      }
      upsertDocProgress(docs, row);
    }
    else if (ev.event === "embed_progress") embed = { done: ev.done, total: ev.total, batchMs: ev.batch_ms };
    else if (ev.event === "job_complete") {
      countsByTier = ev.counts_by_tier;
      indexStates = ev.index_states;
    }
  }
  return { docs, embed, countsByTier, indexStates };
}
