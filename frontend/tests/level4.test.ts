/**
 * Level 4 (Agentic RAG with Google ADK) helper tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: agent_turn ordering + call/result grouping, open-call detection, tool-node highlight mapping (explicit
 * `tool_names` and the backend fallback descriptor), agent ⇄ tool edge flow, unsupported-claims diff, calculator
 * rendering, timeline header counts, safe-mode masking of tool args/results, compare teaser L1–L3, and the DEV fixture.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { ArchNode, BattleEvent, EngineTimeline } from "../src/types/arena";
import {
  agentStageSummary,
  claimsDiff,
  deriveToolNodeView,
  finalCheckLine,
  formatTokens,
  groupAgentTurns,
  headerLine,
  normalizeAgentTurn,
  normalizeExpression,
  openToolNames,
  parseAgentStagePayload,
  parseClaims,
  pricingView,
  timelineHeader,
  toolFlowEdges,
  toolNodeBindings,
  toolNodeSummary,
  toolRowState,
  toolStyle,
  verifyResultClaims,
  type ToolRow,
} from "../src/features/levels/agentTimelineModel";
import { reduceStreamEvents, type TimelineEvent } from "../src/features/levels/model";
import { summarizeStage } from "../src/features/levels/stagePayloads";
import { maskDeep } from "../src/lib/safeMode";
import { lowerEnabledLevels, teaserTitle } from "../src/features/levels/compareTeaserModel";
import { LEVEL4_DEV_ARCHITECTURE, LEVEL4_DEV_EVENTS } from "../src/dev/fixtures/level4_run";

const RUN = "run_l4";
const E = "level4_agentic";
const T0 = Date.parse("2026-10-22T16:00:00Z");
const at = (ms: number): string => new Date(T0 + ms).toISOString();

function turnEv(ms: number, fields: Record<string, unknown>): BattleEvent {
  return { event: "agent_turn", run_id: RUN, engine: E, ts: at(ms), ...fields } as unknown as BattleEvent;
}

const started = (ms: number, stage: string): BattleEvent =>
  ({ event: "stage_started", run_id: RUN, engine: E, stage, label: stage, service: "svc", request: {}, ts: at(ms) }) as BattleEvent;
const completed = (ms: number, stage: string, payload: Record<string, unknown> = {}): BattleEvent =>
  ({ event: "stage_completed", run_id: RUN, engine: E, stage, duration_ms: 5, payload, ts: at(ms) }) as BattleEvent;

function timelineOf(events: readonly BattleEvent[]): EngineTimeline {
  const runStarted = { event: "run_started", run_id: RUN, engines: [E], demo_mode: "replay", question_id: null, ts: at(0) } as BattleEvent;
  const state = reduceStreamEvents([runStarted, ...events] as TimelineEvent[]);
  return state.timelines[E];
}

// ---------------------------------------------------------------------------
// Ordering + grouping
// ---------------------------------------------------------------------------

test("reducer keeps agent_turn events in arrival order with seq indices", () => {
  const tl = timelineOf([
    started(1, "agent_turn"),
    turnEv(10, { turn_index: 2, kind: "model" }),
    turnEv(20, { turn_index: 1, kind: "tool_call", tool_name: "search_procurement_corpus", tool_args: { query: "x" } }),
  ]);
  assert.deepEqual(tl.agentTurns.map((t) => t.seq), [0, 1]);
  // Arrival order, never sorted by turn_index.
  assert.deepEqual(tl.agentTurns.map((t) => t.turnIndex), [2, 1]);
  assert.equal(tl.agentTurns[1].kind, "tool_call");
  assert.deepEqual(tl.agentTurns[1].toolArgs, { query: "x" });
});

test("groupAgentTurns pairs results by call_id, then by earliest open call of the same tool", () => {
  const turns = [
    { turn_index: 1, kind: "tool_call", tool_name: "search_procurement_corpus", call_id: "a" },
    { turn_index: 1, kind: "tool_call", tool_name: "search_procurement_corpus", call_id: "b" },
    { turn_index: 1, kind: "tool_result", tool_name: "search_procurement_corpus", call_id: "b", tool_result_summary: "B", duration_ms: 7 },
    { turn_index: 2, kind: "model" },
    { turn_index: 2, kind: "tool_call", tool_name: "check_amendments" },
    { turn_index: 2, kind: "tool_call", tool_name: "check_amendments" },
    { turn_index: 2, kind: "tool_result", tool_name: "check_amendments", tool_result_summary: "first" },
    { turn_index: 3, kind: "tool_result", tool_name: "verify_citations", tool_result_summary: "orphan" },
  ].map((raw, i) => normalizeAgentTurn({ ...raw, ts: at(i) }, i));
  const rows = groupAgentTurns(turns);
  assert.deepEqual(rows.map((r) => r.type), ["tool", "tool", "model", "tool", "tool", "tool"]);
  const [a, b, , c1, c2, orphan] = rows as ToolRow[];
  assert.equal(a.open, true);
  assert.equal(b.open, false);
  assert.equal(b.result?.resultSummary, "B");
  assert.equal(b.result?.durationMs, 7);
  assert.equal(c1.result?.resultSummary, "first", "no ids → earliest open call of the same tool");
  assert.equal(c2.open, true);
  assert.equal(orphan.call, null);
  assert.equal(orphan.result?.resultSummary, "orphan");
});

test("legacy frames without kind are one finished call; frames without tool_name are model turns", () => {
  const legacy = normalizeAgentTurn({ turn_index: 1, tool_name: "check_amendments", tool_args: { s: 1 }, tool_result_summary: "ok", duration_ms: 12, ts: at(0) }, 0);
  assert.equal(legacy.kind, "tool");
  const rows = groupAgentTurns([legacy]);
  assert.equal(rows[0].type, "tool");
  assert.equal((rows[0] as ToolRow).open, false);
  assert.equal((rows[0] as ToolRow).result?.durationMs, 12);
  assert.equal(normalizeAgentTurn({ turn_index: 1, ts: at(0) }, 0).kind, "model");
});

test("model turn tokens accept numbers or objects and never invent missing parts", () => {
  assert.equal(formatTokens(normalizeAgentTurn({ kind: "model", tokens: 1842, ts: "" }, 0).tokens), "1,842 tok");
  assert.equal(formatTokens(normalizeAgentTurn({ kind: "model", tokens: { prompt_tokens: 1420, output_tokens: 64 }, ts: "" }, 0).tokens), "in 1,420 · out 64");
  assert.equal(normalizeAgentTurn({ kind: "model", tokens: { foo: 1 }, ts: "" }, 0).tokens, null);
  assert.equal(formatTokens(null), "");
});

// ---------------------------------------------------------------------------
// Open-call detection + row state
// ---------------------------------------------------------------------------

test("open tool call is detected while running and resolved by its result", () => {
  const mid = timelineOf([started(1, "agent_turn"), turnEv(5, { turn_index: 1, kind: "tool_call", tool_name: "calculate_contract_pricing", tool_args: { expression: "1+1" } })]);
  assert.deepEqual([...openToolNames(mid.agentTurns)], ["calculate_contract_pricing"]);
  const row = groupAgentTurns(mid.agentTurns)[0] as ToolRow;
  assert.equal(toolRowState(row, "active"), "running");
  assert.equal(toolRowState(row, "done"), "no_result");
  assert.equal(toolRowState(row, "failed"), "failed");
  const after = timelineOf([
    started(1, "agent_turn"),
    turnEv(5, { turn_index: 1, kind: "tool_call", tool_name: "calculate_contract_pricing" }),
    turnEv(9, { turn_index: 1, kind: "tool_result", tool_name: "calculate_contract_pricing", duration_ms: 2 }),
  ]);
  assert.equal(openToolNames(after.agentTurns).size, 0);
});

// ---------------------------------------------------------------------------
// Diagram: tool node highlight mapping + edge flow
// ---------------------------------------------------------------------------

/** Backend descriptor shape (core/architectures.py _level4): agent + a single generic tools node, no tool_names. */
const BACKEND_NODES: ArchNode[] = [
  { id: "user", label: "User", service: "Browser", kind: "actor", col: 0, row: 1, stage_ids: [] },
  { id: "agent", label: "ADK agent loop", service: "ADK", kind: "agent", col: 1, row: 1, stage_ids: ["query_prep", "agent_turn"] },
  { id: "tools", label: "Tools", service: "BigQuery", kind: "api", col: 2, row: 1, stage_ids: ["agent_turn"] },
  { id: "llm", label: "Final response", service: "ADK", kind: "model", col: 4, row: 1, stage_ids: ["synthesize"] },
];
const BACKEND_EDGES = [
  { from: "user", to: "agent" },
  { from: "agent", to: "tools" },
  { from: "tools", to: "agent" },
  { from: "agent", to: "llm" },
];

