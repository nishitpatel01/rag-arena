/**
 * Model-picker tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: picker validation (judge ≠ synthesis, unavailable blocked, invalid localStorage → defaults),
 * replay pair gating, reducer storing the run's models, diagram preferring stage service over default,
 * and leaderboard grouping by model pair.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MUST_DIFFER_REASON,
  formatModelPrice,
  loadSelection,
  optionStates,
  replayGate,
  safeHttpUrl,
  serializeSelection,
  validateSelection,
} from "../src/features/levels/modelSelection";
import { reduceStreamEvents, type TimelineEvent } from "../src/features/levels/model";
import { resolveNodeService } from "../src/features/levels/diagramModel";
import { backendErrorText } from "../src/lib/api";
import {
  BATTLE_SCORE_FOOTNOTE,
  defaultSource,
  formatBattleScore,
  groupLeaderboard,
  leaderboardRows,
  provenanceDetails,
  provenanceSummary,
  sourceOptions,
} from "../src/pages/leaderboardModel";
import { leaderboardSearch } from "../src/lib/api";
import type { ArchNode, ModelCatalogEntry, ModelsResponse } from "../src/types/arena";

const entry = (id: string, available = true, reason: string | null = null): ModelCatalogEntry => ({
  id,
  label: id.toUpperCase(),
  provider: id.startsWith("claude") ? "anthropic_vertex" : "vertex_gemini",
  location: "global",
  roles: ["synthesis", "judge"],
  input_usd_per_1m: 1.25,
  output_usd_per_1m: 10,
  price_source: "https://example.test/pricing",
  price_as_of: "2026-10-21",
  available,
  unavailable_reason: reason,
  verified_at: null,
});

const CATALOG: ModelsResponse = {
  models: [entry("argon"), entry("flash"), entry("claude-opus"), entry("claude-fable", false, "quota exceeded in global")],
  defaults: { synthesis_model: "argon", judge_model: "claude-opus" },
  demo_mode: "live",
  recorded_pairs: [],
};

test("judge select disables the current synthesis model (and vice versa) with the differ reason", () => {
  const judgeOpts = optionStates(CATALOG, "judge", "argon");
  const argon = judgeOpts.find((o) => o.entry.id === "argon");
  assert.equal(argon?.disabled, true);
  assert.equal(argon?.reason, MUST_DIFFER_REASON);
  const synthOpts = optionStates(CATALOG, "synthesis", "claude-opus");
  assert.equal(synthOpts.find((o) => o.entry.id === "claude-opus")?.disabled, true);
  assert.equal(synthOpts.find((o) => o.entry.id === "flash")?.disabled, false);
  assert.match(validateSelection(CATALOG, { synthesis: "flash", judge: "flash" }) ?? "", /must differ from synthesis model/);
});

test("unavailable models are listed but disabled with the server reason, and cannot be selected", () => {
  const opts = optionStates(CATALOG, "judge", "argon");
  const fable = opts.find((o) => o.entry.id === "claude-fable");
  assert.ok(fable, "unavailable model stays listed");
  assert.equal(fable.disabled, true);
  assert.equal(fable.unavailable, true);
  assert.equal(fable.reason, "quota exceeded in global");
  assert.match(validateSelection(CATALOG, { synthesis: "argon", judge: "claude-fable" }) ?? "", /unavailable: quota exceeded/);
  assert.equal(validateSelection(CATALOG, { synthesis: "argon", judge: "claude-opus" }), null);
  // Role filter: a judge-only model is not offered for synthesis.
  const judgeOnly: ModelsResponse = { ...CATALOG, models: [...CATALOG.models, { ...entry("judge-only"), roles: ["judge"] }] };
  assert.equal(optionStates(judgeOnly, "synthesis", null).some((o) => o.entry.id === "judge-only"), false);
  assert.match(validateSelection(judgeOnly, { synthesis: "judge-only", judge: "argon" }) ?? "", /cannot be used for synthesis/);
});

test("localStorage selection is validated against the catalog; invalid → defaults", () => {
  const defaults = { synthesis: "argon", judge: "claude-opus" };
  assert.deepEqual(loadSelection(CATALOG, serializeSelection({ synthesis: "flash", judge: "claude-opus" })), { synthesis: "flash", judge: "claude-opus" });
  assert.deepEqual(loadSelection(CATALOG, null), defaults);
  assert.deepEqual(loadSelection(CATALOG, "not json"), defaults);
  assert.deepEqual(loadSelection(CATALOG, JSON.stringify({ synthesis: 1, judge: "x" })), defaults);
  assert.deepEqual(loadSelection(CATALOG, serializeSelection({ synthesis: "flash", judge: "flash" })), defaults, "judge == synthesis");
  assert.deepEqual(loadSelection(CATALOG, serializeSelection({ synthesis: "argon", judge: "claude-fable" })), defaults, "unavailable");
  assert.deepEqual(loadSelection(CATALOG, serializeSelection({ synthesis: "gone", judge: "claude-opus" })), defaults, "not in catalog");
  assert.deepEqual(loadSelection(CATALOG, "x".repeat(5000)), defaults, "oversized");
});

test("replay gate: only recorded pairs for the question can run", () => {
  const replay: ModelsResponse = {
    ...CATALOG,
    demo_mode: "replay",
    recorded_pairs: [
      { engine: "level1_naive", synthesis_model: "argon", judge_model: "claude-opus", question_ids: ["q1", "q2"] },
      { engine: "level1_naive", synthesis_model: "flash", judge_model: "claude-opus", question_ids: ["q1"] },
      { engine: "level2_hybrid", synthesis_model: "flash", judge_model: "argon", question_ids: ["q2"] },
    ],
  };
  const ok = replayGate(replay, "level1_naive", "q1", { synthesis: "flash", judge: "claude-opus" });
  assert.equal(ok.blockedReason, null);
  assert.equal(ok.pairs.length, 2);
  const blocked = replayGate(replay, "level1_naive", "q2", { synthesis: "flash", judge: "claude-opus" });
  assert.match(blocked.blockedReason ?? "", /No recording of "q2"/);
  assert.deepEqual(blocked.pairs.map((p) => p.synthesis_model), ["argon"]);
  assert.match(replayGate(replay, "level1_naive", "", { synthesis: "argon", judge: "claude-opus" }).blockedReason ?? "", /choose a golden question/);
  assert.equal(replayGate(CATALOG, "level1_naive", "q1", { synthesis: "flash", judge: "argon" }).replay, false, "live mode never gates on recordings");
});

test("price text and link come from the catalog; non-http links are dropped", () => {
  assert.equal(formatModelPrice(entry("argon")), "$1.25 / $10 per 1M in/out · as of 2026-10-21");
  assert.equal(formatModelPrice({ ...entry("x"), input_usd_per_1m: null, output_usd_per_1m: null }), "price unknown");
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("https://example.test/p"), "https://example.test/p");
});

const RUN = "run-1";
const E = "level1_naive";
const T = "2026-10-21T18:00:00.000Z";

test("reducer stores the run's models from run_started (null on pre-picker backends)", () => {
  const s = reduceStreamEvents([
    { event: "run_started", run_id: RUN, engines: [E], demo_mode: "live", question_id: "q1", synthesis_model: "flash", judge_model: "claude-opus", ts: T },
  ]);
  assert.equal(s.synthesisModel, "flash");
  assert.equal(s.judgeModel, "claude-opus");
  const old = reduceStreamEvents([{ event: "run_started", run_id: RUN, engines: [E], demo_mode: "live", question_id: null, ts: T }]);
  assert.equal(old.synthesisModel, null);
  assert.equal(old.judgeModel, null);
});

test("diagram prefers stage_started.service over run model over the labelled descriptor default", () => {
  const node: ArchNode = { id: "llm", label: "Answer synthesis", service: "Vertex AI · gemini-4-argon", kind: "model", col: 0, row: 0, stage_ids: ["synthesize"] };
  assert.deepEqual(resolveNodeService(node, undefined), { text: "Vertex AI · gemini-4-argon", source: "default" });
  const runOnly = { synthesize: "FLASH" };
  assert.deepEqual(resolveNodeService(node, undefined, runOnly), { text: "FLASH", source: "run" });
  const s = reduceStreamEvents([
    { event: "stage_started", run_id: RUN, engine: E, stage: "synthesize", label: "Synthesis", service: "Vertex AI · Gemini 3.8 Flash", request: {}, ts: T } as TimelineEvent,
  ]);
  assert.deepEqual(resolveNodeService(node, s.timelines[E], runOnly), { text: "Vertex AI · Gemini 3.8 Flash", source: "stage" });
});

test("backend {error} bodies are surfaced verbatim; other bodies are bounded raw text", () => {
  assert.equal(backendErrorText('{"error": "judge_model must differ from synthesis_model"}', "Bad Request"), "judge_model must differ from synthesis_model");
  assert.equal(backendErrorText("Not Found", "x"), "Not Found");
  assert.equal(backendErrorText("", "Bad Request"), "Bad Request");
});

const BOARD = {
  source: "query_runs",
  groups: [
    { synthesis_model: "argon", judge_model: "claude-opus", pair_known: true, note: null, rows: [
      { engine: "level2_hybrid", level_label: "L2", runs: 2, avg_battle_score: 80, avg_cost_usd: 0.05 },
      { engine: E, level_label: "L1", runs: 0, failures: 1, avg_battle_score: null },
      { engine: "level3_multimodal", runs: 1, avg_battle_score: 71.5 },
    ] },
    { synthesis_model: "flash", judge_model: "claude-opus", rows: [{ engine: E, runs: 1, avg_battle_score: 64 }] },
    { synthesis_model: "unknown", judge_model: "unknown", pair_known: false, note: "kept apart", rows: [{ engine: E, runs: 3, avg_battle_score: 90 }] },
    { synthesis_model: "", judge_model: "x", rows: [{ engine: E }] },
  ],
};

test("leaderboard keeps the server's per-pair groups, ranks scored rows only, isolates unknown pairs", () => {
  const groups = groupLeaderboard(BOARD);
  assert.deepEqual(groups.map((g) => [g.key, g.pairKnown, g.rows.length]), [
    ["argon|claude-opus", true, 3], ["flash|claude-opus", true, 1], ["unknown|unknown", false, 1],
  ]);
  assert.deepEqual(groups[0].rows.map((r) => [r.row.engine, r.rank]), [["level2_hybrid", 1], [E, null], ["level3_multimodal", 2]]);
  assert.equal(groups[2].note, "kept apart");
  assert.deepEqual(groupLeaderboard(null), []);
  assert.deepEqual(groupLeaderboard({ source: "recordings" }), []);
});

test("leaderboardRows flattens groups and stamps each row with its pair (Compare / teaser lookups)", () => {
  const rows = leaderboardRows(BOARD);
  assert.equal(rows.length, 5);
  assert.deepEqual([rows[0].engine, rows[0].synthesis_model, rows[0].judge_model], ["level2_hybrid", "argon", "claude-opus"]);
  assert.equal(rows.filter((r) => r.engine === E && r.synthesis_model === "flash").length, 1);
});

test("source toggle: replay disables Live runs with a reason; live/record default to query_runs", () => {
  assert.equal(defaultSource("replay"), "recordings");
  assert.equal(defaultSource(null), "recordings");
  assert.equal(defaultSource("live"), "query_runs");
  assert.equal(defaultSource("record"), "query_runs");
  const replay = sourceOptions("replay");
  assert.deepEqual(replay.map((o) => [o.value, o.disabled]), [["recordings", false], ["query_runs", true]]);
  assert.match(replay[1].reason ?? "", /replay mode never reads/);
  assert.equal(sourceOptions("live")[1].disabled, false);
  assert.equal(leaderboardSearch({}), "");
  assert.equal(leaderboardSearch({ source: "query_runs", questionId: "q 1", engine: "" }), "?source=query_runs&question_id=q+1");
});

test("provenance text: recordings files/range, query_runs table/modes, details list full SHAs", () => {
  const rec = { kind: "recording", files: 8, recorded_at: { first: "2026-10-21T18:11:48Z", last: "2026-10-23T01:00:00Z" }, git_shas: ["a", "b"] };
  assert.equal(provenanceSummary(rec), "8 recording files · recorded 2026-10-21 → 2026-10-23 · 2 git SHAs");
  const live = { kind: "query_runs", table: "rag_arena.query_runs", demo_modes: { live: 3, record: 1 }, git_shas: ["abc"] };
  assert.equal(provenanceSummary(live), "rag_arena.query_runs · live 3, record 1 · 1 git SHA");
  assert.deepEqual(provenanceDetails(live), [
    ["source", "query_runs"], ["table", "rag_arena.query_runs"], ["demo_mode live", "3"], ["demo_mode record", "1"], ["git SHAs", "abc"],
  ]);
  assert.equal(provenanceSummary(undefined), "provenance not reported");
  assert.equal(formatBattleScore(71.46), "71.5");
  assert.equal(formatBattleScore(null), "—");
  assert.match(BATTLE_SCORE_FOOTNOTE, /40% QA relevance \+ 35% groundedness \+ 15% context precision \+ 10% efficiency/);
  assert.match(BATTLE_SCORE_FOOTNOTE, /60 s/);
  assert.match(BATTLE_SCORE_FOOTNOTE, /\$0\.20/);
});
