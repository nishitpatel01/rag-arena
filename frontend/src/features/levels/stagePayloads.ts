/**
 * Pure parsers + derivations for stage payloads that get a dedicated renderer (no React).
 *
 * Contract (Level 2, spec §3 + Phase 2 brief):
 * - `query_prep` payload `{original_query, prepared_query, expansions:[{term, expansion}]}`
 * - `retrieve` payload `{rows_returned, candidate_k, bytes_processed, slot_millis, cache_hit, job_id, rrf_k,
 *    candidates:[{chunk_id, doc_name, page_number, section_id, vector_rank|null, lexical_rank|null,
 *    vector_distance|null, rrf_score}]}` (Level 1 has no `candidates` → generic view)
 * - `rerank` payload `{service_used, model, candidates_in, candidates_out,
 *    results:[{chunk_id, doc_name, page_number, rrf_rank, rrf_score, rerank_rank, rerank_score}], rank_api_ms}`
 *
 * Parsers never invent values: missing scalars become `null`, malformed rows are dropped and counted in
 * `invalidRows` so the UI can say so (fail loud, never silently).
 */

import type { JsonObject } from "../../types/arena";
import { formatBytes, summarizePayload } from "../../lib/format";

/** Bound on rows parsed from one payload (backend sends ≤ 50 candidates; this is a safety cap). */
export const MAX_PAYLOAD_ROWS = 500;
/** Bound on tokens for the word diff's LCS table (n × m cells); larger inputs use prefix/suffix diff. */
export const MAX_DIFF_CELLS = 250_000;

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
/** Ranks are positive integers; anything else is treated as "absent" (null). */
const rank = (v: unknown): number | null => {
  const n = num(v);
  return n !== null && Number.isInteger(n) && n > 0 ? n : null;
};

// ---------------------------------------------------------------------------
// query_prep
// ---------------------------------------------------------------------------

export interface Expansion {
  readonly term: string;
  readonly expansion: string;
}

export interface QueryPrepView {
  readonly originalQuery: string;
  readonly preparedQuery: string;
  readonly expansions: readonly Expansion[];
  readonly invalidRows: number;
}

export function parseQueryPrep(payload: JsonObject | null): QueryPrepView | null {
  if (!payload) return null;
  const originalQuery = str(payload.original_query);
  const preparedQuery = str(payload.prepared_query);
  if (originalQuery === null || preparedQuery === null) return null;
  const raw = Array.isArray(payload.expansions) ? payload.expansions.slice(0, MAX_PAYLOAD_ROWS) : [];
  const expansions: Expansion[] = [];
  let invalidRows = Array.isArray(payload.expansions) ? Math.max(0, payload.expansions.length - raw.length) : 0;
  for (const item of raw) {
    const term = isObj(item) ? str(item.term) : null;
    const expansion = isObj(item) ? str(item.expansion) : null;
    if (term === null || expansion === null) invalidRows += 1;
    else expansions.push({ term, expansion });
  }
  return { originalQuery, preparedQuery, expansions, invalidRows };
}

export type DiffKind = "same" | "added" | "removed";

export interface DiffOp {
  readonly kind: DiffKind;
  readonly text: string;
}

/**
 * Whitespace runs, alphanumeric runs, and single punctuation characters, so "CLIN-0004-AB" diffs
 * against "CLIN (Contract Line Item Number)-0004-AB" as a pure insertion. Joining the tokens
 * reproduces the input exactly.
 */
export function tokenize(text: string): string[] {
  return text.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
}

function pushOp(ops: DiffOp[], kind: DiffKind, text: string): void {
  if (text.length === 0) return;
  const last = ops[ops.length - 1];
  if (last && last.kind === kind) ops[ops.length - 1] = { kind, text: last.text + text };
  else ops.push({ kind, text });
}

