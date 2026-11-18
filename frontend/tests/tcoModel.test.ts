/**
 * TDD suite for Slice #1 (`frontend/src/features/wizard/tcoModel.ts`):
 * - Strict workload validation & logarithmic slider round-trips (`1k..10M` queries, `100..500k` pages)
 * - Exact parity with `backend/app/core/tco.py` across all 8 architectures, 5 scenario presets, & crossover math
 * - Zero hardcoded benchmark scores (`spec.md` P3); dynamic joining of `LeaderboardResponse` + `LevelDescriptor[]`
 * - 1-click `/compare` candidate pre-selection (`uncheckedForCompareCandidates`)
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server.browser";
import { MemoryRouter } from "react-router-dom";
import { SHORT_LEVEL_LABEL } from "../src/app/Shell";
import { ArchitectureWizard } from "../src/features/wizard/ArchitectureWizard";
import { TcoCalculatorPanel } from "../src/features/wizard/TcoCalculatorPanel";
import {
  COMPLIANCE_POSTURE_OPTIONS,
  CORPUS_PAGES_BOUNDS,
  DATA_GRAVITY_OPTIONS,
  DEFAULT_WORKLOAD,
  DOC_COMPLEXITY_OPTIONS,
  ENTERPRISE_SCENARIO_PRESETS,
  LATENCY_SLA_OPTIONS,
  MONTHLY_QUERIES_BOUNDS,
  TCO_SYNTHESIS_MODELS,
  estimateEnterpriseTco,
  extractRecordedCohorts,
  formatCompactInt,
  formatUsdMonthly,
  logSliderToValue,
  parseTcoWorkload,
  sortArchitectures,
  uncheckedForCompareCandidates,
  valueToLogSlider,
  type TcoWorkload,
} from "../src/features/wizard/tcoModel";
import { LEVEL_IDS, type LeaderboardResponse, type LevelDescriptor } from "../src/types/arena";

test("parseTcoWorkload enforces strict bounds, enums, and rejects invalid inputs", () => {
  assert.deepEqual(parseTcoWorkload({}), DEFAULT_WORKLOAD);

  const minWl = parseTcoWorkload({
    data_gravity: "bigquery_warehouse",
    monthly_queries: MONTHLY_QUERIES_BOUNDS[0],
    corpus_pages: CORPUS_PAGES_BOUNDS[0],
    latency_sla: "sub_second_retrieval",
    compliance_posture: "standard_serverless",
    doc_complexity: "plain_text",
    synthesis_model: "gemini-3.8-flash",
  });
  assert.equal(minWl.monthly_queries, 1_000);
  assert.equal(minWl.corpus_pages, 100);

  const maxWl = parseTcoWorkload({
    data_gravity: "managed_turnkey",
    monthly_queries: MONTHLY_QUERIES_BOUNDS[1],
    corpus_pages: CORPUS_PAGES_BOUNDS[1],
    latency_sla: "deep_agentic_reasoning",
    compliance_posture: "cmek_vpc",
    doc_complexity: "multi_hop_math",
    synthesis_model: "gemini-4-argon",
  });
  assert.equal(maxWl.monthly_queries, 10_000_000);
  assert.equal(maxWl.corpus_pages, 500_000);

  assert.equal(DATA_GRAVITY_OPTIONS.length, 4);
  assert.equal(LATENCY_SLA_OPTIONS.length, 3);
  assert.equal(COMPLIANCE_POSTURE_OPTIONS.length, 3);
  assert.equal(DOC_COMPLEXITY_OPTIONS.length, 3);
  assert.equal(TCO_SYNTHESIS_MODELS.length, 6);
  assert.equal(DEFAULT_WORKLOAD.synthesis_model, "gemini-4-hydrogen");
  assert.deepEqual(
    [...TCO_SYNTHESIS_MODELS],
    ["gemini-4-hydrogen", "gemini-4-carbon", "gemini-4-argon", "claude-sonnet-4-6", "claude-opus-5-5", "gemini-3.8-flash"]
  );
  for (const preset of ENTERPRISE_SCENARIO_PRESETS) {
    assert.notEqual(preset.workload.synthesis_model, "gemini-3.8-flash");
  }

  for (const invalid of [
    { monthly_queries: 999 },
    { monthly_queries: 10_000_001 },
    { monthly_queries: 2500.5 },
    { monthly_queries: "25000" },
    { corpus_pages: 99 },
    { corpus_pages: 500_001 },
    { data_gravity: "snowflake" },
    { synthesis_model: "gpt-4o" },
    { unknown_key: 1 },
  ]) {
    assert.throws(() => parseTcoWorkload(invalid as Record<string, unknown>));
  }

  // Claude synthesis model triggers Level 8 ADK Gemini-4 worker watch-out
  const claudeEst = estimateEnterpriseTco(parseTcoWorkload({ synthesis_model: "claude-sonnet-4-6" }));
  const claudeL8 = claudeEst.architectures.find((a) => a.level_id === "level4_agentic");
  assert.ok(claudeL8?.tradeoffs.some((t) => t.includes("Gemini 4 worker") && t.includes("ADK")));
});

test("logarithmic slider helpers preserve exact min/max endpoints and monotonic round-trips", () => {
  assert.equal(logSliderToValue(0, MONTHLY_QUERIES_BOUNDS), 1_000);
  assert.equal(logSliderToValue(1000, MONTHLY_QUERIES_BOUNDS), 10_000_000);
  assert.equal(valueToLogSlider(1_000, MONTHLY_QUERIES_BOUNDS), 0);
  assert.equal(valueToLogSlider(10_000_000, MONTHLY_QUERIES_BOUNDS), 1000);

  assert.equal(logSliderToValue(0, CORPUS_PAGES_BOUNDS), 100);
  assert.equal(logSliderToValue(1000, CORPUS_PAGES_BOUNDS), 500_000);

  let prev = 0;
  for (let pos = 0; pos <= 1000; pos += 100) {
    const val = logSliderToValue(pos, MONTHLY_QUERIES_BOUNDS);
    assert.ok(val >= prev, `non-monotonic at pos=${pos}: ${val} < ${prev}`);
    prev = val;
  }
});

test("pure TCO estimate never invents fake scores and matches GCP standing/crossover economics", () => {
  const base = estimateEnterpriseTco(DEFAULT_WORKLOAD);
  assert.equal(base.architectures.length, 8);
  assert.deepEqual(
    base.architectures.map((a) => a.level_id),
    [
      "managed_vertex",
      "rag_engine",
      "level1_naive",
      "level2_hybrid",
      "alloydb_scann",
      "vertex_vector_v2",
      "level3_multimodal",
      "level4_agentic",
    ]
  );
  assert.deepEqual(
    base.architectures.map((a) => a.level_id),
    [...LEVEL_IDS]
  );
  base.architectures.forEach((row, idx) => {
    assert.equal(row.order, idx + 1);
    assert.ok(row.short_label.startsWith(`L${idx + 1} · `), `short_label ${row.short_label} must start with L${idx + 1}`);
  });
  for (const row of base.architectures) {
    assert.equal(row.recorded_cohort, null);
    assert.equal(row.alternate_cohort, null);
  }

  const byId = Object.fromEntries(base.architectures.map((a) => [a.level_id, a]));
  assert.equal(byId.level1_naive.standing_monthly_usd, 0);
  assert.equal(byId.level2_hybrid.standing_monthly_usd, 0);
  assert.equal(byId.level3_multimodal.standing_monthly_usd, 0);
  assert.equal(byId.level4_agentic.standing_monthly_usd, 0);
  assert.equal(byId.managed_vertex.standing_monthly_usd, 0);
  assert.equal(byId.rag_engine.standing_monthly_usd, 0);
  assert.ok(byId.rag_engine.standing_note.includes("Spanner Basic"));
  assert.ok(byId.alloydb_scann.standing_monthly_usd > 200);
  assert.ok(byId.vertex_vector_v2.standing_monthly_usd > 190);
  assert.ok(byId.level3_multimodal.one_time_ingestion_usd >= 50);

  // High-scale crossover check
  const highScale: TcoWorkload = {
    ...DEFAULT_WORKLOAD,
    monthly_queries: 1_000_000,
    corpus_pages: 100_000,
    doc_complexity: "plain_text",
  };
  const highEst = estimateEnterpriseTco(highScale);
  const highById = Object.fromEntries(highEst.architectures.map((a) => [a.level_id, a]));
  assert.equal(highEst.crossover.regime, "dedicated_scann_favorable");
  assert.ok(highById.vertex_vector_v2.total_monthly_usd < highById.managed_vertex.total_monthly_usd);
  assert.ok(highById.vertex_vector_v2.total_monthly_usd < highById.level3_multimodal.total_monthly_usd);
});

test("all 5 enterprise scenario presets select distinct non-baseline primary recommendations", () => {
  assert.equal(ENTERPRISE_SCENARIO_PRESETS.length, 5);
  const primaries = new Set<string>();
  for (const preset of ENTERPRISE_SCENARIO_PRESETS) {
    const est = estimateEnterpriseTco(preset.workload);
    assert.notEqual(est.recommended_level_id, "level1_naive");
    assert.notEqual(est.runner_up_level_id, "level1_naive");
    assert.notEqual(est.recommended_level_id, est.runner_up_level_id);
    primaries.add(est.recommended_level_id);
  }
  assert.ok(primaries.has("level2_hybrid"));
  assert.ok(primaries.has("level3_multimodal"));
  assert.ok(primaries.has("level4_agentic"));
  assert.ok(primaries.has("alloydb_scann"));
  assert.ok(primaries.has("rag_engine") || primaries.has("managed_vertex"));
});

test("extractRecordedCohorts joins real LeaderboardResponse groups honestly (exact vs alternate pair)", () => {
  const mockLeaderboard: LeaderboardResponse = {
    source: "recordings",
    groups: [
      {
        synthesis_model: "gemini-3.8-flash",
        judge_model: "claude-opus-5-5",
        pair_known: true,
        rows: [
          {
            engine: "level3_multimodal",
            runs: 8,
            questions_covered: 8,
            avg_qa_relevance: 0.825,
            avg_groundedness: 0.963,
            avg_context_precision: 0.704,
            avg_battle_score: 85.0,
            avg_latency_ms: 18350,
            avg_cost_usd: 0.0271,
          },
        ],
      },
      {
        synthesis_model: "gemini-4-argon",
        judge_model: "claude-opus-5-5",
        pair_known: true,
        rows: [
          {
            engine: "managed_vertex",
            runs: 8,
            questions_covered: 8,
            avg_qa_relevance: 0.905,
            avg_groundedness: 0.974,
            avg_context_precision: 0.706,
            avg_battle_score: 87.3,
            avg_latency_ms: 26870,
            avg_cost_usd: 0.0542,
          },
        ],
      },
    ],
  };

  const cohorts = extractRecordedCohorts(mockLeaderboard);
  assert.equal(cohorts.exactCohorts.get("level3_multimodal|gemini-3.8-flash")?.avg_qa_relevance, 0.825);

  // Default workload uses gemini-4-hydrogen (no direct recording yet) -> honest alternate_cohort fallback
  const joined = estimateEnterpriseTco(DEFAULT_WORKLOAD, { leaderboard: mockLeaderboard });
  const byId = Object.fromEntries(joined.architectures.map((a) => [a.level_id, a]));
  assert.equal(byId.level3_multimodal.recorded_cohort, null);
  assert.equal(byId.level3_multimodal.alternate_cohort?.avg_qa_relevance, 0.825);
  assert.equal(byId.managed_vertex.recorded_cohort, null);
  assert.equal(byId.managed_vertex.alternate_cohort?.synthesis_model, "gemini-4-argon");
  assert.equal(byId.managed_vertex.alternate_cohort?.avg_qa_relevance, 0.905);

  // Explicit gemini-3.8-flash workload joins exact Flash cohort cleanly
  const flashJoined = estimateEnterpriseTco(
    { ...DEFAULT_WORKLOAD, synthesis_model: "gemini-3.8-flash" },
    { leaderboard: mockLeaderboard }
  );
  const flashById = Object.fromEntries(flashJoined.architectures.map((a) => [a.level_id, a]));
  assert.equal(flashById.level3_multimodal.recorded_cohort?.avg_qa_relevance, 0.825);
  assert.equal(flashById.level3_multimodal.alternate_cohort, null);

  // Sorting modes work deterministically
  const byTco = sortArchitectures(joined.architectures, "monthly_tco");
  assert.ok(byTco[0].total_monthly_usd <= byTco[byTco.length - 1].total_monthly_usd);
  const byFit = sortArchitectures(joined.architectures, "fit_score");
  assert.ok(byFit[0].fit_score >= byFit[byFit.length - 1].fit_score);
});

test("uncheckedForCompareCandidates pre-selects enabled top candidates for /compare", () => {
  const levels: LevelDescriptor[] = LEVEL_IDS.map((id, idx) => ({
    level_id: id,
    label: id,
    order: idx + 1,
    enabled: id !== "alloydb_scann",
    stages: ["query_prep", "retrieve", "synthesize", "evaluate"],
    architecture: { level_id: id, nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] },
  }));

  const unchecked = uncheckedForCompareCandidates(["level3_multimodal", "level4_agentic"], levels);
  assert.equal(unchecked.includes("level3_multimodal"), false);
  assert.equal(unchecked.includes("level4_agentic"), false);
  assert.equal(unchecked.includes("level1_naive"), true);
  assert.equal(unchecked.length, LEVEL_IDS.length - 2);

  assert.equal(formatUsdMonthly(0), "$0.00");
  assert.equal(formatCompactInt(25_000), "25k");
  assert.equal(formatCompactInt(1_500_000), "1.5M");
});

test("SHORT_LEVEL_LABEL and ArchitectureWizard progressive disclosure enforce Order A + A2 collapsed default", () => {
  assert.deepEqual(
    LEVEL_IDS.map((id) => SHORT_LEVEL_LABEL[id]),
    [
      "1 · Vertex Search",
      "2 · RAG Engine",
      "3 · BQ Vector",
      "4 · BQ Hybrid",
      "5 · AlloyDB ScaNN",
      "6 · Vector Search 2.0",
      "7 · Multimodal",
      "8 · Agentic ADK",
    ]
  );

  const levels: LevelDescriptor[] = LEVEL_IDS.map((id, idx) => ({
    level_id: id,
    label: `Level ${idx + 1}: ${SHORT_LEVEL_LABEL[id]}`,
    order: idx + 1,
    enabled: true,
    stages: ["query_prep", "retrieve", "synthesize", "evaluate"],
    architecture: { level_id: id, nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] },
  }));
  const estimate = estimateEnterpriseTco(DEFAULT_WORKLOAD, { levels });

  const collapsedHtml = renderToStaticMarkup(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(ArchitectureWizard, {
        workload: DEFAULT_WORKLOAD,
        onChange: () => {},
        estimate,
        levels,
        leaderboardLoading: false,
        onCompareCandidates: () => {},
        showAdvanced: false,
        onToggleAdvanced: () => {},
        advancedContent: React.createElement(TcoCalculatorPanel, {
          estimate,
          levels,
          leaderboardLoading: false,
          onCompareCandidates: () => {},
        }),
      })
    )
  );
  assert.match(collapsedHtml, /data-testid="wizard-model-pair-bar"/);
  assert.match(collapsedHtml, /Gemini 4 Hydrogen/);
  assert.doesNotMatch(collapsedHtml, /3\.8-flash|3\.8 Flash/);
  assert.match(collapsedHtml, /data-testid="wizard-primary-card"/);
  assert.match(collapsedHtml, /data-testid="wizard-runner-up-card"/);
  assert.match(collapsedHtml, /data-testid="toggle-advanced-tco"/);
  assert.match(collapsedHtml, /aria-expanded="false"/);
  assert.doesNotMatch(collapsedHtml, /data-testid="overview-advanced-tco"/);
  assert.doesNotMatch(collapsedHtml, /data-testid="tco-calculator-panel"/);

  const expandedHtml = renderToStaticMarkup(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(ArchitectureWizard, {
        workload: DEFAULT_WORKLOAD,
        onChange: () => {},
        estimate,
        levels,
        leaderboardLoading: false,
        onCompareCandidates: () => {},
        showAdvanced: true,
        onToggleAdvanced: () => {},
        advancedContent: React.createElement(TcoCalculatorPanel, {
          estimate,
          levels,
          leaderboardLoading: false,
          onCompareCandidates: () => {},
        }),
      })
    )
  );
  assert.match(expandedHtml, /aria-expanded="true"/);
  assert.match(expandedHtml, /data-testid="overview-advanced-tco"/);
  assert.match(expandedHtml, /data-testid="wizard-crossover-banner"/);
  assert.match(expandedHtml, /data-testid="tco-calculator-panel"/);
  assert.match(expandedHtml, /L6 VVS2 &lt; L1 VAIS/);
});
