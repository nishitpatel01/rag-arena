/**
 * Level 4 (Agentic RAG with Google ADK): pure shaping of `agent_turn` events and the `agent_turn` stage payload.
 * No React, no timers. Every value comes from an SSE event; missing fields stay null (never invented).
 *
 * Event contract (provisional, coded defensively): {turn_index, kind: "model"|"tool_call"|"tool_result",
 * tool_name?, tool_args?, tool_result_summary?, tool_result?, duration_ms?, tokens?, call_id?, repair?, ts}.
 * Legacy events without `kind` (spec §6.3: one event per finished tool call) are treated as a combined call+result.
 */

import type { AgentTurnKind, AgentTurnRecord, ArchNode, EngineTimeline, JsonObject, StageStatus } from "../../types/arena";
import { parseTs } from "../../lib/format";

/** Bound on agent events kept per engine (the SSE stream itself is capped at 600 frames). */
export const MAX_AGENT_TURNS = 400;
/** Bound on claims rendered per list. */
export const MAX_CLAIMS = 50;

const isObj = (v: unknown): v is JsonObject => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim().length > 0 ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

// ---------------------------------------------------------------------------
// Event normalisation (used by the reducer)
// ---------------------------------------------------------------------------

function kindOf(raw: JsonObject): AgentTurnKind {
  const k = raw.kind;
  if (k === "model" || k === "tool_call" || k === "tool_result") return k;
  // Legacy shape: a tool name without `kind` = one finished call (args + summary + duration in one event).
  return str(raw.tool_name) ? "tool" : "model";
}

/** Bound on tool_args text kept verbatim (backend truncates at 1200 chars). */
export const MAX_TOOL_ARGS_TEXT = 4000;

/** `tool_args` may be an object or a JSON string (possibly truncated): parsed object when valid, else the raw text. */
export function toolArgsOf(v: unknown): { readonly toolArgs: JsonObject | null; readonly toolArgsText: string | null } {
  if (isObj(v)) return { toolArgs: v, toolArgsText: null };
  if (typeof v !== "string" || v.trim().length === 0) return { toolArgs: null, toolArgsText: null };
  const text = v.slice(0, MAX_TOOL_ARGS_TEXT);
  try {
    const parsed: unknown = JSON.parse(text);
    if (isObj(parsed)) return { toolArgs: parsed, toolArgsText: null };
  } catch {
    // Truncated / non-JSON args are shown verbatim (never repaired or guessed).
  }
  return { toolArgs: null, toolArgsText: text };
}

/** One `agent_turn` SSE frame → timeline record. `seq` is the arrival index (the only ordering ever used). */
export function normalizeAgentTurn(raw: JsonObject, seq: number): AgentTurnRecord {
  const kind = kindOf(raw);
  const repair = raw.repair === true || raw.phase === "repair" || raw.is_repair === true;
  return {
    seq,
    turnIndex: num(raw.turn_index),
    kind,
    toolName: str(raw.tool_name),
    ...toolArgsOf(raw.tool_args),
    resultSummary: typeof raw.tool_result_summary === "string" ? raw.tool_result_summary : null,
    result: isObj(raw.tool_result) ? raw.tool_result : null,
    durationMs: num(raw.duration_ms),
    tokens: tokenCount(raw.tokens ?? raw.llm_tokens),
    callId: str(raw.call_id) ?? str(raw.function_call_id),
    repair,
    text: str(raw.text_preview) ?? str(raw.text) ?? str(raw.model_text),
    status: str(raw.status),
    ts: typeof raw.ts === "string" ? raw.ts : "",
  };
}

export interface TokenCount {
  readonly total: number | null;
  readonly prompt: number | null;
  readonly output: number | null;
  readonly thinking: number | null;
}

/** `tokens` may be a number or an object ({prompt_tokens|input, output_tokens|output, thinking_tokens, total}). */
export function tokenCount(v: unknown): TokenCount | null {
  if (num(v) !== null) return { total: v as number, prompt: null, output: null, thinking: null };
  if (!isObj(v)) return null;
  const prompt = num(v.prompt_tokens) ?? num(v.prompt) ?? num(v.input_tokens) ?? num(v.input);
  const output = num(v.output_tokens) ?? num(v.output) ?? num(v.candidates_tokens);
  const thinking = num(v.thinking_tokens) ?? num(v.thoughts_tokens) ?? num(v.thinking);
  const total = num(v.total_tokens) ?? num(v.total);
  if (prompt === null && output === null && thinking === null && total === null) return null;
  return { total, prompt, output, thinking };
}

