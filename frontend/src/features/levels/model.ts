/**
 * Pure reducer: append-only event log → per-engine stage timeline.
 * No timers, no inference; every field comes from an event (spec §7.2, P1).
 * Shared by battle streams (run_id) and ingest job streams (job_id).
 */

import type {
  AgentTurnRecord,
  BattleEvent,
  EngineTimeline,
  IngestEvent,
  StageState,
} from "../../types/arena";
import { parseTs } from "../../lib/format";

export type TimelineEvent = BattleEvent | IngestEvent;

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
  /** True once any frame carried `replayed: true`. */
  readonly replayed: boolean;
  readonly events: readonly TimelineEvent[];
  readonly timelines: TimelineMap;
  readonly warnings: readonly string[];
  readonly complete: boolean;
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
  replayed: false,
  events: [],
  timelines: {},
  warnings: [],
  complete: false,
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

/** Apply one event. Unknown event types are appended to the log but otherwise ignored. */
export function reduceStreamEvent(state: StreamState, ev: TimelineEvent): StreamState {
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
      const turn: AgentTurnRecord = {
        turnIndex: ev.turn_index,
        toolName: ev.tool_name,
        toolArgs: ev.tool_args ?? {},
        resultSummary: ev.tool_result_summary,
        durationMs: ev.duration_ms,
        ts: ev.ts,
      };
      return withTimeline(base, { ...tl, agentTurns: [...tl.agentTurns, turn] });
    }
    case "answer_complete": {
      const tl = getTimeline(base, ev.engine);
      return withTimeline(base, {
        ...tl,
        answer: {
          answer: ev.answer,
          citations: ev.citations ?? [],
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
        enginesCompleted: ev.engines_completed ?? [],
        enginesFailed: ev.engines_failed ?? [],
      };
    case "job_complete":
      return { ...base, complete: true };
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
  readonly docs: readonly { doc_name: string; pages: number; chunks: number }[];
  readonly embed: { done: number; total: number; batchMs: number } | null;
  readonly countsByTier: Record<string, number> | null;
  readonly indexStates: Record<string, string> | null;
}

export function deriveIngestProgress(events: readonly TimelineEvent[]): IngestProgress {
  const docs: { doc_name: string; pages: number; chunks: number }[] = [];
  let embed: IngestProgress["embed"] = null;
  let countsByTier: Record<string, number> | null = null;
  let indexStates: Record<string, string> | null = null;
  for (const ev of events) {
    if (ev.event === "doc_progress") docs.push({ doc_name: ev.doc_name, pages: ev.pages, chunks: ev.chunks });
    else if (ev.event === "embed_progress") embed = { done: ev.done, total: ev.total, batchMs: ev.batch_ms };
    else if (ev.event === "job_complete") {
      countsByTier = ev.counts_by_tier;
      indexStates = ev.index_states;
    }
  }
  return { docs, embed, countsByTier, indexStates };
}
