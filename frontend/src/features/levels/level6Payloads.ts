/**
 * Pure parsers for Level 6 (Managed Vertex AI RAG Engine, `rag_engine`) stage payloads — no React.
 *
 * Contract (`backend/app/engines/rag_engine.py`):
 * - `retrieve` payload `{api_request, corpus, location, embedding_model, rows_returned, api_ms, attempts,
 *    retrieval_api_fee_usd, solicitation_filter,
 *    contexts:[{rank, chunk_id, doc_name, solicitation_id, uri, page_number, last_page, page_source, score, text}]}`
 * - `synthesize` payload, two modes:
 *   (a) `{model:"vertex-rag-engine-grounded", underlying_gemini_model, provider:"vertex_rag_engine", corpus,
 *        thinking_level, prompt_tokens, output_tokens, thinking_tokens, grounding_chunks_count}` — Gemini grounded
 *       natively via `VertexRagStore`;
 *   (b) `{model, provider, context_source:"Vertex AI RAG Engine RetrieveContexts", context_chunks, corpus,
 *        prompt_tokens, output_tokens, ...}` — a catalog LLM synthesizing over `:retrieveContexts` chunks.
 *
 * Parsers never invent values: missing scalars become `null`, malformed rows are dropped and counted in `invalidRows`.
 */

import type { JsonObject } from "../../types/arena";
import { formatMs } from "../../lib/format";

export const MAX_RAG_CONTEXTS = 50;
export const GROUNDED_MODE = "vertex_rag_store_grounded";
export const MODEL_OVER_CONTEXTS_MODE = "model_over_rag_contexts";
export type RagSynthesizeMode = typeof GROUNDED_MODE | typeof MODEL_OVER_CONTEXTS_MODE;

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const nonEmpty = (v: unknown): string | null =>
  typeof v === "string" && v.trim().length > 0 ? v.trim() : null;
const posInt = (v: unknown): number | null => {
  const n = num(v);
  return n !== null && Number.isInteger(n) && n > 0 ? n : null;
};

// ---------------------------------------------------------------------------
// retrieve (Vertex AI RAG Engine `:retrieveContexts`)
// ---------------------------------------------------------------------------

export interface RagContextRow {
  readonly rank: number;
  readonly chunkId: string;
  readonly docName: string | null;
  readonly solicitationId: string | null;
  readonly uri: string | null;
  readonly pageNumber: number | null;
  readonly lastPage: number | null;
  readonly pageSource: string | null;
  readonly score: number | null;
  readonly text: string;
}

export interface RagEngineRetrieveView {
  readonly apiRequest: JsonObject | null;
  readonly corpus: string | null;
  readonly location: string | null;
  readonly embeddingModel: string | null;
  readonly rowsReturned: number | null;
  readonly apiMs: number | null;
  readonly attempts: number | null;
  readonly retrievalApiFeeUsd: number | null;
  readonly queryEmbeddingTokens: string | null;
  readonly solicitationFilter: string | null;
  readonly contexts: readonly RagContextRow[];
  readonly invalidRows: number;
}

function parseContextRow(item: unknown, position: number): RagContextRow | null {
  if (!isObj(item)) return null;
  const chunkId = nonEmpty(item.chunk_id);
  const text = nonEmpty(item.text);
  if (chunkId === null || text === null) return null;
  const pageNumber = posInt(item.page_number);
  const rawLast = posInt(item.last_page);
  const lastPage =
    pageNumber !== null && rawLast !== null && rawLast >= pageNumber ? rawLast : pageNumber;
  return {
    rank: posInt(item.rank) ?? position,
    chunkId,
    docName: nonEmpty(item.doc_name),
    solicitationId: nonEmpty(item.solicitation_id),
    uri: nonEmpty(item.uri),
    pageNumber,
    lastPage,
    pageSource: nonEmpty(item.page_source),
    score: num(item.score),
    text,
  };
}

/**
 * Parses a Level 6 `:retrieveContexts` payload; returns `null` for L1–L3 (`candidates`) and L5 (`results`).
 */
export function parseRagEngineRetrieve(payload: JsonObject | null): RagEngineRetrieveView | null {
  if (
    !payload ||
    !Array.isArray(payload.contexts) ||
    Array.isArray(payload.candidates) ||
    Array.isArray(payload.results)
  ) {
    return null;
  }
  const raw = payload.contexts;
  const kept = raw.slice(0, MAX_RAG_CONTEXTS);
  let invalidRows = raw.length - kept.length;
  const contexts: RagContextRow[] = [];
  kept.forEach((item, i) => {
    const row = parseContextRow(item, i + 1);
    if (row) contexts.push(row);
    else invalidRows += 1;
  });
  return {
    apiRequest: isObj(payload.api_request)
      ? payload.api_request
      : isObj(payload.request)
        ? payload.request
        : null,
    corpus: nonEmpty(payload.corpus),
    location: nonEmpty(payload.location),
    embeddingModel: nonEmpty(payload.embedding_model),
    rowsReturned: num(payload.rows_returned),
    apiMs: num(payload.api_ms),
    attempts: posInt(payload.attempts),
    retrievalApiFeeUsd: num(payload.retrieval_api_fee_usd),
    queryEmbeddingTokens: nonEmpty(payload.query_embedding_tokens),
    solicitationFilter: nonEmpty(payload.solicitation_filter),
    contexts,
    invalidRows,
  };
}

