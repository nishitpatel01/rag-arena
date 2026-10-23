/**
 * Pure Compare-page rules (no React, no fetching): which enabled levels can run with the chosen
 * (synthesis, judge) pair, the "estimate from recordings" cost line, and the per-level scoreboard.
 *
 * Every fact comes from the server: levels (GET /levels), catalog + recorded pairs (GET /models),
 * recorded averages (GET /leaderboard) and the run's events (eval_complete.battle_score). The Battle
 * Score is never computed client-side — a level without a server score is shown unscored, not ranked.
 */

import type { LeaderboardResponse, LevelDescriptor, ModelsResponse, ScoreBreakdown } from "../../types/arena";
import { leaderboardRows } from "../../pages/leaderboardModel";
import type { StreamState } from "../levels/model";
import { findModel, modelLabel, validateSelection, type ModelSelection } from "../levels/modelSelection";

export const LEVEL4_ENGINE = "level4_agentic";
export const GEMINI_PROVIDER = "vertex_gemini";
export const MAX_COMPARE_LEVELS = 5;

export interface LevelChoice {
  readonly levelId: string;
  readonly label: string;
  /** "Level 1" … from the descriptor order. */
  readonly short: string;
  readonly order: number;
  /** Why this level cannot run with the current pair/question (null = runnable). */
  readonly disabledReason: string | null;
}

/** "Level N" from the descriptor order. */
export function shortLabel(order: number): string {
  return `Level ${order}`;
}

/** Descriptor label without a leading "Level N:" (the page prefixes `short` itself). */
export function levelName(label: string): string {
  const stripped = label.replace(/^level\s*\d+\s*[:·-]\s*/i, "").trim();
  return stripped.length > 0 ? stripped : label;
}

/** Level 4 runs its ADK agent on the synthesis model: Gemini only (the server answers HTTP 400 otherwise). */
function level4Reason(catalog: ModelsResponse, sel: ModelSelection, levelId: string): string | null {
  if (levelId !== LEVEL4_ENGINE) return null;
  const synth = findModel(catalog, sel.synthesis);
  if (!synth || synth.provider === GEMINI_PROVIDER) return null;
  return `Level 4 (ADK agent) requires a Gemini synthesis model; ${synth.label} is not Gemini.`;
}

/** Replay plays recordings only: the (engine, synthesis, judge) recording must contain the question. */
function replayReason(catalog: ModelsResponse, sel: ModelSelection, levelId: string, questionId: string): string | null {
  if (catalog.demo_mode !== "replay") return null;
  const q = questionId.trim();
  if (!q) return "Replay plays recordings only: choose a golden question (question_id).";
  const recorded = (catalog.recorded_pairs ?? []).some(
    (p) => p.engine === levelId && p.synthesis_model === sel.synthesis && p.judge_model === sel.judge && p.question_ids.includes(q)
  );
  return recorded ? null : `no recording for this pair (${modelLabel(catalog, sel.synthesis)} · judge ${modelLabel(catalog, sel.judge)}) and ${q}`;
}

/** Enabled levels in ladder order, each with the reason it cannot run (catalog rules → Level 4 → replay). */
export function levelChoices(
  levels: readonly LevelDescriptor[],
  catalog: ModelsResponse | null,
  sel: ModelSelection | null,
  questionId: string
): LevelChoice[] {
  return [...levels]
    .filter((l) => l.enabled && !l.dev_fixture)
    .sort((a, b) => a.order - b.order)
    .slice(0, MAX_COMPARE_LEVELS)
    .map((l) => {
      const base = { levelId: l.level_id, label: levelName(l.label), short: shortLabel(l.order), order: l.order };
      if (!catalog || !sel) return { ...base, disabledReason: "model catalog not loaded" };
      const reason = validateSelection(catalog, sel, l.level_id) ?? level4Reason(catalog, sel, l.level_id) ?? replayReason(catalog, sel, l.level_id, questionId);
      return { ...base, disabledReason: reason };
    });
}

/** Checked AND runnable levels, in ladder order (what the single POST sends as `engines`). */
export function enginesToRun(choices: readonly LevelChoice[], checked: ReadonlySet<string>): string[] {
  return choices.filter((c) => c.disabledReason === null && checked.has(c.levelId)).map((c) => c.levelId);
}

export interface CostEstimateLine {
  readonly engine: string;
  /** Average recorded run cost (engine + judge) for this pair, or null when nothing is recorded. */
  readonly usd: number | null;
  readonly runs: number | null;
}

