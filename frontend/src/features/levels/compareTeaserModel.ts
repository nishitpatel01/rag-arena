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
