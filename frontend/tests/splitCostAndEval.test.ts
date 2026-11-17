/**
 * TDD suite for Slice #4: Split Engine vs. Judge Economics + Vertex AI GenAI Evaluation Service UI contracts.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildPickerDetails,
  optionStates,
  validateSelection,
} from "../src/features/levels/modelSelection";
import { formatRunCostPill } from "../src/features/levels/RunPills";
import {
  BATTLE_SCORE_FOOTNOTE,
  isManagedEvalService,
  resolveLeaderboardRowCosts,
  resolveScoreboardCosts,
  resolveTimelineCosts,
} from "../src/pages/leaderboardModel";
import type {
  EngineTimeline,
  LeaderboardRow,
  ModelCatalogEntry,
  ModelsResponse,
} from "../src/types/arena";

const GENAI_EVAL_ENTRY: ModelCatalogEntry = {
  id: "vertex-genai-eval",
  label: "Vertex AI GenAI Eval Service",
  provider: "vertex_genai_eval",
  location: "us-central1",
  roles: ["judge"],
  input_usd_per_1m: 0.75,
  output_usd_per_1m: 3.75,
  price_source: "https://cloud.google.com/vertex-ai/generative-ai/pricing",
  price_as_of: "2026-10-21",
  available: true,
  unavailable_reason: null,
  verified_at: "2026-10-29T16:24:14Z",
};

const CATALOG_LIVE: ModelsResponse = {
  models: [
    {
      ...GENAI_EVAL_ENTRY,
      id: "gemini-3.8-flash",
      label: "Gemini 3.8 Flash",
      provider: "vertex_gemini",
      location: "global",
      roles: ["synthesis", "judge"],
    },
    {
      ...GENAI_EVAL_ENTRY,
      id: "claude-opus-5-5",
      label: "Claude Opus 5.5",
      provider: "anthropic_vertex",
      location: "global",
      roles: ["synthesis", "judge"],
      input_usd_per_1m: 4.0,
      output_usd_per_1m: 20.0,
    },
    GENAI_EVAL_ENTRY,
  ],
  defaults: { synthesis_model: "gemini-3.8-flash", judge_model: "claude-opus-5-5" },
  demo_mode: "live",
  recorded_pairs: [],
};

test("resolveLeaderboardRowCosts splits avg_engine_cost_usd, avg_judge_cost_usd, and fallback rows cleanly", () => {
  const splitRow: LeaderboardRow = {
    engine: "rag_engine",
    runs: 8,
    avg_engine_cost_usd: 0.011,
    avg_judge_cost_usd: 0.0366,
    avg_cost_usd: 0.0476,
  };
  const resolved = resolveLeaderboardRowCosts(splitRow);
  assert.equal(resolved.engineCostUsd, 0.011);
  assert.equal(resolved.judgeCostUsd, 0.0366);
  assert.equal(resolved.totalCostUsd, 0.0476);

  // Fallback when avg_engine_cost_usd is absent: total - judge clamped >= 0
  const fallbackRow: LeaderboardRow = {
    engine: "level2_hybrid",
    runs: 8,
    avg_judge_cost_usd: 0.0223,
    avg_cost_usd: 0.0308,
  };
  const fallback = resolveLeaderboardRowCosts(fallbackRow);
  assert.ok(Math.abs((fallback.engineCostUsd ?? 0) - 0.0085) < 1e-8);
  assert.equal(fallback.judgeCostUsd, 0.0223);
  assert.equal(fallback.totalCostUsd, 0.0308);

  // Legacy query_runs row with only avg_cost_usd
  const legacyRow: LeaderboardRow = {
    engine: "level1_naive",
    runs: 2,
    avg_cost_usd: 0.0412,
  };
  assert.deepEqual(resolveLeaderboardRowCosts(legacyRow), {
    engineCostUsd: 0.0412,
    judgeCostUsd: null,
    totalCostUsd: 0.0412,
  });

  // Unscored empty row
  assert.deepEqual(resolveLeaderboardRowCosts({ engine: "level1_naive", runs: 0, avg_cost_usd: null }), {
    engineCostUsd: null,
    judgeCostUsd: null,
    totalCostUsd: null,
  });
});

test("resolveScoreboardCosts derives Engine $ from score_breakdown.cost_usd and Judge $ from run_cost_usd", () => {
  // Post-eval scored row (breakdown.cost_usd = engine_cost_usd = 0.0085, costUsd = run_cost_usd = 0.0308)
  const scored = resolveScoreboardCosts({
    costUsd: 0.0308,
    breakdown: {
      battle_score: 86.9,
      efficiency: 0.896,
      qa_relevance: 0.855,
      groundedness: 0.984,
      context_precision: 0.637,
      latency_ms: 9840,
      cost_usd: 0.0085,
    },
  });
  assert.equal(scored.engineCostUsd, 0.0085);
  assert.ok(Math.abs((scored.judgeCostUsd ?? 0) - 0.0223) < 1e-8);
  assert.equal(scored.totalCostUsd, 0.0308);

  // Mid-run after answer_complete before eval_complete
  const running = resolveScoreboardCosts({ costUsd: 0.0085, breakdown: null });
  assert.deepEqual(running, { engineCostUsd: 0.0085, judgeCostUsd: null, totalCostUsd: 0.0085 });

  // Pending / failed row
  assert.deepEqual(resolveScoreboardCosts({ costUsd: null, breakdown: null }), {
    engineCostUsd: null,
    judgeCostUsd: null,
    totalCostUsd: null,
  });
});

test("resolveTimelineCosts and formatRunCostPill surface Engine $ vs Judge $ transparently on Level & Workbench rails", () => {
  assert.equal(formatRunCostPill(undefined), null);

  const midStream: EngineTimeline = {
    engine: "level2_hybrid",
    stages: {},
    stageOrder: [],
    agentTurns: [],
    answer: {
      answer: "CLIN rate is $245/hr.",
      citations: [],
      retrievedChunks: [],
      latencyMs: 9800,
      costUsd: 0.0085,
      usage: { prompt_tokens: 1200, output_tokens: 90, embedding_tokens: 14 },
      flags: { reranker_used: true, adk_runner_used: false, layout_source: null },
    },
    evaluation: null,
    error: null,
  };
  const midPill = formatRunCostPill(midStream);
  assert.ok(midPill);
  assert.equal(midPill.text, "engine: $0.00850");

  const completed: EngineTimeline = {
    ...midStream,
    evaluation: {
      metrics: { groundedness: 0.98, qa_relevance: 0.95, context_precision: 0.8 },
      judgeModel: "claude-opus-5-5",
      referenceAnswerUsed: true,
      durationMs: 4100,
      judgeCostUsd: 0.0223,
      runCostUsd: 0.0308,
      battleScore: 91.2,
    },
  };
  const costs = resolveTimelineCosts(completed);
  assert.equal(costs.engineCostUsd, 0.0085);
  assert.equal(costs.judgeCostUsd, 0.0223);
  assert.equal(costs.totalCostUsd, 0.0308);

  const fullPill = formatRunCostPill(completed);
  assert.ok(fullPill);
  assert.equal(fullPill.text, "engine: $0.00850 · judge: $0.0223");
  assert.match(fullPill.title, /total \$0\.0308/i);
});

test("vertex-genai-eval is judge-only in live mode, honest in replay mode, and recognized by isManagedEvalService", () => {
  assert.equal(isManagedEvalService("vertex-genai-eval"), true);
  assert.equal(isManagedEvalService("claude-opus-5-5"), false);
  assert.match(BATTLE_SCORE_FOOTNOTE, /engine cost ÷ \$0\.20/i);
  assert.match(BATTLE_SCORE_FOOTNOTE, /Judge Cost/i);

  // Judge-only: never listed under synthesis, always selectable under judge
  assert.equal(optionStates(CATALOG_LIVE, "synthesis", null).some((o) => o.entry.id === "vertex-genai-eval"), false);
  const judgeOpts = optionStates(CATALOG_LIVE, "judge", "gemini-3.8-flash");
  const evalOpt = judgeOpts.find((o) => o.entry.id === "vertex-genai-eval");
  assert.ok(evalOpt && !evalOpt.disabled);
  assert.equal(validateSelection(CATALOG_LIVE, { synthesis: "gemini-3.8-flash", judge: "vertex-genai-eval" }), null);
  assert.equal(validateSelection(CATALOG_LIVE, { synthesis: "claude-opus-5-5", judge: "vertex-genai-eval" }), null);

  // Replay mode without vertex-genai-eval recordings: marked unavailable honestly in picker disclosure
  const replayCatalog: ModelsResponse = {
    ...CATALOG_LIVE,
    demo_mode: "replay",
    models: CATALOG_LIVE.models.map((m) =>
      m.id === "vertex-genai-eval"
        ? {
            ...m,
            available: false,
            unavailable_reason:
              "no recorded runs in data/recordings for judge 'vertex-genai-eval' (switch to DEMO_MODE=live to call :evaluateInstances)",
          }
        : m
    ),
  };
  const details = buildPickerDetails(
    replayCatalog,
    { synthesis: "gemini-3.8-flash", judge: "claude-opus-5-5" },
    "level2_hybrid"
  );
  const blocked = details.blockedModels.find((b) => b.id === "vertex-genai-eval");
  assert.ok(blocked && blocked.unavailable);
  assert.match(blocked.reason, /data\/recordings/);
});
