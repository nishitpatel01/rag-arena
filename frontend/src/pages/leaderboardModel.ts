/**
 * Pure leaderboard shaping + split Engine vs. Judge economics helpers (no React).
 * The server returns one shape for both sources (`groups[{synthesis_model, judge_model, rows}]`);
 * rows are shown exactly as aggregated — never averaged across pairs or judges.
 */

import type {
  EngineTimeline,
  LeaderboardGroupResponse,
  LeaderboardProvenance,
  LeaderboardResponse,
  LeaderboardRow,
  LeaderboardScope,
  LeaderboardSource,
  ScoreBreakdown,
} from "../types/arena";

export interface LeaderboardGroup {
  readonly key: string;
  readonly synthesisModel: string;
  readonly judgeModel: string;
  readonly pairKnown: boolean;
  readonly note: string | null;
  readonly rows: readonly RankedRow[];
}

export interface RankedRow {
  /** 1-based rank among scored rows of the pair; null when the row has no scored run. */
  readonly rank: number | null;
  readonly row: LeaderboardRow;
}

export interface SourceOption {
  readonly value: LeaderboardSource;
  readonly label: string;
  readonly disabled: boolean;
  readonly reason: string | null;
}

export interface ScopeOption {
  readonly value: LeaderboardScope;
  readonly label: string;
  readonly disabled: boolean;
  readonly reason: string | null;
}

export interface SplitCostBreakdown {
  readonly engineCostUsd: number | null;
  readonly judgeCostUsd: number | null;
  readonly totalCostUsd: number | null;
}

export const VERTEX_GENAI_EVAL_MODEL_ID = "vertex-genai-eval";

const MAX_GROUPS = 50;
const MAX_ROWS_PER_GROUP = 50;
export const LIVE_SOURCE_DISABLED_REASON =
  "Live runs come from BigQuery query_runs, which replay mode never reads (DEMO_MODE=replay). Recordings are shown instead.";
export const BATTLE_SCORE_FOOTNOTE =
  "Battle Score = 40% QA relevance + 35% groundedness + 15% context precision + 10% efficiency, computed per run " +
  "on the server and averaged per row. Efficiency = 1 − (latency ÷ 60 s) × 0.5 − min(0.5, engine cost ÷ $0.20 × 0.5), " +
  "clamped to 0–1. Efficiency scores operational Engine Cost only (Judge Cost is reported separately so expensive autoraters " +
  "never penalize fast architectures). Scores are only comparable within one (synthesis, judge) pair.";

const isNonEmpty = (v: unknown): v is string => typeof v === "string" && v.trim().length > 0;
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isRow = (r: unknown): r is LeaderboardRow =>
  typeof r === "object" && r !== null && isNonEmpty((r as { engine?: unknown }).engine);
const roundCost = (v: number): number => Number(Math.max(0, v).toFixed(8));

/** True when `judgeModel` is the managed Vertex AI GenAI Evaluation Service (`:evaluateInstances`). */
export function isManagedEvalService(judgeModel: string | null | undefined): boolean {
  return typeof judgeModel === "string" && judgeModel.trim() === VERTEX_GENAI_EVAL_MODEL_ID;
}

/** Resolve operational Engine Cost, external Judge Cost, and Total Run Cost for a LeaderboardRow. */
export function resolveLeaderboardRowCosts(row: LeaderboardRow): SplitCostBreakdown {
  const totalCostUsd = isNum(row.avg_cost_usd) ? row.avg_cost_usd : null;
  const judgeCostUsd = isNum(row.avg_judge_cost_usd)
    ? row.avg_judge_cost_usd
    : isNum(row.judge_cost_usd)
      ? row.judge_cost_usd
      : null;
  const explicitEngine = isNum(row.avg_engine_cost_usd)
    ? row.avg_engine_cost_usd
    : isNum(row.engine_cost_usd)
      ? row.engine_cost_usd
      : null;

  if (explicitEngine !== null) {
    const resolvedTotal =
      totalCostUsd !== null ? totalCostUsd : judgeCostUsd !== null ? roundCost(explicitEngine + judgeCostUsd) : explicitEngine;
    return { engineCostUsd: explicitEngine, judgeCostUsd, totalCostUsd: resolvedTotal };
  }
  if (totalCostUsd === null) {
    return { engineCostUsd: null, judgeCostUsd, totalCostUsd: null };
  }
  if (judgeCostUsd !== null) {
    return { engineCostUsd: roundCost(totalCostUsd - judgeCostUsd), judgeCostUsd, totalCostUsd };
  }
  return { engineCostUsd: totalCostUsd, judgeCostUsd: null, totalCostUsd };
}