test("toolNodeBindings: explicit tool_names win; otherwise an agent_turn-only node is the generic tools node", () => {
  const explicit = toolNodeBindings(LEVEL4_DEV_ARCHITECTURE.nodes);
  assert.deepEqual([...explicit.keys()].sort(), ["t_amend", "t_calc", "t_search", "t_verify"]);
  const calc = explicit.get("t_calc");
  assert.ok(calc !== "*" && calc?.has("calculate_contract_pricing"));
  const fallback = toolNodeBindings(BACKEND_NODES);
  assert.deepEqual([...fallback.entries()], [["tools", "*"]]);
});

test("tool node is active only while its call is open; done afterwards with summed backend ms; failed if the stage fails mid-call", () => {
  const bindings = toolNodeBindings(LEVEL4_DEV_ARCHITECTURE.nodes);
  const calc = bindings.get("t_calc")!;
  const search = bindings.get("t_search")!;
  const open = timelineOf([started(1, "agent_turn"), turnEv(5, { turn_index: 1, kind: "tool_call", tool_name: "calculate_contract_pricing" })]);
  const v = deriveToolNodeView(calc, open);
  assert.equal(v.status, "active");
  assert.equal(v.startedAt, T0 + 5);
  assert.equal(deriveToolNodeView(search, open).status, "idle", "other tools stay idle");

  const done = timelineOf([
    started(1, "agent_turn"),
    turnEv(5, { kind: "tool_call", tool_name: "calculate_contract_pricing" }),
    turnEv(7, { kind: "tool_result", tool_name: "calculate_contract_pricing", duration_ms: 2 }),
    turnEv(8, { kind: "tool_call", tool_name: "calculate_contract_pricing" }),
    turnEv(9, { kind: "tool_result", tool_name: "calculate_contract_pricing", duration_ms: 3 }),
  ]);
  assert.deepEqual(deriveToolNodeView(calc, done), { status: "done", stageId: "agent_turn", durationMs: 5, startedAt: null });
  assert.equal(toolNodeSummary(calc, done), "calculate ×2");

  const failed = timelineOf([
    started(1, "agent_turn"),
    turnEv(5, { kind: "tool_call", tool_name: "calculate_contract_pricing" }),
    { event: "stage_failed", run_id: RUN, engine: E, stage: "agent_turn", duration_ms: 9, error_type: "TimeoutError", error: "x", ts: at(9) } as BattleEvent,
  ]);
  assert.equal(deriveToolNodeView(calc, failed).status, "failed");
});

