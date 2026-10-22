/**
 * Pure shaping for the "Same question on Level 1" teaser (no React, no fetching).
 *
 * Read-only: it never starts runs. Facts come from GET /api/v1/models (`recorded_pairs`: is the same
 * question recorded on the baseline level for the same model pair?) and GET /api/v1/leaderboard (the
 * baseline level's aggregate row for that pair). Leaderboard rows are averages over all recorded runs of
 * the pair — the UI must label them as such; per-question comparison is the Phase 6 Compare page.
 */

import type { LeaderboardResponse, LeaderboardRow, ModelsResponse } from "../../types/arena";
import { leaderboardRows } from "../../pages/leaderboardModel";

export const BASELINE_ENGINE = "level1_naive";

export interface BaselineLookup {
  readonly engine: string;
  readonly synthesisModel: string | null;
  readonly judgeModel: string | null;
  readonly questionId: string | null;
}

export interface TeaserMetric {
  readonly key: string;
  readonly label: string;
  /** "score" (0–1, 2 decimals) or "ms". */
  readonly unit: "score" | "ms";
  readonly current: number | null;
  readonly baseline: number | null;
}

export interface BaselineView {
  /** Both model ids of the current run are known (from `run_started`). */
  readonly pairKnown: boolean;
  /** `recorded_pairs` has (engine, synthesis, judge) at all. */
  readonly pairRecorded: boolean;
  /** …and its `question_ids` include the current question. */
  readonly questionRecorded: boolean;
  readonly row: LeaderboardRow | null;
  readonly runs: number | null;
  /** Provenance entries flagged `synthetic_dev_fixture: true` inside the baseline row (shown as a caveat). */
  readonly syntheticRuns: number;
}

export interface CurrentRunMetrics {
  readonly groundedness: number | null;
  readonly qaRelevance: number | null;
  readonly contextPrecision: number | null;
  readonly latencyMs: number | null;
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function baselineFor(
  catalog: ModelsResponse | null,
  leaderboard: LeaderboardResponse | null,
  lookup: BaselineLookup
): BaselineView {
  const { engine, synthesisModel, judgeModel } = lookup;
  const questionId = lookup.questionId?.trim() ?? "";
  const pairKnown = Boolean(synthesisModel && judgeModel);
  if (!pairKnown) return { pairKnown, pairRecorded: false, questionRecorded: false, row: null, runs: null, syntheticRuns: 0 };
  const pairs = (catalog?.recorded_pairs ?? []).filter(
    (p) => p.engine === engine && p.synthesis_model === synthesisModel && p.judge_model === judgeModel
  );
  const pairRecorded = pairs.length > 0;
  const questionRecorded = questionId.length > 0 && pairs.some((p) => p.question_ids.includes(questionId));
  const row =
    leaderboardRows(leaderboard).find(
      (r) => r.engine === engine && r.synthesis_model === synthesisModel && r.judge_model === judgeModel
    ) ?? null;
  const runs = row && isNum(row.runs) ? row.runs : null;
  const syntheticRuns = (row?.provenance ?? []).filter((p) => p.synthetic_dev_fixture === true).length;
  return { pairKnown, pairRecorded, questionRecorded, row, runs, syntheticRuns };
}

/** Rows of the teaser table: current run value vs the baseline pair's server-side average. */
export function teaserMetrics(view: BaselineView, current: CurrentRunMetrics): TeaserMetric[] {
  const avg = (k: string): number | null => (view.row && isNum(view.row[k]) ? (view.row[k] as number) : null);
  return [
    { key: "groundedness", label: "Groundedness", unit: "score", current: current.groundedness, baseline: avg("avg_groundedness") },
    { key: "qa_relevance", label: "QA relevance", unit: "score", current: current.qaRelevance, baseline: avg("avg_qa_relevance") },
    { key: "context_precision", label: "Context precision", unit: "score", current: current.contextPrecision, baseline: avg("avg_context_precision") },
    { key: "latency", label: "Latency", unit: "ms", current: current.latencyMs, baseline: avg("avg_latency_ms") },
  ];
}

// ---------------------------------------------------------------------------
// Generalised teaser (Level N vs every lower enabled level)
// ---------------------------------------------------------------------------

export interface LowerLevel {
  readonly engine: string;
  /** "Level 1", "Level 2", … (from the descriptor order). */
  readonly short: string;
  readonly order: number;
}

/** Bound on teaser columns (there are at most 5 levels). */
const MAX_LOWER_LEVELS = 10;

/** Enabled levels strictly below `current` (by `order`), ascending; empty for Level 1. */
export function lowerEnabledLevels(
  levels: readonly { readonly level_id: string; readonly order: number; readonly enabled: boolean }[],
  current: { readonly order: number }
): LowerLevel[] {
  return levels
    .filter((l) => l.enabled && Number.isFinite(l.order) && l.order < current.order)
    .sort((a, b) => a.order - b.order)
    .slice(0, MAX_LOWER_LEVELS)
    .map((l) => ({ engine: l.level_id, short: `Level ${l.order}`, order: l.order }));
}

/** "Same question on Level 1" / "Same question on Levels 1–2" / "… on Levels 1, 3". */
export function teaserTitle(lower: readonly LowerLevel[]): string {
  if (lower.length === 0) return "Same question on lower levels";
  if (lower.length === 1) return `Same question on ${lower[0].short}`;
  const orders = lower.map((l) => l.order);
  const contiguous = orders.every((o, i) => i === 0 || o === orders[i - 1] + 1);
  return `Same question on Levels ${contiguous ? `${orders[0]}–${orders[orders.length - 1]}` : orders.join(", ")}`;
}

export interface BaselineEntry {
  readonly level: LowerLevel;
  readonly view: BaselineView;
}

export function baselinesFor(
  catalog: ModelsResponse | null,
  leaderboard: LeaderboardResponse | null,
  lower: readonly LowerLevel[],
  lookup: Omit<BaselineLookup, "engine">
): BaselineEntry[] {
  return lower.map((level) => ({ level, view: baselineFor(catalog, leaderboard, { ...lookup, engine: level.engine }) }));
}

export interface TeaserMatrixRow {
  readonly key: string;
  readonly label: string;
  readonly unit: "score" | "ms";
  readonly current: number | null;
  /** One value per entry of `baselinesFor` (same order); null when that level has no row / metric. */
  readonly baselines: readonly (number | null)[];
}

/** Metric rows: this run vs each lower level's server-side pair average. */
export function teaserMatrix(entries: readonly BaselineEntry[], current: CurrentRunMetrics): TeaserMatrixRow[] {
  const perLevel = entries.map((e) => teaserMetrics(e.view, current));
  const template = teaserMetrics({ pairKnown: false, pairRecorded: false, questionRecorded: false, row: null, runs: null, syntheticRuns: 0 }, current);
  return template.map((m, i) => ({ key: m.key, label: m.label, unit: m.unit, current: m.current, baselines: perLevel.map((rows) => rows[i].baseline) }));
}
