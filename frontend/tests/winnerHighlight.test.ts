/**
 * Winner-highlighting + Leaderboard scope tests — run with `npm test`.
 * Covers `bestRows(rows, metric)` direction per metric, ties, nulls, empty tables, failed/unscored exclusion,
 * localStorage metric parsing, and Leaderboard `scope` options + query string builder.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { leaderboardSearch } from "../src/lib/api";
import {
  DEFAULT_HIGHLIGHT_METRIC,
  HIGHLIGHT_METRIC_STORAGE_KEY,
  bestEngineSet,
  bestRows,
  highlightAriaLabel,
  highlightBadgeText,
  parseHighlightMetric,
  summaryTextForMetric,
} from "../src/lib/winnerHighlight";
import type { ScoreRow } from "../src/features/compare/compareModel";
import { effectiveScope, scopeOptions } from "../src/pages/leaderboardModel";
import type { LeaderboardRow } from "../src/types/arena";

const lbRow = (engine: string, over: Partial<LeaderboardRow> = {}): LeaderboardRow => ({
  engine,
  level_label: engine,
  runs: 5,
  failures: 0,
  avg_battle_score: 70,
  avg_qa_relevance: 0.7,
  avg_groundedness: 0.7,
  avg_context_precision: 0.7,
  avg_latency_ms: 4000,
  avg_cost_usd: 0.04,
  ...over,
});

const scoreRow = (engine: string, over: Partial<ScoreRow> = {}): ScoreRow => ({
  engine,
  short: engine,
  label: engine,
  status: "scored",
  battleScore: 70,
  breakdown: null,
  groundedness: 0.7,
  qaRelevance: 0.7,
  contextPrecision: 0.7,
  latencyMs: 4000,
  costUsd: 0.04,
  judgeModel: "claude-opus-5-5",
  error: null,
  rank: 2,
  ...over,
});

test("bestRows direction per metric: quality metrics pick max; latency and run cost pick min", () => {
  const rows: LeaderboardRow[] = [
    lbRow("l1", { avg_battle_score: 82.5, avg_qa_relevance: 0.85, avg_groundedness: 0.95, avg_context_precision: 0.6, avg_latency_ms: 1200, avg_cost_usd: 0.008 }),
    lbRow("l2", { avg_battle_score: 88.1, avg_qa_relevance: 0.92, avg_groundedness: 0.90, avg_context_precision: 0.88, avg_latency_ms: 3400, avg_cost_usd: 0.015 }),
    lbRow("l4", { avg_battle_score: 84.0, avg_qa_relevance: 0.98, avg_groundedness: 0.91, avg_context_precision: 0.80, avg_latency_ms: 14000, avg_cost_usd: 0.065 }),
  ];
  assert.deepEqual(bestRows(rows, "battle_score").map((r) => r.engine), ["l2"]);
  assert.deepEqual(bestRows(rows, "qa_relevance").map((r) => r.engine), ["l4"]);
  assert.deepEqual(bestRows(rows, "groundedness").map((r) => r.engine), ["l1"]);
  assert.deepEqual(bestRows(rows, "context_precision").map((r) => r.engine), ["l2"]);
  assert.deepEqual(bestRows(rows, "latency_ms").map((r) => r.engine), ["l1"]);
  assert.deepEqual(bestRows(rows, "cost_usd").map((r) => r.engine), ["l1"]);
});

test("bestRows highlights every tied row and handles empty or all-null tables", () => {
  assert.deepEqual(bestRows([], "battle_score"), []);
  const tied = [
    lbRow("l1", { avg_qa_relevance: 0.95, avg_latency_ms: 2100 }),
    lbRow("l2", { avg_qa_relevance: 0.95, avg_latency_ms: 2100 }),
    lbRow("l3", { avg_qa_relevance: 0.80, avg_latency_ms: 3500 }),
  ];
  assert.deepEqual(bestRows(tied, "qa_relevance").map((r) => r.engine), ["l1", "l2"]);
  assert.deepEqual(bestRows(tied, "latency_ms").map((r) => r.engine), ["l1", "l2"]);
  assert.deepEqual([...bestEngineSet(tied, "qa_relevance")], ["l1", "l2"]);

  const nulls = [lbRow("l1", { avg_battle_score: null }), lbRow("l2", { avg_battle_score: undefined })];
  assert.deepEqual(bestRows(nulls, "battle_score"), []);
});

test("bestRows never selects rows with failures or zero scored runs on Leaderboard or Compare", () => {
  const lb = [
    lbRow("flaky_fast", { avg_battle_score: 99.0, avg_latency_ms: 200, avg_cost_usd: 0.001, failures: 1 }),
    lbRow("empty", { avg_battle_score: 95.0, avg_latency_ms: 100, runs: 0, failures: 0 }),
    lbRow("clean", { avg_battle_score: 81.2, avg_latency_ms: 2800, avg_cost_usd: 0.02, failures: 0 }),
  ];
  assert.deepEqual(bestRows(lb, "battle_score").map((r) => r.engine), ["clean"]);
  assert.deepEqual(bestRows(lb, "latency_ms").map((r) => r.engine), ["clean"]);
  assert.deepEqual(bestRows(lb, "cost_usd").map((r) => r.engine), ["clean"]);

  const cmp = [
    scoreRow("failed_fast", { status: "failed", battleScore: null, latencyMs: 100, costUsd: 0.001 }),
    scoreRow("unscored_cheap", { status: "unscored", battleScore: null, latencyMs: 150, costUsd: 0.002 }),
    scoreRow("running", { status: "running", battleScore: null, latencyMs: 120, costUsd: 0.002 }),
    scoreRow("clean_winner", { status: "scored", battleScore: 84.3, latencyMs: 2400, costUsd: 0.019, rank: 1 }),
  ];
  assert.deepEqual(bestRows(cmp, "battle_score").map((r) => r.engine), ["clean_winner"]);
  assert.deepEqual(bestRows(cmp, "latency_ms").map((r) => r.engine), ["clean_winner"]);
  assert.deepEqual(bestRows(cmp, "cost_usd").map((r) => r.engine), ["clean_winner"]);
});

test("parseHighlightMetric, badge/aria labels, and Compare summaryTextForMetric", () => {
  assert.equal(HIGHLIGHT_METRIC_STORAGE_KEY, "rag-arena.highlight-metric");
  assert.equal(parseHighlightMetric(null), DEFAULT_HIGHLIGHT_METRIC);
  assert.equal(parseHighlightMetric("invalid"), "battle_score");
  assert.equal(parseHighlightMetric("cost_usd"), "cost_usd");
  assert.equal(highlightBadgeText("latency_ms"), "Best · latency");
  assert.equal(highlightAriaLabel("qa_relevance"), "Best by QA relevance");

  const cmp = [
    scoreRow("l1", { short: "Level 1", label: "Naive", latencyMs: 1500, costUsd: 0.005, battleScore: 72.0, rank: 2 }),
    scoreRow("l2", { short: "Level 2", label: "Hybrid", latencyMs: 3200, costUsd: 0.012, battleScore: 86.4, rank: 1 }),
  ];
  assert.match(summaryTextForMetric(cmp, "battle_score"), /Best Battle Score: Level 2 · Hybrid · 86\.4/);
  assert.match(summaryTextForMetric(cmp, "latency_ms"), /Best latency: Level 1 · Naive/);
});

test("Leaderboard scope options and query builder separate golden set from uploads", () => {
  assert.equal(leaderboardSearch({ source: "query_runs", scope: "uploads" }), "?source=query_runs&scope=uploads");
  assert.equal(leaderboardSearch({ source: "recordings", scope: "golden" }), "?source=recordings&scope=golden");

  const replayOpts = scopeOptions("replay", "recordings");
  assert.equal(replayOpts.find((o) => o.value === "uploads")?.disabled, true);
  assert.equal(effectiveScope("uploads", "replay", "recordings"), "golden");

  const recOpts = scopeOptions("live", "recordings");
  assert.equal(recOpts.find((o) => o.value === "uploads")?.disabled, true);
  assert.equal(effectiveScope("uploads", "live", "recordings"), "golden");

  const liveOpts = scopeOptions("live", "query_runs");
  assert.equal(liveOpts.find((o) => o.value === "uploads")?.disabled, false);
  assert.equal(effectiveScope("uploads", "live", "query_runs"), "uploads");
});