/** Prefix/suffix diff used when the LCS table would exceed MAX_DIFF_CELLS. */
function coarseDiff(a: readonly string[], b: readonly string[]): DiffOp[] {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const ops: DiffOp[] = [];
  pushOp(ops, "same", a.slice(0, start).join(""));
  pushOp(ops, "removed", a.slice(start, endA).join(""));
  pushOp(ops, "added", b.slice(start, endB).join(""));
  pushOp(ops, "same", a.slice(endA).join(""));
  return ops;
}

/**
 * Word-level diff original → prepared (LCS over word/whitespace tokens). Joining the `same`+`added`
 * ops reproduces `prepared`; joining `same`+`removed` reproduces `original`.
 */
export function diffWords(original: string, prepared: string): DiffOp[] {
  const a = tokenize(original);
  const b = tokenize(prepared);
  if (a.length * b.length > MAX_DIFF_CELLS) return coarseDiff(a, b);
  const n = a.length;
  const m = b.length;
  // lcs[i][j] = LCS length of a[i:] and b[j:]
  const lcs: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      pushOp(ops, "same", a[i]);
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      pushOp(ops, "removed", a[i]);
      i += 1;
    } else {
      pushOp(ops, "added", b[j]);
      j += 1;
    }
  }
  pushOp(ops, "removed", a.slice(i).join(""));
  pushOp(ops, "added", b.slice(j).join(""));
  return ops;
}

// ---------------------------------------------------------------------------
// retrieve (hybrid: VECTOR_SEARCH + SEARCH() → RRF)
// ---------------------------------------------------------------------------

export type CandidateCategory = "both" | "vector" | "lexical" | "unranked";

export const CATEGORY_LABEL: Readonly<Record<CandidateCategory, string>> = {
  both: "vector+lexical",
  vector: "vector only",
  lexical: "lexical only",
  unranked: "no rank reported",
};

export interface RetrieveCandidate {
  readonly chunkId: string;
  readonly docName: string | null;
  readonly pageNumber: number | null;
  readonly sectionId: string | null;
  readonly vectorRank: number | null;
  readonly lexicalRank: number | null;
  readonly vectorDistance: number | null;
  readonly rrfScore: number;
}

export interface HybridRetrieveView {
  readonly rowsReturned: number | null;
  readonly candidateK: number | null;
  readonly bytesProcessed: number | null;
  readonly slotMillis: number | null;
  readonly cacheHit: boolean | null;
  readonly jobId: string | null;
  readonly rrfK: number | null;
  /** Sorted by `rrf_score` descending (RRF rank = index + 1); ties keep payload order. */
  readonly candidates: readonly RetrieveCandidate[];
  readonly invalidRows: number;
}

function parseCandidate(item: unknown): RetrieveCandidate | null {
  if (!isObj(item)) return null;
  const chunkId = str(item.chunk_id);
  const rrfScore = num(item.rrf_score);
  if (chunkId === null || rrfScore === null) return null;
  return {
    chunkId,
    docName: str(item.doc_name),
    pageNumber: num(item.page_number),
    sectionId: str(item.section_id),
    vectorRank: rank(item.vector_rank),
    lexicalRank: rank(item.lexical_rank),
    vectorDistance: num(item.vector_distance),
    rrfScore,
  };
}

/** Null when the payload is not the hybrid shape (no `candidates` array) — e.g. Level 1 retrieve. */
export function parseHybridRetrieve(payload: JsonObject | null): HybridRetrieveView | null {
  if (!payload || !Array.isArray(payload.candidates)) return null;
  const raw = payload.candidates.slice(0, MAX_PAYLOAD_ROWS);
  let invalidRows = payload.candidates.length - raw.length;
  const parsed: { c: RetrieveCandidate; i: number }[] = [];
  raw.forEach((item, i) => {
    const c = parseCandidate(item);
    if (c) parsed.push({ c, i });
    else invalidRows += 1;
  });
  parsed.sort((x, y) => y.c.rrfScore - x.c.rrfScore || x.i - y.i);
  return {
    rowsReturned: num(payload.rows_returned),
    candidateK: num(payload.candidate_k),
    bytesProcessed: num(payload.bytes_processed),
    slotMillis: num(payload.slot_millis),
    cacheHit: bool(payload.cache_hit),
    jobId: str(payload.job_id),
    rrfK: num(payload.rrf_k),
    candidates: parsed.map((p) => p.c),
    invalidRows,
  };
}