export interface CostEstimate {
  readonly lines: readonly CostEstimateLine[];
  /** Sum over levels — null as soon as one level has no recordings for the pair ("unknown"). */
  readonly totalUsd: number | null;
  readonly unknownEngines: readonly string[];
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Live-mode estimate from the recordings' `avg_cost_usd` (run cost incl. judge) for the exact pair per level. */
export function costEstimate(leaderboard: LeaderboardResponse | null, engines: readonly string[], sel: ModelSelection | null): CostEstimate {
  const rows = leaderboardRows(leaderboard);
  const lines = engines.map<CostEstimateLine>((engine) => {
    const row = sel ? rows.find((r) => r.engine === engine && r.synthesis_model === sel.synthesis && r.judge_model === sel.judge) : undefined;
    const usd = row && isNum(row.avg_cost_usd) ? row.avg_cost_usd : null;
    return { engine, usd, runs: row && isNum(row.runs) ? row.runs : null };
  });
  const unknownEngines = lines.filter((l) => l.usd === null).map((l) => l.engine);
  const totalUsd = lines.length > 0 && unknownEngines.length === 0 ? lines.reduce((s, l) => s + (l.usd ?? 0), 0) : null;
  return { lines, totalUsd, unknownEngines };
}

export type ScoreStatus = "pending" | "running" | "scored" | "unscored" | "failed";

export interface ScoreRow {
  readonly engine: string;
  readonly short: string;
  readonly label: string;
  readonly status: ScoreStatus;
  readonly battleScore: number | null;
  readonly breakdown: ScoreBreakdown | null;
  readonly groundedness: number | null;
  readonly qaRelevance: number | null;
  readonly contextPrecision: number | null;
  readonly latencyMs: number | null;
  /** Run cost (engine + judge) from eval_complete, else the engine cost from answer_complete. */
  readonly costUsd: number | null;
  readonly judgeModel: string | null;
  readonly error: { readonly type: string; readonly message: string; readonly stage: string | null } | null;
  /** Competition rank among scored rows (1 = best; ties share a rank); null when not ranked. */
  readonly rank: number | null;
}

function failedStageOf(stages: StreamState["timelines"][string]["stages"]): string | null {
  for (const [id, st] of Object.entries(stages)) if (st.status === "failed") return id;
  return null;
}

function rowStatus(tl: StreamState["timelines"][string] | undefined, score: number | null): ScoreStatus {
  if (!tl) return "pending";
  if (tl.error) return "failed";
  if (tl.evaluation) return score === null ? "unscored" : "scored";
  if (failedStageOf(tl.stages)) return "failed";
  return "running";
}

function rowError(tl: StreamState["timelines"][string] | undefined): ScoreRow["error"] {
  if (!tl) return null;
  if (tl.error) return { type: tl.error.type, message: tl.error.message, stage: tl.error.failedStage ?? failedStageOf(tl.stages) };
  const stage = failedStageOf(tl.stages);
  const err = stage ? tl.stages[stage]?.error : null;
  return stage && err ? { type: err.type, message: err.message, stage } : null;
}

/** One row per included level (ladder order); only `scored` rows are ranked — failures never get a score. */
export function scoreboardRows(choices: readonly LevelChoice[], engines: readonly string[], state: StreamState): ScoreRow[] {
  const rows = choices
    .filter((c) => engines.includes(c.levelId))
    .map<ScoreRow>((c) => {
      const tl = state.timelines[c.levelId];
      const ev = tl?.evaluation ?? null;
      const score = ev?.battleScore ?? null;
      const status = rowStatus(tl, score);
      return {
        engine: c.levelId,
        short: c.short,
        label: c.label,
        status,
        battleScore: status === "scored" ? score : null,
        breakdown: status === "scored" ? ev?.scoreBreakdown ?? null : null,
        groundedness: ev?.metrics.groundedness ?? null,
        qaRelevance: ev?.metrics.qa_relevance ?? null,
        contextPrecision: ev?.metrics.context_precision ?? null,
        latencyMs: tl?.answer?.latencyMs ?? null,
        costUsd: ev?.runCostUsd ?? tl?.answer?.costUsd ?? null,
        judgeModel: ev?.judgeModel ?? null,
        error: status === "failed" ? rowError(tl) : null,
        rank: null,
      };
    });
  const scores = rows.filter((r) => r.status === "scored").map((r) => r.battleScore as number);
  return rows.map((r) => (r.status === "scored" ? { ...r, rank: 1 + scores.filter((s) => s > (r.battleScore as number)).length } : r));
}

/** Rank-1 rows (several on a tie); empty when nothing was scored. */
export function bestRows(rows: readonly ScoreRow[]): ScoreRow[] {
  return rows.filter((r) => r.rank === 1);
}

/** Sticky header text after completion, e.g. "Best Battle Score: Level 2 · BQ Hybrid · 81.4". */
export function summaryText(rows: readonly ScoreRow[]): string {
  const best = bestRows(rows);
  const failed = rows.filter((r) => r.status === "failed").length;
  const tail = failed > 0 ? ` (${failed} failed level${failed === 1 ? "" : "s"} not ranked)` : "";
  if (best.length === 0) return `No level produced a Battle Score${tail}.`;
  const who = best.map((r) => `${r.short} · ${r.label}`).join(" and ");
  return `Best Battle Score: ${who} · ${(best[0].battleScore as number).toFixed(1)}${best.length > 1 ? " (tie)" : ""}${tail}`;
}
