/**
 * Pure winner-row highlighting logic shared by Leaderboard and Compare Scoreboard.
 *
 * Quality metrics (Battle Score, QA relevance, groundedness, context precision) pick highest;
 * efficiency metrics (latency, run cost) pick lowest. Ties highlight every tied row.
 * Rows with failures or zero scored runs are never eligible.
 */

import type { ScoreRow } from "../features/compare/compareModel";
import type { LeaderboardRow } from "../types/arena";

export type HighlightMetric =
  | "battle_score"
  | "qa_relevance"
  | "groundedness"
  | "context_precision"
  | "latency_ms"
  | "cost_usd";

export interface HighlightMetricOption {
  readonly value: HighlightMetric;
  readonly label: string;
  readonly shortLabel: string;
  readonly direction: "max" | "min";
}

export const HIGHLIGHT_METRIC_STORAGE_KEY = "rag-arena.highlight-metric";
export const DEFAULT_HIGHLIGHT_METRIC: HighlightMetric = "battle_score";
const TIE_EPSILON = 1e-12;

export const HIGHLIGHT_METRIC_OPTIONS: readonly HighlightMetricOption[] = [
  { value: "battle_score", label: "Battle Score (highest)", shortLabel: "Battle Score", direction: "max" },
  { value: "qa_relevance", label: "QA relevance (highest)", shortLabel: "QA relevance", direction: "max" },
  { value: "groundedness", label: "Groundedness (highest)", shortLabel: "groundedness", direction: "max" },
  { value: "context_precision", label: "Context precision (highest)", shortLabel: "context precision", direction: "max" },
  { value: "latency_ms", label: "Latency (lowest)", shortLabel: "latency", direction: "min" },
  { value: "cost_usd", label: "Run cost (lowest)", shortLabel: "run cost", direction: "min" },
] as const;

const METRIC_BY_VALUE: Readonly<Record<HighlightMetric, HighlightMetricOption>> = Object.fromEntries(
  HIGHLIGHT_METRIC_OPTIONS.map((opt) => [opt.value, opt])
) as Record<HighlightMetric, HighlightMetricOption>;

const isFiniteNumber = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function parseHighlightMetric(raw: string | null | undefined): HighlightMetric {
  if (typeof raw === "string" && Object.prototype.hasOwnProperty.call(METRIC_BY_VALUE, raw)) {
    return raw as HighlightMetric;
  }
  return DEFAULT_HIGHLIGHT_METRIC;
}

export function highlightOption(metric: HighlightMetric): HighlightMetricOption {
  return METRIC_BY_VALUE[metric];
}

export function highlightBadgeText(metric: HighlightMetric): string {
  return `Best · ${METRIC_BY_VALUE[metric].shortLabel}`;
}

export function highlightAriaLabel(metric: HighlightMetric): string {
  return `Best by ${METRIC_BY_VALUE[metric].shortLabel}`;
}

function isScoreRow(row: LeaderboardRow | ScoreRow): row is ScoreRow {
  return typeof (row as ScoreRow).status === "string" && "battleScore" in row;
}

function isRowEligible(row: LeaderboardRow | ScoreRow): boolean {
  if (isScoreRow(row)) {
    return row.status === "scored" && isFiniteNumber(row.battleScore);
  }
  const runs = isFiniteNumber(row.runs) ? row.runs : 0;
  const failures = isFiniteNumber(row.failures) ? row.failures : 0;
  return runs > 0 && failures === 0 && isFiniteNumber(row.avg_battle_score);
}

function extractMetricValue(row: LeaderboardRow | ScoreRow, metric: HighlightMetric): number | null {
  if (isScoreRow(row)) {
    switch (metric) {
      case "battle_score":
        return isFiniteNumber(row.battleScore) ? row.battleScore : null;
      case "qa_relevance":
        return isFiniteNumber(row.qaRelevance) ? row.qaRelevance : null;
      case "groundedness":
        return isFiniteNumber(row.groundedness) ? row.groundedness : null;
      case "context_precision":
        return isFiniteNumber(row.contextPrecision) ? row.contextPrecision : null;
      case "latency_ms":
        return isFiniteNumber(row.latencyMs) ? row.latencyMs : null;
      case "cost_usd":
        return isFiniteNumber(row.costUsd) ? row.costUsd : null;
    }
  }
  switch (metric) {
    case "battle_score":
      return isFiniteNumber(row.avg_battle_score) ? row.avg_battle_score : null;
    case "qa_relevance":
      return isFiniteNumber(row.avg_qa_relevance) ? row.avg_qa_relevance : null;
    case "groundedness":
      return isFiniteNumber(row.avg_groundedness) ? row.avg_groundedness : null;
    case "context_precision":
      return isFiniteNumber(row.avg_context_precision) ? row.avg_context_precision : null;
    case "latency_ms":
      return isFiniteNumber(row.avg_latency_ms) ? row.avg_latency_ms : null;
    case "cost_usd":
      return isFiniteNumber(row.avg_cost_usd) ? row.avg_cost_usd : null;
  }
}

/** Winning rows (every tied winner) for `metric`; empty when no eligible row has a finite value. */
export function bestRows<T extends LeaderboardRow | ScoreRow>(
  rows: readonly T[],
  metric: HighlightMetric = DEFAULT_HIGHLIGHT_METRIC
): T[] {
  const direction = METRIC_BY_VALUE[metric].direction;
  const candidates: Array<{ readonly row: T; readonly value: number }> = [];
  for (const row of rows) {
    if (!isRowEligible(row)) continue;
    const value = extractMetricValue(row, metric);
    if (value !== null) candidates.push({ row, value });
  }
  if (candidates.length === 0) return [];
  let target = candidates[0].value;
  for (let i = 1; i < candidates.length; i += 1) {
    const v = candidates[i].value;
    if (direction === "max" ? v > target : v < target) target = v;
  }
  return candidates.filter((c) => Math.abs(c.value - target) <= TIE_EPSILON).map((c) => c.row);
}

export function bestEngineSet(
  rows: readonly (LeaderboardRow | ScoreRow)[],
  metric: HighlightMetric = DEFAULT_HIGHLIGHT_METRIC
): ReadonlySet<string> {
  return new Set(bestRows(rows, metric).map((r) => r.engine));
}

function formatSummaryMetric(value: number, metric: HighlightMetric): string {
  switch (metric) {
    case "battle_score":
      return value.toFixed(1);
    case "qa_relevance":
    case "groundedness":
    case "context_precision":
      return value.toFixed(3);
    case "latency_ms":
      return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
    case "cost_usd":
      return `$${value.toFixed(4)}`;
  }
}

/** Compare summary banner text for the active highlight metric. */
export function summaryTextForMetric(
  rows: readonly ScoreRow[],
  metric: HighlightMetric = DEFAULT_HIGHLIGHT_METRIC
): string {
  const best = bestRows(rows, metric);
  const failed = rows.filter((r) => r.status === "failed").length;
  const tail = failed > 0 ? ` (${failed} failed level${failed === 1 ? "" : "s"} not ranked)` : "";
  const meta = METRIC_BY_VALUE[metric];
  if (best.length === 0) {
    return metric === "battle_score"
      ? `No level produced a Battle Score${tail}.`
      : `No scored level reported ${meta.shortLabel}${tail}.`;
  }
  const who = best.map((r) => `${r.short} · ${r.label}`).join(" and ");
  const val = extractMetricValue(best[0], metric) as number;
  return `Best ${meta.shortLabel}: ${who} · ${formatSummaryMetric(val, metric)}${best.length > 1 ? " (tie)" : ""}${tail}`;
}
