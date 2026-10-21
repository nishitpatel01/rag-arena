/**
 * Level 2 renderer helper tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: hybrid candidate categorisation, rank movement, expansions diff, diagram node summaries,
 * the compare-teaser baseline lookup, the reducer's question id, and safe-mode masking of Level 2 payloads.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CATEGORY_LABEL,
  MAX_DIFF_CELLS,
  categorizeCandidate,
  countCategories,
  diffWords,
  movedIntoTop,
  parseHybridRetrieve,
  parseQueryPrep,
  parseRerank,
  rankMovement,
  rerankNodeSummary,
  rerankSummaryLine,
  retrieveNodeSummary,
  scoreBarFraction,
  summarizeStage,
  topCutoff,
} from "../src/features/levels/stagePayloads";
import { wrapSummary } from "../src/features/levels/diagramModel";
import { baselineFor, teaserMetrics } from "../src/features/levels/compareTeaserModel";
import { reduceStreamEvents } from "../src/features/levels/model";
import { maskDeep, MASK_PROJECT_ID, MASK_PROJECT_NUMBER } from "../src/lib/safeMode";
import { level2DevFixture } from "../src/dev/fixtures/level2_run";
import type { JsonObject, LeaderboardResponse, ModelsResponse } from "../src/types/arena";

const cand = (chunk_id: string, vector_rank: number | null, lexical_rank: number | null, rrf_score: number): JsonObject => ({
  chunk_id, doc_name: `${chunk_id}.pdf`, page_number: 1, section_id: "1.1", vector_rank, lexical_rank, vector_distance: vector_rank ? 0.2 : null, rrf_score,
});

const RETRIEVE: JsonObject = {
  rows_returned: 4, candidate_k: 50, bytes_processed: 13_041_664, slot_millis: 900, cache_hit: false, job_id: "job_1", rrf_k: 60,
  candidates: [cand("c", null, 1, 0.0164), cand("a", 1, 2, 0.0325), cand("b", 2, null, 0.0161), cand("d", 3, null, 0.0159)],
};

test("categorizeCandidate: both / vector only / lexical only / unranked", () => {
  assert.equal(categorizeCandidate({ vectorRank: 1, lexicalRank: 4 }), "both");
  assert.equal(categorizeCandidate({ vectorRank: 2, lexicalRank: null }), "vector");
  assert.equal(categorizeCandidate({ vectorRank: null, lexicalRank: 7 }), "lexical");
  assert.equal(categorizeCandidate({ vectorRank: null, lexicalRank: null }), "unranked");
  assert.equal(CATEGORY_LABEL.both, "vector+lexical");
  assert.equal(CATEGORY_LABEL.vector, "vector only");
  assert.equal(CATEGORY_LABEL.lexical, "lexical only");
});

test("parseHybridRetrieve sorts by rrf_score desc and counts categories", () => {
  const v = parseHybridRetrieve(RETRIEVE);
  assert.ok(v);
  assert.deepEqual(v.candidates.map((c) => c.chunkId), ["a", "c", "b", "d"]);
  assert.deepEqual(countCategories(v.candidates), { both: 1, vector: 2, lexical: 1, unranked: 0 });
  assert.equal(v.invalidRows, 0);
  assert.equal(v.cacheHit, false);
});

test("parseHybridRetrieve returns null for the Level 1 retrieve payload (no candidates)", () => {
  assert.equal(parseHybridRetrieve({ sql: "SELECT 1", rows_returned: 5, top_k: 5, scores: [0.8], bytes_processed: 10 }), null);
  assert.equal(parseHybridRetrieve(null), null);
});

test("parseHybridRetrieve drops malformed rows loudly (invalidRows) and treats bad ranks as absent", () => {
  const v = parseHybridRetrieve({ candidates: [cand("ok", 1, null, 0.1), { chunk_id: "no-score" }, "junk", cand("bad-rank", 0, -2, 0.05)] });
  assert.ok(v);
  assert.equal(v.invalidRows, 2);
  assert.equal(v.candidates.length, 2);
  assert.equal(categorizeCandidate(v.candidates[1]), "unranked");
});

test("rankMovement: up shows +N, down shows −N (U+2212), same shows 0", () => {
  assert.deepEqual(rankMovement(4, 1), { delta: 3, direction: "up", text: "+3", srText: "moved up 3 places" });
  assert.deepEqual(rankMovement(1, 3), { delta: -2, direction: "down", text: "\u22122", srText: "moved down 2 places" });
  assert.deepEqual(rankMovement(2, 2), { delta: 0, direction: "same", text: "0", srText: "rank unchanged" });
  assert.equal(rankMovement(2, 1).srText, "moved up 1 place");
});

test("movedIntoTop only when RRF rank was outside the cutoff and rerank rank is inside", () => {
  assert.equal(movedIntoTop(9, 3, 5), true);
  assert.equal(movedIntoTop(5, 1, 5), false);
  assert.equal(movedIntoTop(9, 6, 5), false);
  assert.equal(movedIntoTop(9, 1, 0), false);
});

const RERANK: JsonObject = {
  service_used: "Discovery Engine Ranking API", model: "semantic-ranker-default@latest", candidates_in: 50, candidates_out: 5, rank_api_ms: 402.4,
  results: [
    { chunk_id: "x", doc_name: "x.pdf", page_number: 2, rrf_rank: 1, rrf_score: 0.03, rerank_rank: 2, rerank_score: 0.88 },
    { chunk_id: "y", doc_name: "y.pdf", page_number: 3, rrf_rank: 9, rrf_score: 0.01, rerank_rank: 1, rerank_score: 0.951 },
    { chunk_id: "broken", rrf_rank: "1", rerank_rank: 3, rerank_score: 0.5 },
  ],
};

test("parseRerank sorts by rerank_rank, counts invalid rows, and builds the summary line", () => {
  const v = parseRerank(RERANK);
  assert.ok(v);
  assert.deepEqual(v.results.map((r) => r.chunkId), ["y", "x"]);
  assert.equal(v.invalidRows, 1);
  assert.equal(topCutoff(v), 5);
  assert.equal(rerankSummaryLine(v), "50 → 5 via Discovery Engine Ranking API (semantic-ranker-default@latest) · 402 ms");
  assert.equal(parseRerank({ service_used: "x" }), null, "failed/absent results → no dedicated view");
});

test("scoreBarFraction clamps to [0,1] and normalises scores above 1 by the max", () => {
  assert.equal(scoreBarFraction(0.5, 0.95), 0.5);
  assert.equal(scoreBarFraction(-1, 1), 0);
  assert.equal(scoreBarFraction(Number.NaN, 1), 0);
  assert.equal(scoreBarFraction(5, 10), 0.5);
  assert.equal(scoreBarFraction(2, 1), 1);
});

test("diffWords highlights added expansion text and reconstructs both strings", () => {
  const original = "Rate for CLIN-0004-AB under the SOW?";
  const prepared = "Rate for CLIN (Contract Line Item Number)-0004-AB under the SOW (Statement of Work)?";
  const ops = diffWords(original, prepared);
  const added = ops.filter((o) => o.kind === "added").map((o) => o.text.trim());
  assert.deepEqual(added, ["(Contract Line Item Number)", "(Statement of Work)"]);
  assert.equal(ops.filter((o) => o.kind === "removed").length, 0, "pure insertions: nothing removed");
  assert.equal(ops.filter((o) => o.kind !== "removed").map((o) => o.text).join(""), prepared);
  assert.equal(ops.filter((o) => o.kind !== "added").map((o) => o.text).join(""), original);
  assert.deepEqual(diffWords("same text", "same text"), [{ kind: "same", text: "same text" }]);
});

test("diffWords pure insertion between words yields only same + added ops", () => {
  const ops = diffWords("the SOW says", "the SOW (Statement of Work) says");
  assert.deepEqual(ops.map((o) => o.kind), ["same", "added", "same"]);
  assert.equal(ops[1].text.trim(), "(Statement of Work)");
  assert.equal(ops.map((o) => o.text).join(""), "the SOW (Statement of Work) says");
});

test("diffWords falls back to a bounded prefix/suffix diff for huge inputs", () => {
  const big = Array.from({ length: 600 }, (_, i) => `w${i}`).join(" ");
  assert.ok(1199 * 1201 > MAX_DIFF_CELLS);
  const ops = diffWords(big, `${big} EXTRA`);
  assert.equal(ops.filter((o) => o.kind !== "removed").map((o) => o.text).join(""), `${big} EXTRA`);
  assert.equal(ops[ops.length - 1].kind, "added");
});

test("parseQueryPrep reads expansions and counts malformed rows", () => {
  const v = parseQueryPrep({ original_query: "a", prepared_query: "a b", expansions: [{ term: "a", expansion: "b" }, { term: 1 }] });
  assert.ok(v);
  assert.deepEqual(v.expansions, [{ term: "a", expansion: "b" }]);
  assert.equal(v.invalidRows, 1);
  assert.equal(parseQueryPrep({ original_query: "a" }), null);
});

test("node summaries: retrieve rows/both/bytes, rerank in→out + top score, generic fallback", () => {
  const r = parseHybridRetrieve(RETRIEVE);
  assert.ok(r);
  assert.equal(retrieveNodeSummary(r), "rows=4 · both=1 · 12.4 MB scanned");
  assert.equal(retrieveNodeSummary({ ...r, cacheHit: true, bytesProcessed: 0 }), "rows=4 · both=1 · 0 B scanned · cache hit");
  const k = parseRerank(RERANK);
  assert.ok(k);
  assert.equal(rerankNodeSummary(k), "50→5 · top score 0.95");
  assert.equal(summarizeStage("retrieve", RETRIEVE), "rows=4 · both=1 · 12.4 MB scanned");
  assert.equal(summarizeStage("retrieve", { rows_returned: 5, bytes_processed: 1024 }), "rows=5 · 1 KB scanned", "Level 1 keeps generic summary");
  assert.equal(summarizeStage("query_prep", { original_query: "a", prepared_query: "a", expansions: [] }), "");
  assert.equal(summarizeStage("query_prep", { original_query: "a", prepared_query: "a b", expansions: [{ term: "a", expansion: "b" }] }), "expansions=1");
});

test("wrapSummary breaks between ' · ' parts, max 2 lines, ellipsis on overflow", () => {
  assert.deepEqual(wrapSummary("rows=50 · both=12 · 12.4 MB scanned"), ["rows=50 · both=12", "12.4 MB scanned"]);
  assert.deepEqual(wrapSummary("50→5 · top score 0.95"), ["50→5 · top score 0.95"]);
  assert.deepEqual(wrapSummary(""), []);
  const lines = wrapSummary("rows=50 · both=12 · 12.4 MB scanned · cache hit · extra part");
  assert.equal(lines.length, 2);
  assert.ok(lines[1].endsWith("…"));
  assert.ok(lines.every((l) => l.length <= 23), JSON.stringify(lines));
  assert.deepEqual(wrapSummary("a-single-part-that-is-much-too-long-for-one-line"), ["a-single-part-that-is-…"]);
});

const CATALOG = {
  models: [], defaults: { synthesis_model: "s", judge_model: "j" }, demo_mode: "live",
  recorded_pairs: [{ engine: "level1_naive", synthesis_model: "s", judge_model: "j", question_ids: ["q1"] }],
} as unknown as ModelsResponse;
const BOARD: LeaderboardResponse = {
  source: "recordings",
  engines: [
    { engine: "level1_naive", synthesis_model: "s", judge_model: "j", runs: 9, avg_groundedness: 0.94, avg_qa_relevance: 0.687, avg_context_precision: 0.7, avg_latency_ms: 22068.9,
      provenance: [{ question_id: "dev_q01", synthetic_dev_fixture: true }, { question_id: "q1", synthetic_dev_fixture: false }] },
    { engine: "level1_naive", synthesis_model: "other", judge_model: "j", runs: 8, avg_groundedness: 0.1 },
  ],
};

test("baselineFor finds the Level 1 row for the exact pair and flags synthetic provenance", () => {
  const v = baselineFor(CATALOG, BOARD, { engine: "level1_naive", synthesisModel: "s", judgeModel: "j", questionId: "q1" });
  assert.equal(v.pairRecorded, true);
  assert.equal(v.questionRecorded, true);
  assert.equal(v.runs, 9);
  assert.equal(v.syntheticRuns, 1);
  const m = teaserMetrics(v, { groundedness: 1, qaRelevance: 0.9, contextPrecision: 0.8, latencyMs: 6130 });
  assert.deepEqual(m.map((x) => [x.key, x.current, x.baseline]), [
    ["groundedness", 1, 0.94], ["qa_relevance", 0.9, 0.687], ["context_precision", 0.8, 0.7], ["latency", 6130, 22068.9],
  ]);
});

test("baselineFor: unknown pair / unrecorded question / missing row are reported, never guessed", () => {
  assert.equal(baselineFor(CATALOG, BOARD, { engine: "level1_naive", synthesisModel: null, judgeModel: "j", questionId: "q1" }).pairKnown, false);
  const v = baselineFor(CATALOG, BOARD, { engine: "level1_naive", synthesisModel: "s", judgeModel: "j", questionId: "q_other" });
  assert.equal(v.questionRecorded, false);
  const none = baselineFor(CATALOG, BOARD, { engine: "level1_naive", synthesisModel: "x", judgeModel: "y", questionId: "q1" });
  assert.equal(none.row, null);
  assert.equal(none.pairRecorded, false);
  assert.deepEqual(teaserMetrics(none, { groundedness: 1, qaRelevance: null, contextPrecision: null, latencyMs: null }).map((x) => x.baseline), [null, null, null, null]);
});

test("reducer keeps run_started.question_id (empty → null)", () => {
  const base = { event: "run_started" as const, run_id: "r", engines: ["level2_hybrid"], demo_mode: "live", ts: "2026-10-21T00:00:00Z" };
  assert.equal(reduceStreamEvents([{ ...base, question_id: "q1" }]).questionId, "q1");
  assert.equal(reduceStreamEvents([{ ...base, question_id: "" }]).questionId, null);
});

test("safe mode: masked Level 2 payloads never surface identities through the helpers", () => {
  const health = { project_id: "acme-proj-42", gcs_bucket: "acme-proj-42-rag-arena-corpus" };
  const raw: JsonObject = {
    ...RETRIEVE,
    job_id: "acme-proj-42:US.job_123",
    candidates: [{ ...cand("acme-proj-42-chunk", 1, 1, 0.03), doc_name: "gs://acme-proj-42-rag-arena-corpus/a.pdf", section_id: "projects/123456789/x" }],
  };
  const masked = maskDeep(raw, health);
  const v = parseHybridRetrieve(masked);
  assert.ok(v);
  const rendered = JSON.stringify(v) + retrieveNodeSummary(v);
  assert.ok(!rendered.includes("acme-proj-42"), rendered);
  assert.ok(!/projects\/\d{6,}/.test(rendered), rendered);
  assert.ok(rendered.includes(MASK_PROJECT_ID));
  assert.ok(rendered.includes(MASK_PROJECT_NUMBER));
  const rr = parseRerank(maskDeep({ ...RERANK, service_used: "projects/123456789/rankingConfigs/default", model: "acme-proj-42 ranker" }, health));
  assert.ok(rr);
  const line = rerankSummaryLine(rr);
  assert.ok(!line.includes("acme-proj-42") && !/projects\/\d{6,}/.test(line), line);
  const qp = parseQueryPrep(maskDeep({ original_query: "acme-proj-42?", prepared_query: "acme-proj-42 ?", expansions: [{ term: "acme-proj-42", expansion: "x" }] }, health));
  assert.ok(qp);
  assert.ok(!JSON.stringify(diffWords(qp.originalQuery, qp.preparedQuery)).includes("acme-proj-42"));
  assert.ok(!JSON.stringify(qp.expansions).includes("acme-proj-42"));
});

test("DEV Level 2 fixture conforms to the contract parsers (no invalid rows, lifts into top-5)", () => {
  for (const failRerank of [false, true]) {
    const events = level2DevFixture(failRerank).map((e) => e.event);
    const state = reduceStreamEvents(events);
    const tl = state.timelines.level2_hybrid;
    const r = parseHybridRetrieve(tl.stages.retrieve.payload);
    assert.ok(r);
    assert.equal(r.invalidRows, 0);
    assert.equal(r.candidates.length, 50);
    const qp = parseQueryPrep(tl.stages.query_prep.payload);
    assert.ok(qp && qp.expansions.length === 2);
    if (failRerank) {
      assert.equal(tl.stages.rerank.status, "failed");
      assert.equal(tl.error?.failedStage, "rerank");
      assert.equal(parseRerank(tl.stages.rerank.payload), null);
    } else {
      const k = parseRerank(tl.stages.rerank.payload);
      assert.ok(k);
      assert.equal(k.invalidRows, 0);
      assert.equal(k.results.filter((x) => movedIntoTop(x.rrfRank, x.rerankRank, topCutoff(k))).length, 2);
      assert.equal(state.complete, true);
    }
  }
});
