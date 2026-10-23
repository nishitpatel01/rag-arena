/**
 * Compare-page tests — run with `npm test`.
 * Covers: per-level selection rules (catalog restriction, Level 4 Gemini-only, replay recordings),
 * engines sent in ladder order, cost estimate from recordings, server Battle Score stored by the reducer
 * (never computed client-side), and the scoreboard ranking that excludes failed / unscored levels.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  bestRows,
  costEstimate,
  enginesToRun,
  levelChoices,
  levelName,
  scoreboardRows,
  summaryText,
} from "../src/features/compare/compareModel";
import { reduceStreamEvents, type TimelineEvent } from "../src/features/levels/model";
import type { LevelDescriptor, ModelCatalogEntry, ModelsResponse } from "../src/types/arena";

const model = (id: string, provider: string, extra: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry => ({
  id,
  label: id,
  provider,
  location: "global",
  roles: ["synthesis", "judge"],
  input_usd_per_1m: 1,
  output_usd_per_1m: 2,
  price_source: null,
  price_as_of: null,
  available: true,
  unavailable_reason: null,
  verified_at: null,
  ...extra,
});

const IDS = ["level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex"];
const level = (id: string, order: number, enabled = true): LevelDescriptor => ({
  level_id: id as LevelDescriptor["level_id"],
  label: `L${order} label`,
  order,
  enabled,
  stages: [],
  architecture: { nodes: [], edges: [] } as unknown as LevelDescriptor["architecture"],
});
// Deliberately out of order: the page must sort by `order`.
const LEVELS = [level(IDS[4], 5), level(IDS[0], 1), level(IDS[2], 3), level(IDS[1], 2), level(IDS[3], 4)];

const PAIRS = IDS.map((engine) => ({ engine, synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", question_ids: ["q1"] }));
const CATALOG: ModelsResponse = {
  models: [
    model("gemini-4-argon", "vertex_gemini"),
    model("claude-opus-5-5", "anthropic_vertex"),
    model("claude-fable", "anthropic_vertex"),
    model("vertex-ai-search-summary", "vertex_ai_search", { roles: ["synthesis"], allowed_levels: ["managed_vertex"] }),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "live",
  recorded_pairs: PAIRS,
};
const ARGON_OPUS = { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" };

test("default Argon + Opus: all five enabled levels runnable, ladder order", () => {
  const choices = levelChoices(LEVELS, CATALOG, ARGON_OPUS, "q1");
  assert.deepEqual(choices.map((c) => c.levelId), IDS);
  assert.deepEqual(choices.map((c) => c.short), ["Level 1", "Level 2", "Level 3", "Level 4", "Level 5"]);
  assert.ok(choices.every((c) => c.disabledReason === null));
  assert.deepEqual(enginesToRun(choices, new Set([IDS[3], IDS[0]])), [IDS[0], IDS[3]]);
});

test("levelName strips a leading 'Level N:' only", () => {
  assert.equal(levelName("Level 4: Agentic RAG (Google ADK)"), "Agentic RAG (Google ADK)");
  assert.equal(levelName("Managed Vertex AI Search"), "Managed Vertex AI Search");
  assert.equal(levelName("Level 2"), "Level 2");
});

test("disabled (not enabled) levels are not offered", () => {
  const choices = levelChoices([...LEVELS.slice(0, 4), level(IDS[3], 4, false)], CATALOG, ARGON_OPUS, "q1");
  assert.equal(choices.some((c) => c.levelId === IDS[3]), false);
});

test("Claude synthesis disables Level 4 with the Gemini-only reason; other levels stay runnable", () => {
  const choices = levelChoices(LEVELS, CATALOG, { synthesis: "claude-fable", judge: "claude-opus-5-5" }, "");
  const l4 = choices.find((c) => c.levelId === "level4_agentic");
  assert.match(l4?.disabledReason ?? "", /Level 4 \(ADK agent\) requires a Gemini synthesis model; claude-fable is not Gemini/);
  assert.deepEqual(choices.filter((c) => c.disabledReason === null).map((c) => c.levelId), [IDS[0], IDS[1], IDS[2], IDS[4]]);
  // A disabled level is never sent even when checked.
  assert.deepEqual(enginesToRun(choices, new Set(IDS)), [IDS[0], IDS[1], IDS[2], IDS[4]]);
});

test("managed summary synthesis: only Level 5 runnable, others say why", () => {
  const choices = levelChoices(LEVELS, CATALOG, { synthesis: "vertex-ai-search-summary", judge: "claude-opus-5-5" }, "");
  assert.deepEqual(choices.filter((c) => c.disabledReason === null).map((c) => c.levelId), ["managed_vertex"]);
  assert.match(choices[0].disabledReason ?? "", /valid only on Level 5/);
});

test("judge equal to synthesis disables every level", () => {
  const choices = levelChoices(LEVELS, CATALOG, { synthesis: "gemini-4-argon", judge: "gemini-4-argon" }, "q1");
  assert.ok(choices.every((c) => /must differ/.test(c.disabledReason ?? "")));
});

test("replay: only levels with a recording for (question, pair) can run", () => {
  const replay: ModelsResponse = {
    ...CATALOG,
    demo_mode: "replay",
    recorded_pairs: PAIRS.map((p) => (p.engine === "level4_agentic" ? { ...p, question_ids: ["q2"] } : p)),
  };
  const choices = levelChoices(LEVELS, replay, ARGON_OPUS, "q1");
  assert.match(choices.find((c) => c.levelId === "level4_agentic")?.disabledReason ?? "", /^no recording for this pair/);
  assert.equal(choices.filter((c) => c.disabledReason === null).length, 4);
  const adhoc = levelChoices(LEVELS, replay, ARGON_OPUS, "");
  assert.ok(adhoc.every((c) => /choose a golden question/.test(c.disabledReason ?? "")));
  const other = levelChoices(LEVELS, replay, { synthesis: "gemini-4-argon", judge: "claude-fable" }, "q1");
  assert.ok(other.every((c) => /no recording for this pair/.test(c.disabledReason ?? "")));
});

test("cost estimate: sum of recorded avg run cost for the exact pair, unknown when a level lacks recordings", () => {
  const lb = {
    groups: [
      { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", rows: [
        { engine: IDS[0], runs: 8, avg_cost_usd: 0.01 },
        { engine: IDS[1], runs: 8, avg_cost_usd: 0.02 },
      ] },
      { synthesis_model: "gemini-3.8-flash", judge_model: "claude-opus-5-5", rows: [{ engine: IDS[2], runs: 8, avg_cost_usd: 9 }] },
    ],
  };
  const known = costEstimate(lb, [IDS[0], IDS[1]], ARGON_OPUS);
  assert.ok(Math.abs((known.totalUsd ?? 0) - 0.03) < 1e-12);
  assert.deepEqual(known.unknownEngines, []);
  const unknown = costEstimate(lb, [IDS[0], IDS[2]], ARGON_OPUS);
  assert.equal(unknown.totalUsd, null);
  assert.deepEqual(unknown.unknownEngines, [IDS[2]]);
  assert.equal(costEstimate(null, [IDS[0]], ARGON_OPUS).totalUsd, null);
});

const ts = "2026-10-23T00:00:00Z";
const evalEv = (engine: string, score: unknown, extra: Record<string, unknown> = {}): TimelineEvent =>
  ({
    event: "eval_complete", run_id: "r", engine, ts, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 10,
    metrics: { groundedness: 0.9, qa_relevance: 0.8, context_precision: 0.7 }, run_cost_usd: 0.02,
    ...(score === undefined ? {} : {
      battle_score: score,
      score_breakdown: { battle_score: score, efficiency: 0.5, qa_relevance: 0.8, groundedness: 0.9, context_precision: 0.7, latency_ms: 5000, cost_usd: 0.02 },
    }),
    ...extra,
  }) as unknown as TimelineEvent;
const answerEv = (engine: string): TimelineEvent =>
  ({ event: "answer_complete", run_id: "r", engine, ts, answer: "a", citations: [], latency_ms: 5000, cost_usd: 0.01,
     usage: { prompt_tokens: 1, output_tokens: 1, embedding_tokens: 0 }, flags: { reranker_used: false, adk_runner_used: false } }) as unknown as TimelineEvent;

test("reducer keeps the server battle_score + breakdown; malformed scores become null (never recomputed)", () => {
  const s = reduceStreamEvents([evalEv(IDS[0], 77.5), evalEv(IDS[1], 140), evalEv(IDS[2], undefined)]);
  assert.equal(s.timelines[IDS[0]].evaluation?.battleScore, 77.5);
  assert.equal(s.timelines[IDS[0]].evaluation?.scoreBreakdown?.efficiency, 0.5);
  assert.equal(s.timelines[IDS[1]].evaluation?.battleScore, null);
  assert.equal(s.timelines[IDS[2]].evaluation?.battleScore, null);
});

test("scoreboard ranks scored levels only; failed levels keep engine_error verbatim with the failed stage", () => {
  const events: TimelineEvent[] = [
    { event: "run_started", run_id: "r", engines: IDS, demo_mode: "replay", ts } as unknown as TimelineEvent,
    answerEv(IDS[0]), evalEv(IDS[0], 70.1),
    answerEv(IDS[1]), evalEv(IDS[1], 81.4),
    { event: "stage_started", run_id: "r", engine: IDS[2], stage: "retrieve", label: "R", service: "BigQuery", ts } as unknown as TimelineEvent,
    { event: "stage_failed", run_id: "r", engine: IDS[2], stage: "retrieve", duration_ms: 5, error_type: "Forbidden", error: "403 Access Denied", ts } as unknown as TimelineEvent,
    { event: "engine_error", run_id: "r", engine: IDS[2], error_type: "Forbidden", error: "403 Access Denied: table x", failed_stage: "retrieve", ts } as unknown as TimelineEvent,
    answerEv(IDS[3]), evalEv(IDS[3], 81.4),
    answerEv(IDS[4]), evalEv(IDS[4], undefined),
  ];
  const state = reduceStreamEvents(events);
  const choices = levelChoices(LEVELS, CATALOG, ARGON_OPUS, "q1");
  const rows = scoreboardRows(choices, IDS, state);
  assert.deepEqual(rows.map((r) => r.status), ["scored", "scored", "failed", "scored", "unscored"]);
  assert.deepEqual(rows.map((r) => r.rank), [3, 1, null, 1, null]);
  const failed = rows[2];
  assert.equal(failed.battleScore, null);
  assert.deepEqual(failed.error, { type: "Forbidden", message: "403 Access Denied: table x", stage: "retrieve" });
  assert.equal(rows[4].battleScore, null, "no server score → not ranked, not invented");
  assert.equal(rows[0].costUsd, 0.02, "run cost (engine + judge) from eval_complete");
  assert.deepEqual(bestRows(rows).map((r) => r.engine), [IDS[1], IDS[3]]);
  assert.equal(summaryText(rows), "Best Battle Score: Level 2 · L2 label and Level 4 · L4 label · 81.4 (tie) (1 failed level not ranked)");
});

test("scoreboard: only included engines; no scores → explicit message", () => {
  const choices = levelChoices(LEVELS, CATALOG, ARGON_OPUS, "q1");
  const rows = scoreboardRows(choices, [IDS[0]], reduceStreamEvents([]));
  assert.deepEqual(rows.map((r) => [r.engine, r.status]), [[IDS[0], "pending"]]);
  assert.equal(summaryText(rows), "No level produced a Battle Score.");
});