/** Formats a chunk page span as `"p.2–3"`, `"p.1"`, or `"—"`. */
export function contextPageLabel(
  row: Pick<RagContextRow, "pageNumber" | "lastPage">
): string {
  if (row.pageNumber === null) return "—";
  if (row.lastPage !== null && row.lastPage > row.pageNumber) {
    return `p.${row.pageNumber}\u2013${row.lastPage}`;
  }
  return `p.${row.pageNumber}`;
}

/** Diagram node summary: `"contexts=10 · us-central1 · 184 ms"`. */
export function ragEngineRetrieveSummary(view: RagEngineRetrieveView): string {
  const parts: string[] = [`contexts=${view.contexts.length}`];
  if (view.location !== null) parts.push(view.location);
  if (view.apiMs !== null) parts.push(formatMs(view.apiMs));
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// synthesize (`VertexRagStore` grounded vs catalog LLM over `RagCorpus` contexts)
// ---------------------------------------------------------------------------

export interface RagEngineSynthesizeView {
  readonly mode: RagSynthesizeMode;
  readonly model: string | null;
  readonly underlyingGeminiModel: string | null;
  readonly provider: string | null;
  readonly corpus: string | null;
  readonly contextSource: string | null;
  readonly contextChunks: number | null;
  readonly thinkingLevel: string | null;
  readonly promptTokens: number | null;
  readonly outputTokens: number | null;
  readonly thinkingTokens: number | null;
  readonly groundingChunksCount: number | null;
  readonly stopReason: string | null;
  readonly answer: string | null;
}

function detectRagSynthesizeMode(payload: JsonObject): RagSynthesizeMode | null {
  if (
    payload.provider === "vertex_rag_engine" ||
    payload.model === "vertex-rag-engine-grounded" ||
    "grounding_chunks_count" in payload ||
    "underlying_gemini_model" in payload
  ) {
    return GROUNDED_MODE;
  }
  const ctxSource = nonEmpty(payload.context_source);
  if (
    (ctxSource !== null && /rag engine/i.test(ctxSource)) ||
    ("context_chunks" in payload && nonEmpty(payload.corpus) !== null)
  ) {
    return MODEL_OVER_CONTEXTS_MODE;
  }
  return null;
}

/** Parses a Level 6 `synthesize` payload; returns `null` for L1–L5 synthesize shapes. */
export function parseRagEngineSynthesize(
  payload: JsonObject | null
): RagEngineSynthesizeView | null {
  if (!payload || nonEmpty(payload.mode) !== null) return null;
  const mode = detectRagSynthesizeMode(payload);
  if (mode === null) return null;
  return {
    mode,
    model: nonEmpty(payload.model),
    underlyingGeminiModel: nonEmpty(payload.underlying_gemini_model),
    provider: nonEmpty(payload.provider),
    corpus: nonEmpty(payload.corpus),
    contextSource: nonEmpty(payload.context_source),
    contextChunks: num(payload.context_chunks),
    thinkingLevel: nonEmpty(payload.thinking_level),
    promptTokens: num(payload.prompt_tokens),
    outputTokens: num(payload.output_tokens),
    thinkingTokens: num(payload.thinking_tokens),
    groundingChunksCount: num(payload.grounding_chunks_count),
    stopReason: nonEmpty(payload.stop_reason),
    answer: typeof payload.answer === "string" ? payload.answer : null,
  };
}

/** Returns badge text for the Level 6 synthesis path. */
export function ragSynthesizeModeLabel(
  view: RagEngineSynthesizeView,
  labelFor: (id: string | null) => string | null = (id) => id
): string {
  if (view.mode === GROUNDED_MODE) {
    const base = view.underlyingGeminiModel
      ? labelFor(view.underlyingGeminiModel) ?? view.underlyingGeminiModel
      : null;
    return base ? `VertexRagStore grounded (${base})` : "VertexRagStore grounded";
  }
  const modelName = labelFor(view.model) ?? view.model ?? "model";
  return `${modelName} over RagCorpus contexts`;
}

/** Returns one-line stage/node summary for Level 6 synthesis. */
export function ragEngineSynthesizeSummary(view: RagEngineSynthesizeView): string {
  if (view.mode === GROUNDED_MODE) {
    return view.groundingChunksCount !== null
      ? `VertexRagStore · grounding_chunks=${view.groundingChunksCount}`
      : "VertexRagStore grounded";
  }
  const parts = [view.model ?? "model"];
  parts.push(
    view.contextChunks !== null ? `contexts=${view.contextChunks}` : "over RagCorpus"
  );
  return parts.join(" · ");
}