/** Resolve operational Engine Cost, external Judge Cost, and Total Run Cost for a Compare ScoreRow. */
export function resolveScoreboardCosts(row: {
  readonly costUsd: number | null;
  readonly breakdown?: ScoreBreakdown | null;
}): SplitCostBreakdown {
  const b = row.breakdown ?? null;
  if (b !== null) {
    const engineCostUsd = isNum(b.engine_cost_usd) ? b.engine_cost_usd : isNum(b.cost_usd) ? b.cost_usd : null;
    if (engineCostUsd !== null) {
      const judgeCostUsd = isNum(b.judge_cost_usd)
        ? b.judge_cost_usd
        : isNum(row.costUsd)
          ? roundCost(row.costUsd - engineCostUsd)
          : null;
      const totalCostUsd = isNum(row.costUsd)
        ? row.costUsd
        : judgeCostUsd !== null
          ? roundCost(engineCostUsd + judgeCostUsd)
          : engineCostUsd;
      return { engineCostUsd, judgeCostUsd, totalCostUsd };
    }
  }
  if (isNum(row.costUsd)) {
    return { engineCostUsd: row.costUsd, judgeCostUsd: null, totalCostUsd: row.costUsd };
  }
  return { engineCostUsd: null, judgeCostUsd: null, totalCostUsd: null };
}

/** Resolve operational Engine Cost, external Judge Cost, and Total Run Cost from a live/replayed EngineTimeline. */
export function resolveTimelineCosts(timeline: EngineTimeline | undefined): SplitCostBreakdown {
  if (!timeline) return { engineCostUsd: null, judgeCostUsd: null, totalCostUsd: null };
  const engineCostUsd = isNum(timeline.answer?.costUsd)
    ? timeline.answer.costUsd
    : isNum(timeline.evaluation?.engineCostUsd)
      ? timeline.evaluation.engineCostUsd
      : isNum(timeline.evaluation?.scoreBreakdown?.cost_usd)
        ? timeline.evaluation.scoreBreakdown.cost_usd
        : null;
  const judgeCostUsd = isNum(timeline.evaluation?.judgeCostUsd) ? timeline.evaluation.judgeCostUsd : null;
  const totalCostUsd = isNum(timeline.evaluation?.runCostUsd)
    ? timeline.evaluation.runCostUsd
    : isNum(timeline.evaluation?.totalCostUsd)
      ? timeline.evaluation.totalCostUsd
      : engineCostUsd !== null && judgeCostUsd !== null
        ? roundCost(engineCostUsd + judgeCostUsd)
        : engineCostUsd;
  return { engineCostUsd, judgeCostUsd, totalCostUsd };
}

function validGroups(res: LeaderboardResponse | null | undefined): LeaderboardGroupResponse[] {
  if (!res || typeof res !== "object" || !Array.isArray(res.groups)) return [];
  return res.groups
    .filter((g) => typeof g === "object" && g !== null && isNonEmpty(g.synthesis_model) && isNonEmpty(g.judge_model))
    .slice(0, MAX_GROUPS);
}

/** Flat rows carrying their pair (for Compare cost estimates and the level-page teaser). */
export function leaderboardRows(res: LeaderboardResponse | null | undefined): LeaderboardRow[] {
  return validGroups(res).flatMap((g) =>
    (Array.isArray(g.rows) ? g.rows : [])
      .filter(isRow)
      .slice(0, MAX_ROWS_PER_GROUP)
      .map((r) => ({ ...r, synthesis_model: g.synthesis_model, judge_model: g.judge_model }))
  );
}

/** Rank = position among rows with a numeric Battle Score, in server order (already sorted desc). */
export function rankRows(rows: readonly LeaderboardRow[]): RankedRow[] {
  let next = 1;
  return rows.map((row) => ({ row, rank: isNum(row.avg_battle_score) ? next++ : null }));
}

export function groupLeaderboard(res: LeaderboardResponse | null | undefined): LeaderboardGroup[] {
  return validGroups(res).map((g) => ({
    key: `${g.synthesis_model}|${g.judge_model}`,
    synthesisModel: g.synthesis_model,
    judgeModel: g.judge_model,
    pairKnown: g.pair_known !== false,
    note: isNonEmpty(g.note) ? g.note : null,
    rows: rankRows((Array.isArray(g.rows) ? g.rows : []).filter(isRow).slice(0, MAX_ROWS_PER_GROUP)),
  }));
}