test("agent ⇄ tool edges flow only while a call is open (explicit and fallback descriptors)", () => {
  const open = timelineOf([started(1, "agent_turn"), turnEv(5, { kind: "tool_call", tool_name: "verify_citations" })]);
  assert.deepEqual([...toolFlowEdges(LEVEL4_DEV_ARCHITECTURE.edges, LEVEL4_DEV_ARCHITECTURE.nodes, open)], ["agent->t_verify"]);
  assert.deepEqual([...toolFlowEdges(BACKEND_EDGES, BACKEND_NODES, open)].sort(), ["agent->tools", "tools->agent"]);
  const closed = timelineOf([
    started(1, "agent_turn"),
    turnEv(5, { kind: "tool_call", tool_name: "verify_citations" }),
    turnEv(6, { kind: "tool_result", tool_name: "verify_citations" }),
  ]);
  assert.equal(toolFlowEdges(BACKEND_EDGES, BACKEND_NODES, closed).size, 0);
  const stageDone = timelineOf([started(1, "agent_turn"), turnEv(5, { kind: "tool_call", tool_name: "verify_citations" }), completed(9, "agent_turn")]);
  assert.equal(toolFlowEdges(BACKEND_EDGES, BACKEND_NODES, stageDone).size, 0, "no flow after the stage ended");
});

// ---------------------------------------------------------------------------
// Claims + calculator + header
// ---------------------------------------------------------------------------

test("claimsDiff: resolved / still unsupported / newly unsupported; strings and objects accepted", () => {
  const before = parseClaims(["A is true.", { claim: "B costs $5.", status: "unsupported" }, { claim: "C", supported: true }])!;
  const after = parseClaims([{ text: "b  COSTS $5." }, "D appears."])!;
  assert.deepEqual(claimsDiff(before, after), [
    { text: "A is true.", status: "resolved" },
    { text: "B costs $5.", status: "still_unsupported" },
    { text: "D appears.", status: "new_unsupported" },
  ]);
  assert.deepEqual(claimsDiff(before, []).map((r) => r.status), ["resolved", "resolved"]);
  assert.equal(parseClaims("nope"), null);
});