export function formatTokens(t: TokenCount | null): string {
  if (!t) return "";
  const parts: string[] = [];
  if (t.prompt !== null) parts.push(`in ${t.prompt.toLocaleString("en-US")}`);
  if (t.output !== null) parts.push(`out ${t.output.toLocaleString("en-US")}`);
  if (t.thinking !== null) parts.push(`thinking ${t.thinking.toLocaleString("en-US")}`);
  if (parts.length === 0 && t.total !== null) parts.push(`${t.total.toLocaleString("en-US")} tok`);
  else if (t.total !== null) parts.push(`= ${t.total.toLocaleString("en-US")}`);
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Rows: model turns + call/result pairs, in arrival order
// ---------------------------------------------------------------------------

export interface ModelRow {
  readonly type: "model";
  readonly key: string;
  readonly turn: AgentTurnRecord;
}

export interface ToolRow {
  readonly type: "tool";
  readonly key: string;
  readonly toolName: string;
  /** The tool_call event (null when only a result arrived — shown as such, never invented). */
  readonly call: AgentTurnRecord | null;
  readonly result: AgentTurnRecord | null;
  /** A call without its result yet. */
  readonly open: boolean;
  readonly repair: boolean;
}

export type TimelineRow = ModelRow | ToolRow;

const UNKNOWN_TOOL = "(unnamed tool)";

/**
 * Groups events into rows in arrival order. A `tool_result` joins the open call with the same `call_id`;
 * without ids it joins the earliest open call of the same tool name. Unmatched results get their own row.
 */
export function groupAgentTurns(turns: readonly AgentTurnRecord[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  const openIdx: number[] = [];
  for (const t of turns.slice(0, MAX_AGENT_TURNS)) {
    const key = `e${t.seq}`;
    if (t.kind === "model") {
      rows.push({ type: "model", key, turn: t });
      continue;
    }
    const toolName = t.toolName ?? UNKNOWN_TOOL;
    if (t.kind === "tool") {
      rows.push({ type: "tool", key, toolName, call: t, result: t, open: false, repair: t.repair });
      continue;
    }
    if (t.kind === "tool_call") {
      rows.push({ type: "tool", key, toolName, call: t, result: null, open: true, repair: t.repair });
      openIdx.push(rows.length - 1);
      continue;
    }
    // tool_result
    const match = openIdx.findIndex((i) => {
      const row = rows[i] as ToolRow;
      if (t.callId && row.call?.callId) return row.call.callId === t.callId;
      return row.toolName === toolName;
    });
    if (match === -1) {
      rows.push({ type: "tool", key, toolName, call: null, result: t, open: false, repair: t.repair });
      continue;
    }
    const i = openIdx[match];
    const row = rows[i] as ToolRow;
    rows[i] = { ...row, result: t, open: false, repair: row.repair || t.repair };
    openIdx.splice(match, 1);
  }
  return rows;
}

export function openToolRows(rows: readonly TimelineRow[]): ToolRow[] {
  return rows.filter((r): r is ToolRow => r.type === "tool" && r.open);
}

/** Tool names with a call in flight. */
export function openToolNames(turns: readonly AgentTurnRecord[]): Set<string> {
  return new Set(openToolRows(groupAgentTurns(turns)).map((r) => r.toolName));
}

/** Row state shown to the user. `running` only while the agent_turn stage is active. */
export type ToolRowState = "running" | "done" | "error" | "no_result" | "failed";

export function toolRowState(row: ToolRow, stageStatus: StageStatus): ToolRowState {
  if (!row.open) return row.result?.status === "error" ? "error" : "done";
  if (stageStatus === "active") return "running";
  if (stageStatus === "failed") return "failed";
  return "no_result";
}

// ---------------------------------------------------------------------------
// agent_turn stage payload + header
// ---------------------------------------------------------------------------

export type ClaimStatus = "unsupported" | "supported" | "unknown";

export interface Claim {
  readonly text: string;
  readonly status: ClaimStatus;
  readonly chunkId: string | null;
  readonly reason: string | null;
}

/** Claim rows may be strings or objects ({claim|text|sentence, status|supported, chunk_id, reason}). */
export function parseClaims(raw: unknown, defaultStatus: ClaimStatus = "unsupported"): Claim[] | null {
  if (!Array.isArray(raw)) return null;
  const out: Claim[] = [];
  for (const item of raw.slice(0, MAX_CLAIMS)) {
    if (typeof item === "string") {
      if (item.trim()) out.push({ text: item, status: defaultStatus, chunkId: null, reason: null });
      continue;
    }
    if (!isObj(item)) continue;
    const text = str(item.claim) ?? str(item.text) ?? str(item.sentence);
    if (!text) continue;
    let status: ClaimStatus = defaultStatus;
    if (item.supported === true || item.status === "supported" || item.status === "ok") status = "supported";
    else if (item.supported === false || item.status === "unsupported") status = "unsupported";
    out.push({ text, status, chunkId: str(item.chunk_id), reason: str(item.reason) ?? str(item.note) });
  }
  return out;
}

export interface AgentStagePayload {
  readonly turns: number | null;
  readonly toolCallsByName: ReadonlyMap<string, number> | null;
  /** Claim texts (from `unsupported_before_list`, or an array-valued `unsupported_claims_before`). */
  readonly before: Claim[] | null;
  readonly after: Claim[] | null;
  /** Counts (number-valued `unsupported_claims_before/after`, or the list lengths). */
  readonly beforeCount: number | null;
  readonly afterCount: number | null;
  readonly verifyCalls: number | null;
  /** `final_answer_check{checked_claims, unsupported_claims, unsupported_list}` on the final answer. */
  readonly finalCheck: { readonly checked: number | null; readonly unsupported: number | null; readonly list: Claim[] | null } | null;
  readonly repairTurns: number | null;
  readonly finalAnswerSource: string | null;
}

export function parseAgentStagePayload(payload: JsonObject | null): AgentStagePayload | null {
  if (!payload) return null;
  let byName: Map<string, number> | null = null;
  if (isObj(payload.tool_calls_by_name)) {
    byName = new Map();
    for (const [k, v] of Object.entries(payload.tool_calls_by_name)) if (num(v) !== null) byName.set(k, v as number);
  }
  const before = parseClaims(payload.unsupported_before_list) ?? parseClaims(payload.unsupported_claims_before);
  const after = parseClaims(payload.unsupported_after_list) ?? parseClaims(payload.unsupported_claims_after);
  const view: AgentStagePayload = {
    turns: num(payload.turns),
    toolCallsByName: byName,
    before,
    after,
    beforeCount: num(payload.unsupported_claims_before) ?? before?.length ?? null,
    afterCount: num(payload.unsupported_claims_after) ?? after?.length ?? null,
    verifyCalls: num(payload.verify_calls),
    finalCheck: isObj(payload.final_answer_check)
      ? {
          checked: num(payload.final_answer_check.checked_claims),
          unsupported: num(payload.final_answer_check.unsupported_claims),
          list: parseClaims(payload.final_answer_check.unsupported_list),
        }
      : null,
    repairTurns: num(payload.repair_turns),
    finalAnswerSource: str(payload.final_answer_source),
  };
  const empty = Object.values(view).every((v) => v === null);
  return empty ? null : view;
}

export interface TimelineHeader {
  readonly turns: number;
  readonly toolCalls: number;
  readonly repairs: number;
  /** "payload" once the stage completed with counts; "events" while counting live. */
  readonly source: "payload" | "events";
}

/** "N turns · M tool calls · repairs R": stage payload counts when present, else counted from events. */
export function timelineHeader(rows: readonly TimelineRow[], payload: AgentStagePayload | null): TimelineHeader {
  const modelRows = rows.filter((r) => r.type === "model").length;
  const toolRows = rows.filter((r): r is ToolRow => r.type === "tool");
  const turnIdx = new Set(rows.map((r) => (r.type === "model" ? r.turn.turnIndex : r.call?.turnIndex ?? r.result?.turnIndex)).filter((v) => v !== null && v !== undefined));
  const evTurns = modelRows > 0 ? modelRows : turnIdx.size;
  const evRepairs = new Set(
    rows.filter((r) => (r.type === "model" ? r.turn.repair : r.repair)).map((r) => (r.type === "model" ? r.turn.turnIndex : r.call?.turnIndex ?? r.result?.turnIndex))
  ).size;
  const payloadCalls = payload?.toolCallsByName ? [...payload.toolCallsByName.values()].reduce((s, n) => s + n, 0) : null;
  const fromPayload = payload !== null && (payload.turns !== null || payloadCalls !== null || payload.repairTurns !== null);
  return {
    turns: payload?.turns ?? evTurns,
    toolCalls: payloadCalls ?? toolRows.length,
    repairs: payload?.repairTurns ?? evRepairs,
    source: fromPayload ? "payload" : "events",
  };
}

/** "final answer check: 31 claims checked · 0 unsupported" (null when not reported). */
export function finalCheckLine(p: AgentStagePayload | null): string | null {
  const f = p?.finalCheck;
  if (!f || (f.checked === null && f.unsupported === null)) return null;
  return `final answer check: ${f.checked ?? "—"} claims checked · ${f.unsupported ?? "—"} unsupported`;
}

export function headerLine(h: TimelineHeader): string {
  const plural = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;
  return `${plural(h.turns, "turn", "turns")} · ${plural(h.toolCalls, "tool call", "tool calls")} · repairs ${h.repairs}`;
}

/** Diagram summary for a completed agent_turn stage. */
export function agentStageSummary(payload: JsonObject | null): string {
  const p = parseAgentStagePayload(payload);
  if (!p) return "";
  return headerLine(timelineHeader([], p));
}

// ---------------------------------------------------------------------------
// Unsupported claims: before → after
// ---------------------------------------------------------------------------

export type ClaimDiffStatus = "resolved" | "still_unsupported" | "new_unsupported";

export interface ClaimDiffRow {
  readonly text: string;
  readonly status: ClaimDiffStatus;
}

const claimKey = (s: string): string => s.trim().replace(/\s+/g, " ").toLowerCase();

/** Claims unsupported before the repair: resolved if absent afterwards; claims only in `after` are new. */
export function claimsDiff(before: readonly Claim[], after: readonly Claim[]): ClaimDiffRow[] {
  const unsupported = (cs: readonly Claim[]): Claim[] => cs.filter((c) => c.status !== "supported");
  const afterKeys = new Set(unsupported(after).map((c) => claimKey(c.text)));
  const beforeKeys = new Set(unsupported(before).map((c) => claimKey(c.text)));
  const rows: ClaimDiffRow[] = unsupported(before).map((c) => ({
    text: c.text,
    status: afterKeys.has(claimKey(c.text)) ? "still_unsupported" : "resolved",
  }));
  for (const c of unsupported(after)) if (!beforeKeys.has(claimKey(c.text))) rows.push({ text: c.text, status: "new_unsupported" });
  return rows.slice(0, MAX_CLAIMS);
}

export const CLAIM_DIFF_LABEL: Readonly<Record<ClaimDiffStatus, string>> = {
  resolved: "resolved by repair",
  still_unsupported: "still unsupported",
  new_unsupported: "newly unsupported",
};

/** Claims reported by one verify_citations result (own data first, then the generic list keys). */
export function verifyResultClaims(row: ToolRow): Claim[] | null {
  const r = row.result?.result;
  if (!r) return null;
  return parseClaims(r.unsupported_claims) ?? parseClaims(r.claims, "unknown");
}

// ---------------------------------------------------------------------------
// calculate_contract_pricing
// ---------------------------------------------------------------------------

export interface PricingView {
  readonly expression: string;
  readonly value: string | null;
  /** "2000 × 245.00 = 490,000.00" */
  readonly line: string;
}

const MAX_EXPR_CHARS = 400;

/** Operators shown as typeset symbols; operands keep the literal text the agent sent. */
export function normalizeExpression(expr: string): string {
  return expr
    .slice(0, MAX_EXPR_CHARS)
    .replace(/\*\*/g, "^")
    .replace(/\s*\*\s*/g, " × ")
    .replace(/\s*\/\s*/g, " ÷ ")
    .replace(/\s*\+\s*/g, " + ")
    .replace(/(\d|\))\s*-\s*/g, "$1 − ")
    .replace(/\s+/g, " ")
    .trim();
}

export function formatPricingValue(v: number): string {
  return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Backend calculator summary "<substituted expression> = <value>" → its left-hand side. */
function expressionFromSummary(summary: string | null): string | null {
  if (!summary) return null;
  const m = /^(.+?)\s*=\s*\$?\s*-?[\d,]+(?:\.\d+)?\s*$/.exec(summary.trim());
  return m && /\d/.test(m[1]) ? m[1] : null;
}

function valueFromSummary(summary: string | null): number | null {
  if (!summary) return null;
  const m = /=\s*\$?\s*(-?[\d,]+(?:\.\d+)?)/.exec(summary);
  if (!m) return null;
  const v = Number(m[1].replace(/,/g, ""));
  return Number.isFinite(v) ? v : null;
}

/** Expression from the call args (or the result's normalized_expression) and value from the result. */
export function pricingView(row: ToolRow): PricingView | null {
  const args = row.call?.toolArgs ?? null;
  const res = row.result?.result ?? null;
  const rawExpr =
    str(res?.normalized_expression) ??
    str(res?.substituted_expression) ??
    expressionFromSummary(row.result?.resultSummary ?? null) ??
    str(args?.expression) ??
    str(args?.expr) ??
    str(args?.formula);
  if (!rawExpr) return null;
  const expression = normalizeExpression(rawExpr);
  const value = num(res?.value) ?? num(res?.result) ?? num(res?.total) ?? valueFromSummary(row.result?.resultSummary ?? null);
  const shown = value === null ? null : formatPricingValue(value);
  return { expression, value: shown, line: shown === null ? expression : `${expression} = ${shown}` };
}

// ---------------------------------------------------------------------------
// Tools: styling + architecture-diagram bindings
// ---------------------------------------------------------------------------

export type ToolTone = "slate" | "clay" | "ochre" | "moss" | "neutral";

export interface ToolStyle {
  readonly label: string;
  readonly tone: ToolTone;
  readonly icon: "search" | "amendments" | "calculator" | "verify" | "tool";
}

const TOOL_STYLES: Readonly<Record<string, ToolStyle>> = {
  search_procurement_corpus: { label: "search corpus", tone: "slate", icon: "search" },
  check_amendments: { label: "check amendments", tone: "clay", icon: "amendments" },
  calculate_contract_pricing: { label: "calculate pricing", tone: "ochre", icon: "calculator" },
  verify_citations: { label: "verify citations", tone: "moss", icon: "verify" },
};

export function toolStyle(name: string): ToolStyle {
  return TOOL_STYLES[name] ?? { label: name, tone: "neutral", icon: "tool" };
}

/** "*" = generic tools node (all tools); otherwise the node's tool names. */
export type ToolBinding = ReadonlySet<string> | "*";

/**
 * Nodes driven by tool calls rather than stage status. With `tool_names` on any node, exactly those nodes;
 * otherwise (descriptor without tool mapping) a node mapped ONLY to `agent_turn` is the generic tools node.
 */
export function toolNodeBindings(nodes: readonly ArchNode[]): Map<string, ToolBinding> {
  const out = new Map<string, ToolBinding>();
  const explicit = nodes.filter((n) => Array.isArray(n.tool_names) && n.tool_names.length > 0);
  if (explicit.length > 0) {
    for (const n of explicit) out.set(n.id, new Set(n.tool_names));
    return out;
  }
  for (const n of nodes) if (n.stage_ids.length === 1 && n.stage_ids[0] === "agent_turn") out.set(n.id, "*");
  return out;
}

/** Agent nodes: mapped to `agent_turn` but not tool-bound. */
export function agentNodeIds(nodes: readonly ArchNode[], bindings: ReadonlyMap<string, ToolBinding>): Set<string> {
  return new Set(nodes.filter((n) => n.stage_ids.includes("agent_turn") && !bindings.has(n.id)).map((n) => n.id));
}

const bound = (binding: ToolBinding, toolName: string): boolean => binding === "*" || binding.has(toolName);

export function rowsForBinding(rows: readonly TimelineRow[], binding: ToolBinding): ToolRow[] {
  return rows.filter((r): r is ToolRow => r.type === "tool" && bound(binding, r.toolName));
}

export interface ToolNodeView {
  readonly status: StageStatus;
  readonly stageId: string | null;
  readonly durationMs: number | null;
  readonly startedAt: number | null;
}

/**
 * Tool node state from its calls: active while one of its calls is open (agent_turn stage active),
 * failed when the stage failed with its call open, done after ≥1 result (duration = Σ backend result ms),
 * idle when never called.
 */
export function deriveToolNodeView(binding: ToolBinding, timeline: EngineTimeline | undefined): ToolNodeView {
  const idle: ToolNodeView = { status: "idle", stageId: null, durationMs: null, startedAt: null };
  if (!timeline) return idle;
  const stage = timeline.stages.agent_turn?.status ?? "idle";
  const rows = rowsForBinding(groupAgentTurns(timeline.agentTurns), binding);
  if (rows.length === 0) return idle;
  const open = rows.filter((r) => r.open);
  if (open.length > 0 && stage === "active") {
    const last = open[open.length - 1];
    return { status: "active", stageId: "agent_turn", durationMs: null, startedAt: parseTs(last.call?.ts) };
  }
  if (open.length > 0 && stage === "failed") return { status: "failed", stageId: "agent_turn", durationMs: null, startedAt: null };
  const durations = rows.map((r) => r.result?.durationMs ?? null).filter((d): d is number => d !== null);
  return {
    status: "done",
    stageId: "agent_turn",
    durationMs: durations.length ? durations.reduce((s, d) => s + d, 0) : null,
    startedAt: null,
  };
}

/** Diagram summary for a tool node: "search ×2 · verify ×2" from its calls. */
export function toolNodeSummary(binding: ToolBinding, timeline: EngineTimeline | undefined): string {
  if (!timeline) return "";
  const counts = new Map<string, number>();
  for (const r of rowsForBinding(groupAgentTurns(timeline.agentTurns), binding)) counts.set(r.toolName, (counts.get(r.toolName) ?? 0) + 1);
  return [...counts.entries()].map(([name, n]) => `${toolStyle(name).label.split(" ")[0]} ×${n}`).join(" · ");
}

/** Edge keys "from->to" between an agent node and a tool node whose call is open (flow while the call is in flight). */
export function toolFlowEdges(
  edges: readonly { readonly from: string; readonly to: string }[],
  nodes: readonly ArchNode[],
  timeline: EngineTimeline | undefined
): Set<string> {
  const out = new Set<string>();
  if (!timeline || timeline.stages.agent_turn?.status !== "active") return out;
  const bindings = toolNodeBindings(nodes);
  if (bindings.size === 0) return out;
  const agents = agentNodeIds(nodes, bindings);
  const open = openToolNames(timeline.agentTurns);
  if (open.size === 0) return out;
  const activeTool = (id: string): boolean => {
    const b = bindings.get(id);
    return b !== undefined && [...open].some((name) => bound(b, name));
  };
  for (const e of edges) {
    if ((agents.has(e.from) && activeTool(e.to)) || (activeTool(e.from) && agents.has(e.to))) out.add(`${e.from}->${e.to}`);
  }
  return out;
}

/** Short one-line preview of tool args ("query=…, solicitation=…"), bounded. */
export function argsPreview(args: JsonObject | null, maxChars = 90, rawText: string | null = null): string {
  if (!args) return rawText ? (rawText.length > maxChars ? `${rawText.slice(0, maxChars - 1)}…` : rawText) : "";
  const parts = Object.entries(args)
    .slice(0, 6)
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v) ?? String(v)}`);
  const s = parts.join(", ");
  return s.length > maxChars ? `${s.slice(0, maxChars - 1)}…` : s;
}
