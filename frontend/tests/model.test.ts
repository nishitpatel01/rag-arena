/**
 * Reducer tests — run with `npm test` (esbuild bundles to JS, then `node --test`).
 * The reducer is pure: these tests assert state is derived only from events.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  INITIAL_STREAM_STATE,
  activeStageId,
  deriveIngestProgress,
  upsertDocProgress,
  reduceStreamEvent,
  reduceStreamEvents,
  type TimelineEvent,
} from "../src/features/levels/model";
import { buildEdgePaths, deriveNodeView, followTarget, layoutNodes } from "../src/features/levels/diagramModel";
import { parseSseBlock } from "../src/lib/sse";
import type { ArchNode } from "../src/types/arena";

const RUN = "run-1";
const E = "level1_naive";
const T = "2026-10-20T20:00:00.000Z";

const started = (stage: string, ts = T): TimelineEvent => ({
  event: "stage_started", run_id: RUN, engine: E, stage, label: stage, service: "svc", request: { q: 1 }, ts,
});
const completed = (stage: string, duration_ms: number): TimelineEvent => ({
  event: "stage_completed", run_id: RUN, engine: E, stage, duration_ms, payload: { rows_returned: 5 }, ts: T,
});

test("run_started creates idle timelines for every engine", () => {
  const s = reduceStreamEvent(INITIAL_STREAM_STATE, { event: "run_started", run_id: RUN, engines: [E, "x"], demo_mode: "live", question_id: null, ts: T });
  assert.equal(s.runId, RUN);
  assert.deepEqual(Object.keys(s.timelines), [E, "x"]);
  assert.equal(s.events.length, 1);
});

test("stage_started → active with startedAt; stage_completed → done with backend duration_ms", () => {
  const s1 = reduceStreamEvents([started("retrieve")]);
  const st1 = s1.timelines[E].stages["retrieve"];
  assert.equal(st1.status, "active");
  assert.equal(st1.startedAt, Date.parse(T));
  assert.equal(st1.durationMs, null);
  assert.equal(activeStageId(s1.timelines[E]), "retrieve");

  const s2 = reduceStreamEvent(s1, completed("retrieve", 2540));
  const st2 = s2.timelines[E].stages["retrieve"];
  assert.equal(st2.status, "done");
  assert.equal(st2.durationMs, 2540);
  assert.deepEqual(st2.payload, { rows_returned: 5 });
  assert.deepEqual(st2.request, { q: 1 });
  assert.equal(activeStageId(s2.timelines[E]), null);
});

test("stage_failed / stage_skipped / engine_error are recorded verbatim", () => {
  const s = reduceStreamEvents([
    started("rerank"),
    { event: "stage_failed", run_id: RUN, engine: E, stage: "rerank", duration_ms: 12, error_type: "PermissionDenied", error: "403", ts: T },
    { event: "stage_skipped", run_id: RUN, engine: E, stage: "agent_turn", reason: "not an agentic level", ts: T },
    { event: "engine_error", run_id: RUN, engine: E, error_type: "PermissionDenied", error: "403", failed_stage: "rerank", ts: T },
  ]);
  const tl = s.timelines[E];
  assert.equal(tl.stages["rerank"].status, "failed");
  assert.deepEqual(tl.stages["rerank"].error, { type: "PermissionDenied", message: "403" });
  assert.equal(tl.stages["agent_turn"].status, "skipped");
  assert.equal(tl.stages["agent_turn"].skipReason, "not an agentic level");
  assert.deepEqual(tl.error, { type: "PermissionDenied", message: "403", failedStage: "rerank" });
  assert.deepEqual(tl.stageOrder, ["rerank", "agent_turn"]);
});

test("answer/eval/agent_turn/battle_complete populate the timeline; events stay append-only", () => {
  const s = reduceStreamEvents([
    { event: "agent_turn", run_id: RUN, engine: E, turn_index: 1, tool_name: "check_amendments", tool_args: { a: 1 }, tool_result_summary: "ok", duration_ms: 90, ts: T },
    { event: "answer_complete", run_id: RUN, engine: E, answer: "A", citations: [], latency_ms: 10, cost_usd: 0.1, usage: { prompt_tokens: 1, output_tokens: 2, embedding_tokens: 3 }, flags: { reranker_used: false, adk_runner_used: true, layout_source: null }, ts: T },
    { event: "eval_complete", run_id: RUN, engine: E, metrics: { groundedness: 0.5, qa_relevance: 0.6, context_precision: 0.7 }, judge_model: "j", reference_answer_used: true, duration_ms: 5, ts: T },
    { event: "stream_warning", run_id: RUN, message: "cap", ts: T },
    { event: "battle_complete", run_id: RUN, engines_completed: [E], engines_failed: [], ts: T },
  ]);
  const tl = s.timelines[E];
  assert.equal(tl.agentTurns[0].toolName, "check_amendments");
  assert.equal(tl.answer?.answer, "A");
  assert.equal(tl.evaluation?.judgeModel, "j");
  assert.deepEqual(s.warnings, ["cap"]);
  assert.equal(s.complete, true);
  assert.equal(s.events.length, 5);
});

test("ingest job events reuse the reducer and progress derivation", () => {
  const J = "job-1";
  const events: TimelineEvent[] = [
    { event: "job_started", job_id: J, level: E, tier_profile: "naive", ts: T },
    { event: "stage_started", job_id: J, engine: E, stage: "parse", label: "Parse", service: "PyMuPDF", request: {}, ts: T },
    { event: "doc_progress", job_id: J, doc_name: "a.pdf", pages: 3, chunks: 12, ts: T },
    { event: "embed_progress", job_id: J, done: 12, total: 52, batch_ms: 800, ts: T },
    { event: "stage_completed", job_id: J, engine: E, stage: "parse", duration_ms: 1500, payload: {}, ts: T },
    { event: "job_complete", job_id: J, counts_by_tier: { naive: 52 }, index_states: { vector: "ACTIVE" }, ts: T },
  ];
  const s = reduceStreamEvents(events);
  assert.equal(s.runId, J);
  assert.equal(s.timelines[E].stages["parse"].status, "done");
  assert.equal(s.complete, true);
  const p = deriveIngestProgress(events);
  assert.deepEqual(p.docs, [{ doc_name: "a.pdf", pages: 3, chunks: 12 }]);
  assert.deepEqual(p.embed, { done: 12, total: 52, batchMs: 800 });
  assert.deepEqual(p.countsByTier, { naive: 52 });
});

const NODES: ArchNode[] = [
  { id: "a", label: "A", service: "s", kind: "actor", col: 0, row: 0, stage_ids: ["query_prep"] },
  { id: "b", label: "B", service: "s", kind: "store", col: 1, row: 0, stage_ids: ["retrieve", "rerank"] },
];

test("diagram: node state follows mapped stages; edge flows only done→active", () => {
  const s = reduceStreamEvents([started("query_prep"), completed("query_prep", 3), started("retrieve")]);
  const tl = s.timelines[E];
  const va = deriveNodeView(NODES[0], tl);
  const vb = deriveNodeView(NODES[1], tl);
  assert.equal(va.status, "done");
  assert.equal(va.durationMs, 3);
  assert.equal(vb.status, "active");
  const layout = layoutNodes(NODES);
  const paths = buildEdgePaths([{ from: "a", to: "b" }], layout.boxes, { a: va, b: vb });
  assert.equal(paths[0].flowing, true);
  assert.equal(followTarget(NODES, tl), "b");

  const s2 = reduceStreamEvent(s, completed("retrieve", 10));
  const vb2 = deriveNodeView(NODES[1], s2.timelines[E]);
  assert.equal(vb2.status, "done");
  const paths2 = buildEdgePaths([{ from: "a", to: "b" }], layout.boxes, { a: va, b: vb2 });
  assert.equal(paths2[0].flowing, false);
  const s3 = reduceStreamEvent(s2, completed("rerank", 5));
  assert.equal(deriveNodeView(NODES[1], s3.timelines[E]).durationMs, 15);
});

test("replay markers: demoMode from run_started, replayed flag from any replayed:true frame; ad-hoc engine_error without stages", () => {
  const s = reduceStreamEvents([
    { event: "run_started", run_id: RUN, engines: [E], demo_mode: "replay", question_id: "dev_q01", ts: T },
    { ...started("query_prep"), replayed: true, recorded_ts: T } as TimelineEvent,
  ]);
  assert.equal(s.demoMode, "replay");
  assert.equal(s.replayed, true);

  const adhoc = reduceStreamEvents([
    { event: "run_started", run_id: RUN, engines: [E], demo_mode: "replay", question_id: null, ts: T },
    { event: "engine_error", run_id: RUN, engine: E, error_type: "ReplayUnavailableError", error: "No recording for this question in replay mode (question_id missing)", failed_stage: null, ts: T },
    { event: "battle_complete", run_id: RUN, engines_completed: [], engines_failed: [E], ts: T },
  ]);
  assert.equal(adhoc.replayed, false);
  assert.equal(adhoc.timelines[E].error?.type, "ReplayUnavailableError");
  assert.equal(adhoc.timelines[E].stageOrder.length, 0);
  assert.deepEqual(adhoc.enginesFailed, [E]);
  assert.equal(adhoc.complete, true);
});

test("sse: parses data frames, ignores event: lines, rejects non-object frames", () => {
  const frame = parseSseBlock('event: stage_started\ndata: {"event":"stage_started","stage":"x"}');
  assert.equal(frame?.event, "stage_started");
  assert.equal(parseSseBlock(": comment only"), null);
  assert.throws(() => parseSseBlock("data: 42"));
});

test("mini markdown: bullets, bold and inline code parse into blocks (no raw HTML)", async () => {
  const { parseBlocks } = await import("../src/components/ui/MiniMarkdown");
  const blocks = parseBlocks("Intro line\n\n* **Primary:** `us-central1`\n* Failover: `us-east4`\n\n1. first\n2. second");
  assert.deepEqual(blocks.map((b) => b.kind), ["p", "ul", "ol"]);
  assert.equal((blocks[1] as { items: string[] }).items.length, 2);
  assert.equal((blocks[2] as { items: string[] }).items[1], "second");
});

test("ingest doc_progress rows merge per document so chunk-stage counts replace parse-time zeros", () => {
  const rows: { doc_name: string; pages: number; chunks: number }[] = [];
  upsertDocProgress(rows, { doc_name: "a.pdf", pages: 2, chunks: 0 });
  upsertDocProgress(rows, { doc_name: "b.pdf", pages: 1, chunks: 0 });
  upsertDocProgress(rows, { doc_name: "a.pdf", pages: 2, chunks: 17 });
  assert.deepEqual(rows, [
    { doc_name: "a.pdf", pages: 2, chunks: 17 },
    { doc_name: "b.pdf", pages: 1, chunks: 0 },
  ]);
});