test("calculate_contract_pricing renders the normalized expression and value", () => {
  assert.equal(normalizeExpression("2000*245.00"), "2000 × 245.00");
  assert.equal(normalizeExpression("(490000.00+312500.00)/12 - 5"), "(490000.00 + 312500.00) ÷ 12 − 5");
  const rows = groupAgentTurns(
    [
      { kind: "tool_call", tool_name: "calculate_contract_pricing", tool_args: { expression: "2000 * 245.00" } },
      { kind: "tool_result", tool_name: "calculate_contract_pricing", tool_result: { value: 490000 } },
      { kind: "tool_call", tool_name: "calculate_contract_pricing", tool_args: { expression: "1200*99.5" } },
      { kind: "tool_result", tool_name: "calculate_contract_pricing", tool_result_summary: "1200 * 99.5 = 119400.0" },
      { kind: "tool_call", tool_name: "calculate_contract_pricing", tool_args: { expression: "3*4" } },
    ].map((r, i) => normalizeAgentTurn({ ...r, ts: at(i) }, i))
  ) as ToolRow[];
  assert.equal(pricingView(rows[0])?.line, "2000 × 245.00 = 490,000.00");
  assert.equal(pricingView(rows[1])?.line, "1200 × 99.5 = 119,400.00", "value parsed from the summary when no structured result");
  assert.deepEqual(pricingView(rows[2]), { expression: "3 × 4", value: null, line: "3 × 4" }, "open call: no value invented");
});

test("timeline header prefers stage payload counts and falls back to counting events", () => {
  const tl = timelineOf(LEVEL4_DEV_EVENTS.filter((e) => e.event === "agent_turn" || e.event === "run_started"));
  const rows = groupAgentTurns(tl.agentTurns);
  const live = timelineHeader(rows, null);
  assert.deepEqual(live, { turns: 6, toolCalls: 7, repairs: 1, source: "events" });
  const payload = parseAgentStagePayload({ turns: 6, tool_calls_by_name: { a: 2, b: 5 }, repair_turns: 1, unsupported_claims_before: ["x"], unsupported_claims_after: [], final_answer_source: "adk_final_response" });
  assert.equal(headerLine(timelineHeader(rows, payload)), "6 turns · 7 tool calls · repairs 1");
  assert.equal(payload?.finalAnswerSource, "adk_final_response");
  assert.equal(agentStageSummary({ turns: 1, tool_calls_by_name: { a: 1 }, repair_turns: 0 }), "1 turn · 1 tool call · repairs 0");
  assert.equal(summarizeStage("agent_turn", { turns: 3, tool_calls_by_name: { a: 4 }, repair_turns: 1 }), "3 turns · 4 tool calls · repairs 1");
  assert.equal(parseAgentStagePayload({ unrelated: 1 }), null);
});

test("verify_citations rows expose their own claims from tool_result", () => {
  const rows = groupAgentTurns(
    [
      { kind: "tool_call", tool_name: "verify_citations", tool_args: { claims: ["a"] } },
      { kind: "tool_result", tool_name: "verify_citations", tool_result: { claims: [{ claim: "a", supported: false, reason: "no chunk" }] } },
    ].map((r, i) => normalizeAgentTurn({ ...r, ts: at(i) }, i))
  ) as ToolRow[];
  assert.deepEqual(verifyResultClaims(rows[0]), [{ text: "a", status: "unsupported", chunkId: null, reason: "no chunk" }]);
});

test("tool styles carry a text label and a non-colour icon key", () => {
  for (const name of ["search_procurement_corpus", "check_amendments", "calculate_contract_pricing", "verify_citations"]) {
    const s = toolStyle(name);
    assert.ok(s.label.length > 0 && s.icon !== "tool" && s.tone !== "neutral", name);
  }
  assert.deepEqual(toolStyle("other_tool"), { label: "other_tool", tone: "neutral", icon: "tool" });
});

// ---------------------------------------------------------------------------
// Safe mode
// ---------------------------------------------------------------------------