/** Server default mirrored client-side: replay (or unknown mode) → recordings; live/record → query_runs. */
export function defaultSource(demoMode: string | null | undefined): LeaderboardSource {
  return demoMode === "live" || demoMode === "record" ? "query_runs" : "recordings";
}

export function sourceOptions(demoMode: string | null | undefined): SourceOption[] {
  const liveAllowed = demoMode === "live" || demoMode === "record";
  return [
    { value: "recordings", label: "Recordings", disabled: false, reason: null },
    { value: "query_runs", label: "Live runs", disabled: !liveAllowed, reason: liveAllowed ? null : LIVE_SOURCE_DISABLED_REASON },
  ];
}

export const UPLOADS_SCOPE_REPLAY_REASON =
  "Uploads are available only on the live service; replay mode has no upload runs.";
export const UPLOADS_SCOPE_RECORDINGS_REASON =
  "Recordings cover the 8-question golden set only; switch Source to Live runs to view uploaded document runs.";

export function scopeOptions(
  demoMode: string | null | undefined,
  source: LeaderboardSource | string
): ScopeOption[] {
  const liveAllowed = demoMode === "live" || demoMode === "record";
  const uploadsReason = !liveAllowed
    ? UPLOADS_SCOPE_REPLAY_REASON
    : source !== "query_runs"
      ? UPLOADS_SCOPE_RECORDINGS_REASON
      : null;
  return [
    { value: "golden", label: "Golden set", disabled: false, reason: null },
    { value: "uploads", label: "Uploads", disabled: uploadsReason !== null, reason: uploadsReason },
  ];
}

export function effectiveScope(
  picked: LeaderboardScope | string | null | undefined,
  demoMode: string | null | undefined,
  source: LeaderboardSource | string
): LeaderboardScope {
  const liveAllowed = demoMode === "live" || demoMode === "record";
  if (liveAllowed && source === "query_runs" && picked === "uploads") return "uploads";
  return "golden";
}

const plural = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;
const day = (iso: string): string => iso.slice(0, 10);

/** One-line provenance shown under each row. */
export function provenanceSummary(p: LeaderboardProvenance | null | undefined): string {
  if (!p || typeof p !== "object") return "provenance not reported";
  const shas = Array.isArray(p.git_shas) ? p.git_shas.length : 0;
  const shaText = plural(shas, "git SHA", "git SHAs");
  if (p.kind === "recording") {
    const files = isNum(p.files) ? plural(p.files, "recording file", "recording files") : "recording files";
    const range = p.recorded_at ? `recorded ${day(p.recorded_at.first)} → ${day(p.recorded_at.last)}` : "recorded_at not reported";
    return `${files} · ${range} · ${shaText}`;
  }
  if (p.kind === "query_runs") {
    const modes = Object.entries(p.demo_modes ?? {}).map(([m, n]) => `${m} ${n}`).join(", ") || "no demo_mode";
    return `${p.table ?? "query_runs"} · ${modes} · ${shaText}`;
  }
  return `provenance kind ${String(p.kind)}`;
}

/** Expanded provenance: label/value pairs (full git SHAs, exact timestamps, per-mode counts). */
export function provenanceDetails(p: LeaderboardProvenance | null | undefined): [string, string][] {
  if (!p || typeof p !== "object") return [];
  const out: [string, string][] = [["source", String(p.kind)]];
  if (isNum(p.files)) out.push(["files", String(p.files)]);
  if (p.recorded_at) out.push(["recorded_at", `${p.recorded_at.first} → ${p.recorded_at.last}`]);
  if (isNonEmpty(p.table)) out.push(["table", p.table]);
  for (const [mode, n] of Object.entries(p.demo_modes ?? {})) out.push([`demo_mode ${mode}`, String(n)]);
  out.push(["git SHAs", Array.isArray(p.git_shas) && p.git_shas.length > 0 ? p.git_shas.join(", ") : "none recorded"]);
  return out;
}

/** "0.873" for 0–1 metrics; "—" when absent. */
export function formatMetric(v: unknown): string {
  return isNum(v) ? v.toFixed(3) : "—";
}

export function formatBattleScore(v: unknown): string {
  return isNum(v) ? v.toFixed(1) : "—";
}
