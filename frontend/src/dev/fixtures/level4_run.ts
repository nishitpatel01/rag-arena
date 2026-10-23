/**
 * DEV-ONLY Level 4 fixture. Imported exclusively behind `if (import.meta.env.DEV)` so it tree-shakes out of
 * `npm run build`. Every value is a placeholder following the PROVISIONAL Level 4 contract:
 * live `agent_turn` frames {turn_index, kind model|tool_call|tool_result, tool_name, tool_args,
 * tool_result_summary, tool_result, duration_ms, tokens, call_id, repair}; `agent_turn` stage payload
 * {turns, tool_calls_by_name, unsupported_claims_before/after, repair_turns, final_answer_source};
 * `synthesize` payload = ADK final response. Includes one repair turn after verify_citations.
 * Every frame is flagged `synthetic_dev_fixture: true`; the Shell shows the "DEV FIXTURE — not real data" banner.
 */

import type { ArchitectureDescriptor, BattleEvent, JsonObject } from "../../types/arena";
import type { TimedFixtureEvent } from "./level1_run";

const RUN_ID = "devfixture-level4-0001";
const ENGINE = "level4_agentic";
const T0 = Date.parse("2026-10-22T16:00:00.000Z");
const F = true as const;

const ts = (offsetMs: number): string => new Date(T0 + offsetMs).toISOString();
const ev = (e: JsonObject): TimedFixtureEvent["event"] => ({ ...e, synthetic_dev_fixture: F }) as unknown as TimedFixtureEvent["event"];
const base = { run_id: RUN_ID, engine: ENGINE };

const QUERY =
  "[DEV FIXTURE] What is the combined first-year cost of CLIN-0004-AB across Solicitation 01 and Solicitation 02 after all amendments?";

const CLAIM_RATE = "Solicitation 02 prices CLIN-0004-AB at $245.00 per hour.";
const CLAIM_TOTAL = "The combined first-year cost is $802,500.00.";
const CLAIM_SLA = "Both solicitations require 99.95% availability.";

let clock = 0;
const out: TimedFixtureEvent[] = [];
function push(dt: number, e: JsonObject): void {
  clock += dt;
  out.push({ t_rel: dt, event: ev({ ...e, ts: ts(clock) }) });
}
const turn = (dt: number, e: JsonObject): void => push(dt, { ...base, event: "agent_turn", ...e });