export function categorizeCandidate(c: Pick<RetrieveCandidate, "vectorRank" | "lexicalRank">): CandidateCategory {
  if (c.vectorRank !== null && c.lexicalRank !== null) return "both";
  if (c.vectorRank !== null) return "vector";
  if (c.lexicalRank !== null) return "lexical";
  return "unranked";
}

export type CategoryCounts = Readonly<Record<CandidateCategory, number>>;

export function countCategories(candidates: readonly RetrieveCandidate[]): CategoryCounts {
  const out: Record<CandidateCategory, number> = { both: 0, vector: 0, lexical: 0, unranked: 0 };
  for (const c of candidates) out[categorizeCandidate(c)] += 1;
  return out;
}

// ---------------------------------------------------------------------------
// rerank (Discovery Engine Ranking API)
// ---------------------------------------------------------------------------

export interface RerankResult {
  readonly chunkId: string;
  readonly docName: string | null;
  readonly pageNumber: number | null;
  readonly rrfRank: number;
  readonly rrfScore: number | null;
  readonly rerankRank: number;
  readonly rerankScore: number;
}

export interface RerankView {
  readonly serviceUsed: string | null;
  readonly model: string | null;
  readonly candidatesIn: number | null;
  readonly candidatesOut: number | null;
  readonly rankApiMs: number | null;
  /** Sorted by `rerank_rank` ascending. */
  readonly results: readonly RerankResult[];
  readonly invalidRows: number;
}

function parseRerankResult(item: unknown): RerankResult | null {
  if (!isObj(item)) return null;
  const chunkId = str(item.chunk_id);
  const rrfRank = rank(item.rrf_rank);
  const rerankRank = rank(item.rerank_rank);
  const rerankScore = num(item.rerank_score);
  if (chunkId === null || rrfRank === null || rerankRank === null || rerankScore === null) return null;
  return { chunkId, docName: str(item.doc_name), pageNumber: num(item.page_number), rrfRank, rrfScore: num(item.rrf_score), rerankRank, rerankScore };
}

/** Null when the payload has no `results` array (e.g. the stage failed or has not completed). */
export function parseRerank(payload: JsonObject | null): RerankView | null {
  if (!payload || !Array.isArray(payload.results)) return null;
  const raw = payload.results.slice(0, MAX_PAYLOAD_ROWS);
  let invalidRows = payload.results.length - raw.length;
  const results: RerankResult[] = [];
  for (const item of raw) {
    const r = parseRerankResult(item);
    if (r) results.push(r);
    else invalidRows += 1;
  }
  results.sort((x, y) => x.rerankRank - y.rerankRank);
  return {
    serviceUsed: str(payload.service_used),
    model: str(payload.model),
    candidatesIn: num(payload.candidates_in),
    candidatesOut: num(payload.candidates_out),
    rankApiMs: num(payload.rank_api_ms),
    results,
    invalidRows,
  };
}

export interface RankMovement {
  /** Positive = moved up (towards rank 1). */
  readonly delta: number;
  readonly direction: "up" | "down" | "same";
  /** "+3", "−2" (U+2212 minus) or "0". */
  readonly text: string;
  /** Screen-reader phrase, e.g. "moved up 3 places". */
  readonly srText: string;
}