test("backend shape: text_preview, tool_result status=error, count-valued claims + *_list keys, summary-only calculator", () => {
  const model = normalizeAgentTurn({ kind: "model", text_preview: "Let me search", ts: "" }, 0);
  assert.equal(model.text, "Let me search");
  const rows = groupAgentTurns(
    [
      { kind: "tool_call", tool_name: "check_amendments", call_id: "x" },
      { kind: "tool_result", tool_name: "check_amendments", call_id: "x", status: "error", tool_result_summary: "argument error: missing section" },
      { kind: "tool_call", tool_name: "calculate_contract_pricing", call_id: "y", tool_args: { expression: "hours * rate", variables: { hours: 2000, rate: 245 } } },
      { kind: "tool_result", tool_name: "calculate_contract_pricing", call_id: "y", status: "ok", tool_result_summary: "2000 * 245.00 = 490000.0" },
    ].map((r, i) => normalizeAgentTurn({ ...r, ts: at(i) }, i))
  ) as ToolRow[];
  assert.equal(toolRowState(rows[0], "done"), "error");
  assert.equal(toolRowState(rows[1], "done"), "done");
  assert.equal(pricingView(rows[1])?.line, "2000 × 245.00 = 490,000.00", "substituted expression from the summary wins over symbolic args");
  const p = parseAgentStagePayload({ unsupported_claims_before: 2, unsupported_claims_after: 0, unsupported_before_list: ["A", "B"], unsupported_after_list: [], verify_calls: 2, repair_turns: 1 });
  assert.equal(p?.beforeCount, 2);
  assert.equal(p?.afterCount, 0);
  assert.equal(p?.verifyCalls, 2);
  assert.deepEqual(claimsDiff(p!.before!, p!.after!).map((r) => r.status), ["resolved", "resolved"]);
  assert.equal(finalCheckLine(parseAgentStagePayload({ final_answer_check: { checked_claims: 31, unsupported_claims: 0, unsupported_list: [] } })), "final answer check: 31 claims checked · 0 unsupported");
  assert.equal(normalizeAgentTurn({ kind: "tool_call", tool_name: "verify_citations", tool_args: '{"cited_chunk_ids": ["a", "b"]}', ts: "" }, 0).toolArgs?.cited_chunk_ids?.toString(), "a,b");
  const truncated = normalizeAgentTurn({ kind: "tool_call", tool_name: "verify_citations", tool_args: '{"cited_chunk_ids": ["a", "b', ts: "" }, 0);
  assert.equal(truncated.toolArgs, null);
  assert.equal(truncated.toolArgsText, '{"cited_chunk_ids": ["a", "b', "truncated JSON string kept verbatim");
  assert.equal(pricingView(groupAgentTurns([
    normalizeAgentTurn({ kind: "tool_call", tool_name: "calculate_contract_pricing", call_id: "z", tool_args: { expression: "h * r * (1 - d)" }, ts: "" }, 0),
    normalizeAgentTurn({ kind: "tool_result", tool_name: "calculate_contract_pricing", call_id: "z", tool_result_summary: "2000.0 * 245.0 * (1 - 0.05) = 465500.0", ts: "" }, 1),
  ])[0] as ToolRow)?.line, "2000.0 × 245.0 × (1 − 0.05) = 465,500.00", "real replay calculator summary");
  const countsOnly = parseAgentStagePayload({ unsupported_claims_before: 1, unsupported_claims_after: 1 });
  assert.equal(countsOnly?.before, null);
  assert.equal(countsOnly?.beforeCount, 1);
});

test("safe mode masks identity inside tool args, summaries and structured results", () => {
  const health = { project_id: "acme-proj-123", gcs_bucket: "acme-proj-123-rag-arena-corpus" };
  const state = reduceStreamEvents([
    { event: "run_started", run_id: RUN, engines: [E], demo_mode: "replay", question_id: null, ts: at(0) } as BattleEvent,
    turnEv(1, { kind: "tool_call", tool_name: "search_procurement_corpus", tool_args: { table: "acme-proj-123.rag_arena.document_chunks_v3", uri: "gs://acme-proj-123-rag-arena-corpus/x.pdf" } }),
    turnEv(2, { kind: "tool_result", tool_name: "search_procurement_corpus", tool_result_summary: "scanned projects/987654321/locations/us", tool_result: { job: "acme-proj-123:US.job_1" } }),
  ] as TimelineEvent[]);
  const masked = maskDeep(state, health);
  const text = JSON.stringify(masked.timelines[E].agentTurns);
  assert.ok(!text.includes("acme-proj-123"), text);
  assert.ok(!/projects\/\d{6,}/.test(text), text);
  assert.ok(text.includes("${GCP_PROJECT_ID}.rag_arena") && text.includes("gs://${GCS_CORPUS_BUCKET}/x.pdf") && text.includes("projects/${GCP_PROJECT_NUMBER}"));
});