push(0, { event: "run_started", run_id: RUN_ID, engines: [ENGINE], demo_mode: "dev_fixture", question_id: "q_cross_solicitation_total_cost", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" });
push(30, { ...base, event: "stage_started", stage: "query_prep", label: "Query preparation", service: "Python (in-process)", request: { query: QUERY } });
push(15, { ...base, event: "stage_completed", stage: "query_prep", duration_ms: 3, payload: { original_query: QUERY, prepared_query: QUERY, expansions: [] } });
push(20, {
  ...base, event: "stage_started", stage: "agent_turn", label: "ADK agent loop", service: "Google ADK Runner · gemini-4-argon (dev fixture)",
  request: { runner: "InMemoryRunner", model: "gemini-4-argon", max_turns: 8, tools: ["search_procurement_corpus", "check_amendments", "calculate_contract_pricing", "verify_citations"] },
});

turn(900, { turn_index: 1, kind: "model", tokens: { prompt_tokens: 1420, output_tokens: 64, thinking_tokens: 210 }, duration_ms: 880 });
turn(10, { turn_index: 1, kind: "tool_call", call_id: "c1", tool_name: "search_procurement_corpus", tool_args: { query: "CLIN-0004-AB hourly rate", solicitation_id: "solicitation_01", top_k: 5 } });
turn(1300, { turn_index: 1, kind: "tool_result", call_id: "c1", tool_name: "search_procurement_corpus", tool_result_summary: "5 chunks · top: 04_CLIN_Rate_Card.pdf p.2 (table) · 1 amendment chunk", duration_ms: 1290 });
turn(700, { turn_index: 2, kind: "model", tokens: { prompt_tokens: 2310, output_tokens: 48, thinking_tokens: 120 }, duration_ms: 690 });
turn(10, { turn_index: 2, kind: "tool_call", call_id: "c2", tool_name: "check_amendments", tool_args: { section_id: "CLIN-0004-AB", solicitation_id: "solicitation_02" } });
turn(800, { turn_index: 2, kind: "tool_result", call_id: "c2", tool_name: "check_amendments", tool_result_summary: "Amendment 0002 supersedes CLIN-0004-AB rate (effective 2026-03-01)", duration_ms: 780 });
turn(600, { turn_index: 3, kind: "model", tokens: { prompt_tokens: 2690, output_tokens: 72, thinking_tokens: 95 }, duration_ms: 590 });
turn(10, { turn_index: 3, kind: "tool_call", call_id: "c3", tool_name: "calculate_contract_pricing", tool_args: { expression: "2000 * 245.00", label: "Solicitation 02 year-1" } });
turn(40, { turn_index: 3, kind: "tool_result", call_id: "c3", tool_name: "calculate_contract_pricing", tool_result_summary: "2000 * 245.00 = 490000.00", tool_result: { normalized_expression: "2000 * 245.00", value: 490000 }, duration_ms: 2 });
turn(10, { turn_index: 3, kind: "tool_call", call_id: "c4", tool_name: "calculate_contract_pricing", tool_args: { expression: "490000.00 + 312500.00", label: "combined year-1" } });
turn(40, { turn_index: 3, kind: "tool_result", call_id: "c4", tool_name: "calculate_contract_pricing", tool_result_summary: "490000.00 + 312500.00 = 802500.00", tool_result: { normalized_expression: "490000.00 + 312500.00", value: 802500 }, duration_ms: 1 });
turn(650, { turn_index: 4, kind: "model", tokens: { prompt_tokens: 2950, output_tokens: 180, thinking_tokens: 60 }, duration_ms: 640 });
turn(10, { turn_index: 4, kind: "tool_call", call_id: "c5", tool_name: "verify_citations", tool_args: { claims: [CLAIM_RATE, CLAIM_TOTAL, CLAIM_SLA] } });
turn(500, {
  turn_index: 4, kind: "tool_result", call_id: "c5", tool_name: "verify_citations", duration_ms: 480,
  tool_result_summary: "2 of 3 claims supported · 1 unsupported",
  tool_result: { claims: [{ claim: CLAIM_RATE, supported: true, chunk_id: "devfix-l4-007" }, { claim: CLAIM_TOTAL, supported: true, chunk_id: "devfix-l4-002" }, { claim: CLAIM_SLA, supported: false, reason: "no retrieved chunk states 99.95% for Solicitation 02" }] },
});
turn(700, { turn_index: 5, kind: "model", repair: true, tokens: { prompt_tokens: 3310, output_tokens: 40, thinking_tokens: 80 }, duration_ms: 690 });
turn(10, { turn_index: 5, kind: "tool_call", call_id: "c6", repair: true, tool_name: "search_procurement_corpus", tool_args: { query: "availability SLA percentage", solicitation_id: "solicitation_02", top_k: 5 } });
turn(1100, { turn_index: 5, kind: "tool_result", call_id: "c6", repair: true, tool_name: "search_procurement_corpus", tool_result_summary: "5 chunks · top: 05_SLA_and_QASP_Requirements.pdf p.2 (table): 99.9% availability", duration_ms: 1080 });
turn(10, { turn_index: 5, kind: "tool_call", call_id: "c7", repair: true, tool_name: "verify_citations", tool_args: { claims: [CLAIM_RATE, CLAIM_TOTAL, "Solicitation 01 requires 99.95% and Solicitation 02 requires 99.9% availability."] } });
turn(450, {
  turn_index: 5, kind: "tool_result", call_id: "c7", repair: true, tool_name: "verify_citations", duration_ms: 430,
  tool_result_summary: "3 of 3 claims supported",
  tool_result: { claims: [{ claim: CLAIM_RATE, supported: true }, { claim: CLAIM_TOTAL, supported: true }, { claim: "Solicitation 01 requires 99.95% and Solicitation 02 requires 99.9% availability.", supported: true }] },
});
turn(800, { turn_index: 6, kind: "model", tokens: { prompt_tokens: 3720, output_tokens: 210, thinking_tokens: 40 }, duration_ms: 790, text: "Final response composed from verified claims." });
push(20, {
  ...base, event: "stage_completed", stage: "agent_turn", duration_ms: 9420,
  payload: {
    turns: 6,
    tool_calls_by_name: { search_procurement_corpus: 2, check_amendments: 1, calculate_contract_pricing: 2, verify_citations: 2 },
    unsupported_claims_before: [{ claim: CLAIM_SLA, status: "unsupported", reason: "no retrieved chunk states 99.95% for Solicitation 02" }],
    unsupported_claims_after: [],
    repair_turns: 1,
    final_answer_source: "adk_final_response",
  },
});
push(15, { ...base, event: "stage_started", stage: "synthesize", label: "ADK final response", service: "Google ADK · gemini-4-argon (dev fixture)", request: { source: "ADK Runner final event", model: "gemini-4-argon" } });
const ANSWER =
  "[DEV FIXTURE] Combined first-year cost of CLIN-0004-AB: **$802,500.00** — Solicitation 02 at $245.00/h × 2,000 h = $490,000.00 (Amendment 0002 rate) plus Solicitation 01's $312,500.00. Availability: 99.95% (Sol. 01) vs 99.9% (Sol. 02).";
push(10, { ...base, event: "stage_completed", stage: "synthesize", duration_ms: 1, payload: { model: "gemini-4-argon", prompt_tokens: 16400, output_tokens: 614, thinking_tokens: 605, model_calls: 6, answer: ANSWER } });
push(30, {
  ...base, event: "answer_complete", answer: ANSWER,
  citations: [
    { chunk_id: "devfix-l4-007", doc_name: "07_Amendment_0002_Vendor_QA.pdf", solicitation_id: "solicitation_02", page_number: 1, section_id: "AMD-0002", element_type: "table", layout_source: "documentai_layout", bbox_source: "pdf_text_layer", bbox: { page: 1, x0: 0.07, y0: 0.3, x1: 0.93, y1: 0.46 }, excerpt: "CLIN-0004-AB | 245.00" },
    { chunk_id: "devfix-l4-002", doc_name: "04_CLIN_Rate_Card.pdf", solicitation_id: "solicitation_01", page_number: 2, section_id: "CLIN-0004", element_type: "table", layout_source: "documentai_layout", bbox_source: "page", bbox: { page: 2, x0: 0, y0: 0, x1: 1, y1: 1 }, excerpt: "CLIN-0004-AB | 156.25" },
  ],
  latency_ms: 9600, cost_usd: 0.0611,
  usage: { prompt_tokens: 16400, output_tokens: 614, embedding_tokens: 48, model: "gemini-4-argon" },
  flags: { reranker_used: true, adk_runner_used: true, layout_source: "documentai_layout+gemini_vision" },
});
push(30, { ...base, event: "stage_started", stage: "evaluate", label: "LLM-as-a-judge", service: "Vertex AI · Claude Opus 5.5 (dev fixture)", request: { model: "claude-opus-5-5" } });
push(1100, { ...base, event: "stage_completed", stage: "evaluate", duration_ms: 1080, payload: { model: "claude-opus-5-5", input_tokens: 2610, output_tokens: 160 } });
push(20, { ...base, event: "eval_complete", metrics: { groundedness: 1, qa_relevance: 0.9, context_precision: 0.8 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 1080, rationale: "[DEV FIXTURE] synthetic rationale" });
push(20, { event: "battle_complete", run_id: RUN_ID, engines_completed: [ENGINE], engines_failed: [] });

export const LEVEL4_DEV_FIXTURE: readonly TimedFixtureEvent[] = out;

/** Same frames typed as battle events (tests). */
export const LEVEL4_DEV_EVENTS: readonly BattleEvent[] = out.map((f) => f.event);

/** Mirrors the backend Level 4 descriptor plus per-tool nodes carrying `tool_names` (design metadata only). */
export const LEVEL4_DEV_ARCHITECTURE: ArchitectureDescriptor = {
  level_id: "level4_agentic",
  nodes: [
    { id: "user", label: "User question", service: "Browser", kind: "actor", col: 0, row: 1, stage_ids: ["query_prep"] },
    { id: "agent", label: "ADK agent loop", service: "Google ADK Runner · gemini-4-argon", kind: "agent", col: 1, row: 1, stage_ids: ["agent_turn"] },
    { id: "t_search", label: "search corpus", service: "Level 3 retrieval (BigQuery)", kind: "api", col: 2, row: 0, stage_ids: ["agent_turn"], tool_names: ["search_procurement_corpus"] },
    { id: "t_amend", label: "check amendments", service: "BigQuery amends_section", kind: "store", col: 2, row: 1, stage_ids: ["agent_turn"], tool_names: ["check_amendments"] },
    { id: "t_calc", label: "calculate pricing", service: "AST-safe calculator", kind: "compute", col: 3, row: 0, stage_ids: ["agent_turn"], tool_names: ["calculate_contract_pricing"] },
    { id: "t_verify", label: "verify citations", service: "claim ↔ chunk check", kind: "compute", col: 3, row: 1, stage_ids: ["agent_turn"], tool_names: ["verify_citations"] },
    { id: "llm", label: "Final response", service: "Google ADK · gemini-4-argon", kind: "model", col: 4, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI · claude-opus-5-5", kind: "model", col: 5, row: 1, stage_ids: ["evaluate"] },
  ],
  edges: [
    { from: "user", to: "agent", label: "text" },
    { from: "agent", to: "t_search", label: "tool call" },
    { from: "agent", to: "t_amend", label: "tool call" },
    { from: "agent", to: "t_calc", label: "tool call" },
    { from: "agent", to: "t_verify", label: "tool call" },
    { from: "agent", to: "llm", label: "final turn" },
    { from: "llm", to: "judge", label: "answer" },
  ],
  ingest_nodes: [],
  ingest_edges: [],
};