export function rankMovement(rrfRank: number, rerankRank: number): RankMovement {
  const delta = rrfRank - rerankRank;
  if (delta > 0) return { delta, direction: "up", text: `+${delta}`, srText: `moved up ${delta} place${delta === 1 ? "" : "s"}` };
  if (delta < 0) {
    const d = -delta;
    return { delta, direction: "down", text: `\u2212${d}`, srText: `moved down ${d} place${d === 1 ? "" : "s"}` };
  }
  return { delta: 0, direction: "same", text: "0", srText: "rank unchanged" };
}

/** True when the reranker lifted a candidate from outside the top `cutoff` (by RRF) into it. */
export function movedIntoTop(rrfRank: number, rerankRank: number, cutoff: number): boolean {
  return cutoff > 0 && rrfRank > cutoff && rerankRank <= cutoff;
}

/** The "top-N" cutoff for the marker: `candidates_out` when reported, else the number of results. */
export function topCutoff(view: RerankView): number {
  return view.candidatesOut !== null && view.candidatesOut > 0 ? view.candidatesOut : view.results.length;
}

/** Bar width 0–1 for a rerank score; scores above 1 are normalised by the max score. */
export function scoreBarFraction(score: number, maxScore: number): number {
  if (!Number.isFinite(score) || score <= 0) return 0;
  const denom = Number.isFinite(maxScore) && maxScore > 1 ? maxScore : 1;
  return Math.min(1, score / denom);
}

export function maxRerankScore(view: RerankView): number | null {
  if (view.results.length === 0) return null;
  return view.results.reduce((m, r) => Math.max(m, r.rerankScore), Number.NEGATIVE_INFINITY);
}

const dash = (v: number | string | null): string => (v === null ? "—" : String(v));

/** "50 → 5 via Discovery Engine Ranking API (semantic-ranker-default@latest) · 412 ms" (absent parts as —). */
export function rerankSummaryLine(view: RerankView): string {
  const ms = view.rankApiMs === null ? "—" : `${Math.round(view.rankApiMs).toLocaleString()} ms`;
  return `${dash(view.candidatesIn)} → ${dash(view.candidatesOut)} via ${dash(view.serviceUsed)} (${dash(view.model)}) · ${ms}`;
}

// ---------------------------------------------------------------------------
// Diagram node summaries
// ---------------------------------------------------------------------------

/** "rows=50 · both=12 · 12.4 MB scanned" (+ " · cache hit" when BigQuery served it from cache). */
export function retrieveNodeSummary(view: HybridRetrieveView): string {
  const parts: string[] = [];
  const rows = view.rowsReturned ?? view.candidates.length;
  parts.push(`rows=${rows.toLocaleString()}`);
  parts.push(`both=${countCategories(view.candidates).both}`);
  if (view.bytesProcessed !== null) parts.push(`${formatBytes(view.bytesProcessed)} scanned`);
  if (view.cacheHit === true) parts.push("cache hit");
  return parts.join(" · ");
}

/** "50→5 · top score 0.95". */
export function rerankNodeSummary(view: RerankView): string {
  const top = maxRerankScore(view);
  const parts = [`${dash(view.candidatesIn)}→${dash(view.candidatesOut ?? view.results.length)}`];
  if (top !== null) parts.push(`top score ${top.toFixed(2)}`);
  return parts.join(" · ");
}

/** One-line summary for a completed stage: dedicated summaries for hybrid retrieve / rerank, generic otherwise. */
export function summarizeStage(stageId: string | null, payload: JsonObject | null): string {
  if (stageId === "retrieve") {
    const v = parseHybridRetrieve(payload);
    if (v) return retrieveNodeSummary(v);
  } else if (stageId === "rerank") {
    const v = parseRerank(payload);
    if (v) return rerankNodeSummary(v);
  } else if (stageId === "query_prep") {
    const v = parseQueryPrep(payload);
    if (v && v.expansions.length > 0) return `expansions=${v.expansions.length}`;
  }
  return summarizePayload(payload);
}