// ---------------------------------------------------------------------------
// Compare teaser + fixture
// ---------------------------------------------------------------------------

test("compare teaser on Level 4 covers Levels 1–3", () => {
  const levels = [1, 2, 3, 4, 5].map((order) => ({ level_id: `l${order}`, order, enabled: order <= 4 }));
  const lower = lowerEnabledLevels(levels, { order: 4 });
  assert.deepEqual(lower.map((l) => l.short), ["Level 1", "Level 2", "Level 3"]);
  assert.equal(teaserTitle(lower), "Same question on Levels 1–3");
});

test("DEV fixture: synthetic flag on every frame, a repair turn, calculator + verify rows, adk_runner_used", () => {
  assert.ok(LEVEL4_DEV_EVENTS.every((e) => (e as unknown as { synthetic_dev_fixture?: boolean }).synthetic_dev_fixture === true));
  const tl = timelineOf(LEVEL4_DEV_EVENTS.filter((e) => e.event !== "run_started"));
  const rows = groupAgentTurns(tl.agentTurns);
  assert.ok(rows.some((r) => (r.type === "model" ? r.turn.repair : r.repair)), "has a repair turn");
  const calc = rows.filter((r): r is ToolRow => r.type === "tool" && r.toolName === "calculate_contract_pricing");
  assert.equal(pricingView(calc[0])?.line, "2000 × 245.00 = 490,000.00");
  assert.equal(rows.filter((r) => r.type === "tool" && r.open).length, 0);
  assert.equal(tl.stages.agent_turn.status, "done");
  assert.equal(tl.answer?.flags.adk_runner_used, true);
  const p = parseAgentStagePayload(tl.stages.agent_turn.payload);
  assert.deepEqual(claimsDiff(p!.before!, p!.after!).map((r) => r.status), ["resolved"]);
});

test("diagram: paired agent ⇄ tool edges run parallel; return edges start at the source's left side", async () => {
  const { buildEdgePaths, layoutNodes, visibleEdgeLabels, NODE_W } = await import("../src/features/levels/diagramModel");
  const nodes: ArchNode[] = [
    { id: "agent", label: "a", service: "s", kind: "agent", col: 1, row: 1, stage_ids: ["agent_turn"] },
    { id: "tool", label: "t", service: "s", kind: "process", col: 2, row: 0, stage_ids: ["agent_turn"], tool_names: ["x"] },
    { id: "mid", label: "m", service: "s", kind: "model", col: 3, row: 1, stage_ids: [] },
    { id: "far", label: "f", service: "s", kind: "api", col: 4, row: 0, stage_ids: ["agent_turn"], tool_names: ["x"] },
  ];
  const layout = layoutNodes(nodes);
  const box = (id: string) => layout.boxes.find((b) => b.node.id === id)!;
  const idle = { status: "idle" as const, stageId: null, durationMs: null, startedAt: null };
  const paths = buildEdgePaths(
    [{ from: "agent", to: "tool", label: "tool call" }, { from: "tool", to: "agent", label: "top-5 chunks" }, { from: "far", to: "tool", label: "top-5" }],
    layout.boxes,
    { agent: idle, tool: idle, mid: idle, far: idle }
  );
  const [fwd, back, arc] = paths;
  assert.ok(fwd.d.startsWith(`M ${box("agent").x + NODE_W} `), fwd.d);
  assert.ok(back.d.startsWith(`M ${box("tool").x} `), `return edge leaves from the tool's left side: ${back.d}`);
  assert.ok(back.d.endsWith(`${box("agent").x + NODE_W} ${box("agent").y + 92 / 2 + 7}`), back.d);
  assert.ok(back.paired && fwd.paired && !arc.paired);
  assert.ok(back.labelRoom < 60 && fwd.labelRoom < 60, "narrow column gap");
  assert.ok(arc.labelRoom > 2 * NODE_W, "under-box arc has room for its label");
  assert.deepEqual([...visibleEdgeLabels(paths)], [2], "paired labels without room are hover-only; the arc label is drawn");
  const dup = buildEdgePaths([{ from: "agent", to: "mid", label: "x" }, { from: "agent", to: "mid", label: "y" }], layout.boxes, { agent: idle, tool: idle, mid: idle, far: idle });
  assert.deepEqual([...visibleEdgeLabels(dup)], [0], "overlapping label skipped");
    assert.ok(arc.d.startsWith(`M ${box("far").x + NODE_W / 2} ${box("far").y + 92}`), `same-row non-adjacent return arcs under the boxes: ${arc.d}`);
});
