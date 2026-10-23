/**
 * Pure parsers for the Level 5 (Managed Vertex AI Search, `managed_vertex`) stage payloads — no React.
 *
 * PROVISIONAL contract (2026-10-23; unknown fields ignored, missing fields shown as "—", never invented):
 * - `retrieve` payload `{request (search body), total_size, attribution_token, api_ms,
 *    results:[{document_id, doc_name, title, uri, extractive_segments:[{content, page_number, relevance_score}]}]}`
 * - `synthesize` payload, two modes:
 *   (a) `{mode:"vertex_ai_search_summary", summary, references:[{document_id, doc_name, page_number?}], model_version,
 *        summary_skipped_reasons[]}` — the managed Vertex AI Search summary (pseudo model `vertex-ai-search-summary`);
 *   (b) `{mode:"model_over_segments", model, prompt_tokens, output_tokens, …}` — a catalog model over the extractive
 *        segments (same shape as Levels 1–3 plus `mode`).
 * - Ingest stages `upload_gcs` / `import_documents` / `verify_counts` (alias-tolerant counts, see `managedIngestItems`).
 */

import type { Citation, JsonObject } from "../../types/arena";
import { formatMs } from "../../lib/format";

export const MAX_RESULTS = 100;
export const MAX_SEGMENTS_PER_RESULT = 20;
export const MAX_REFERENCES = 50;
export const MAX_SKIPPED_REASONS = 20;
/** Characters of an opaque token kept at each end when displayed (the full token stays in the raw payload). */
export const TOKEN_HEAD = 10;
export const TOKEN_TAIL = 6;

export const SUMMARY_MODE = "vertex_ai_search_summary";
export const MODEL_OVER_SEGMENTS_MODE = "model_over_segments";
export type SynthesizeMode = typeof SUMMARY_MODE | typeof MODEL_OVER_SEGMENTS_MODE;

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim().length > 0 ? v : null);
const page = (v: unknown): number | null => {
  const n = num(v);
  return n !== null && Number.isInteger(n) && n > 0 ? n : null;
};

// ---------------------------------------------------------------------------
// retrieve (Discovery Engine SearchService.search with extractive segments)
// ---------------------------------------------------------------------------

export interface ExtractiveSegment {
  readonly content: string;
  readonly pageNumber: number | null;
  readonly relevanceScore: number | null;
}

export interface ManagedResult {
  /** 1-based position in the search response. */
  readonly rank: number;
  readonly documentId: string;
  readonly docName: string | null;
  readonly title: string | null;
  readonly uri: string | null;
  readonly segments: readonly ExtractiveSegment[];
  /** Segments dropped by the per-result bound or for not matching the contract. */
  readonly invalidSegments: number;
}

export interface ManagedRetrieveView {
  readonly request: JsonObject | null;
  readonly totalSize: number | null;
  readonly attributionToken: string | null;
  readonly apiMs: number | null;
  readonly results: readonly ManagedResult[];
  readonly invalidRows: number;
}

function parseSegment(item: unknown): ExtractiveSegment | null {
  if (!isObj(item)) return null;
  const content = typeof item.content === "string" ? item.content : null;
  if (content === null) return null;
  return { content, pageNumber: page(item.page_number), relevanceScore: num(item.relevance_score) };
}

function parseResult(item: unknown, position: number): ManagedResult | null {
  if (!isObj(item)) return null;
  const reported = num(item.rank);
  const rank = reported !== null && Number.isInteger(reported) && reported > 0 ? reported : position;
  const documentId = text(item.document_id) ?? text(item.id);
  if (documentId === null) return null;
  const rawSegs = Array.isArray(item.extractive_segments) ? item.extractive_segments : [];
  const kept = rawSegs.slice(0, MAX_SEGMENTS_PER_RESULT);
  const segments: ExtractiveSegment[] = [];
  let invalidSegments = rawSegs.length - kept.length;
  for (const s of kept) {
    const seg = parseSegment(s);
    if (seg) segments.push(seg);
    else invalidSegments += 1;
  }
  return { rank, documentId, docName: text(item.doc_name), title: text(item.title), uri: text(item.uri), segments, invalidSegments };
}

/**
 * Null unless the payload is the managed search shape: a `results` array plus at least one managed marker
 * (`total_size`, `attribution_token`, or `extractive_segments` on a row). Level 1–3 retrieve payloads use
 * `candidates` and never match.
 */
export function parseManagedRetrieve(payload: JsonObject | null): ManagedRetrieveView | null {
  if (!payload || !Array.isArray(payload.results) || Array.isArray(payload.candidates)) return null;
  const rows = payload.results;
  const marker =
    "total_size" in payload || "attribution_token" in payload || rows.some((r) => isObj(r) && Array.isArray(r.extractive_segments));
  if (!marker) return null;
  const kept = rows.slice(0, MAX_RESULTS);
  let invalidRows = rows.length - kept.length;
  const results: ManagedResult[] = [];
  kept.forEach((item, i) => {
    const r = parseResult(item, i + 1);
    if (r) results.push(r);
    else invalidRows += 1;
  });
  return {
    request: isObj(payload.request) ? payload.request : isObj(payload.api_request) ? payload.api_request : null,
    totalSize: num(payload.total_size),
    attributionToken: text(payload.attribution_token),
    apiMs: num(payload.api_ms),
    results,
    invalidRows,
  };
}

/** Sorted unique segment pages, e.g. "p.3, p.5" ("—" when no segment reports a page). */
export function resultPages(result: ManagedResult): string {
  const pages = [...new Set(result.segments.map((s) => s.pageNumber).filter((p): p is number => p !== null))].sort((a, b) => a - b);
  return pages.length ? pages.map((p) => `p.${p}`).join(", ") : "—";
}

/** Highest segment relevance score of a result (null when no segment reports one). */
export function topRelevance(result: ManagedResult): number | null {
  let best: number | null = null;
  for (const s of result.segments) if (s.relevanceScore !== null && (best === null || s.relevanceScore > best)) best = s.relevanceScore;
  return best;
}

export function segmentCount(view: ManagedRetrieveView): number {
  return view.results.reduce((n, r) => n + r.segments.length, 0);
}

/** Display form of an opaque token: head…tail (N chars). Never shows the middle. */
export function truncateToken(token: string | null, head = TOKEN_HEAD, tail = TOKEN_TAIL): string {
  if (!token) return "—";
  if (token.length <= head + tail + 1) return token;
  return `${token.slice(0, head)}…${token.slice(-tail)} (${token.length} chars)`;
}

/** Diagram node summary: "results=10 · segments=24 · total=37 · 412 ms". */
export function managedRetrieveSummary(view: ManagedRetrieveView): string {
  const parts = [`results=${view.results.length}`, `segments=${segmentCount(view)}`];
  if (view.totalSize !== null) parts.push(`total=${view.totalSize.toLocaleString()}`);
  if (view.apiMs !== null) parts.push(formatMs(view.apiMs));
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// synthesize (managed summary vs catalog model over extractive segments)
// ---------------------------------------------------------------------------

export interface SummaryReference {
  /** 1-based index — the number Vertex AI Search uses for `[n]` markers in the summary text. */
  readonly index: number;
  readonly documentId: string | null;
  readonly docName: string | null;
  readonly pageNumber: number | null;
}

export interface ManagedSynthesizeView {
  /** Null when the payload names no mode and none can be inferred. */
  readonly mode: SynthesizeMode | null;
  /** Raw `mode` value when it is an unknown string (shown verbatim, never mapped). */
  readonly rawMode: string | null;
  readonly summary: string | null;
  readonly references: readonly SummaryReference[];
  readonly modelVersion: string | null;
  /** `model_version_requested` (summary modelSpec.version sent) when reported separately from the used version. */
  readonly modelVersionRequested: string | null;
  readonly model: string | null;
  readonly skippedReasons: readonly string[];
  readonly invalidReferences: number;
}

function inferMode(payload: JsonObject): { mode: SynthesizeMode | null; rawMode: string | null } {
  const raw = text(payload.mode);
  if (raw === SUMMARY_MODE || raw === MODEL_OVER_SEGMENTS_MODE) return { mode: raw, rawMode: null };
  if (raw !== null) return { mode: null, rawMode: raw };
  if (payload.provider === "vertex_ai_search" || Array.isArray(payload.summary_skipped_reasons) || "model_version" in payload || "model_version_used" in payload) {
    return { mode: SUMMARY_MODE, rawMode: null };
  }
  const ctxSource = text(payload.context_source);
  if ((ctxSource !== null && /extractive segment/i.test(ctxSource)) || "context_segments" in payload) return { mode: MODEL_OVER_SEGMENTS_MODE, rawMode: null };
  return { mode: null, rawMode: null };
}

const lastSegment = (v: string | null): string | null => {
  if (v === null) return null;
  const tail = v.split("/").filter(Boolean).pop() ?? "";
  return tail.length > 0 ? tail : null;
};

/** First page from raw API `chunkContents[].pageIdentifier` / `chunk_contents[].page_identifier` (string or number). */
function chunkPage(r: Record<string, unknown>): number | null {
  const list = Array.isArray(r.chunkContents) ? r.chunkContents : Array.isArray(r.chunk_contents) ? r.chunk_contents : [];
  for (const c of list.slice(0, 10)) {
    if (!isObj(c)) continue;
    const raw = c.pageIdentifier ?? c.page_identifier;
    const n = typeof raw === "string" && /^\d+$/.test(raw) ? Number(raw) : page(raw);
    if (n !== null && n > 0) return n;
  }
  return null;
}

/** Null when the payload is not a Level 5 synthesize payload (no `mode` and no managed-summary keys). */
export function parseManagedSynthesize(payload: JsonObject | null): ManagedSynthesizeView | null {
  if (!payload) return null;
  const { mode, rawMode } = inferMode(payload);
  if (mode === null && rawMode === null) return null;
  const rawRefs = Array.isArray(payload.references) ? payload.references : [];
  const keptRefs = rawRefs.slice(0, MAX_REFERENCES);
  let invalidReferences = rawRefs.length - keptRefs.length;
  const references: SummaryReference[] = [];
  keptRefs.forEach((r, i) => {
    if (!isObj(r)) {
      invalidReferences += 1;
      return;
    }
    // Contract shape {document_id, doc_name, page_number} or the raw API shape {document (resource name), uri, title,
    // chunkContents[].pageIdentifier}; the file name comes from the gs:// URI basename when doc_name is absent.
    const documentId = text(r.document_id) ?? lastSegment(text(r.document));
    const docName = text(r.doc_name) ?? lastSegment(text(r.uri)) ?? text(r.title);
    if (documentId === null && docName === null) {
      invalidReferences += 1;
      return;
    }
    references.push({ index: i + 1, documentId, docName, pageNumber: page(r.page_number) ?? chunkPage(r) });
  });
  const reasons = Array.isArray(payload.summary_skipped_reasons) ? payload.summary_skipped_reasons.slice(0, MAX_SKIPPED_REASONS) : [];
  return {
    mode,
    rawMode,
    summary: typeof payload.summary === "string" ? payload.summary : mode === SUMMARY_MODE && typeof payload.answer === "string" ? payload.answer : null,
    references,
    modelVersion: text(payload.model_version) ?? text(payload.model_version_used),
    modelVersionRequested: text(payload.model_version_requested),
    model: text(payload.model),
    skippedReasons: reasons.filter((s): s is string => typeof s === "string" && s.length > 0),
    invalidReferences,
  };
}

/** Badge text: "Vertex AI Search summary" or "<model> over extractive segments" (model label via `labelFor`). */
export function synthesizeModeLabel(view: ManagedSynthesizeView, labelFor: (id: string | null) => string | null = (id) => id): string {
  if (view.mode === SUMMARY_MODE) return "Vertex AI Search summary";
  if (view.mode === MODEL_OVER_SEGMENTS_MODE) return `${labelFor(view.model) ?? view.model ?? "model"} over extractive segments`;
  return `mode: ${view.rawMode ?? "not reported"}`;
}

/** Diagram node summary for the synthesize stage. */
export function managedSynthesizeSummary(view: ManagedSynthesizeView): string {
  if (view.mode === SUMMARY_MODE) {
    const parts = ["managed summary", `refs=${view.references.length}`];
    if (view.skippedReasons.length) parts.push(`skipped: ${view.skippedReasons.length}`);
    return parts.join(" · ");
  }
  if (view.mode === MODEL_OVER_SEGMENTS_MODE) return `${view.model ?? "model"} · over segments`;
  return `mode ${view.rawMode ?? "?"}`;
}

/** Case-insensitive file stem without a trailing `.pdf`. */
export function docStem(name: string): string {
  return name.trim().replace(/\.pdf$/i, "").toLowerCase();
}

/**
 * Index into `citations` for a summary reference: same doc + same page first, then same doc (first page cited),
 * matching on `doc_name` (or `document_id` against `chunk_id`). -1 when the answer does not cite that document.
 */
export function referenceCitationIndex(ref: SummaryReference, citations: readonly Citation[]): number {
  // Reference titles may omit the extension ("03_Technical_Exhibit_Architecture" vs "….pdf"): compare stems.
  const refStem = ref.docName === null ? null : docStem(ref.docName);
  const sameDoc = (c: Citation): boolean =>
    (refStem !== null && docStem(c.doc_name) === refStem) || (ref.documentId !== null && (c.chunk_id === ref.documentId || c.chunk_id.startsWith(`${ref.documentId}`)));
  if (ref.pageNumber !== null) {
    const exact = citations.findIndex((c) => sameDoc(c) && c.page_number === ref.pageNumber);
    if (exact >= 0) return exact;
  }
  return citations.findIndex(sameDoc);
}

// ---------------------------------------------------------------------------
// Ingest (managed import)
// ---------------------------------------------------------------------------

/** Ingest stages of the managed job in display order (skipped canonical stages follow; see `ingestStageOrder`). */
export const MANAGED_INGEST_STAGES = ["discover_docs", "upload_gcs", "import_documents", "verify_counts"] as const;
/** Canonical BigQuery-pipeline stages the managed job reports as `stage_skipped`. */
export const MANAGED_SKIPPED_STAGES = ["parse", "chunk", "embed", "load_bigquery", "build_indexes"] as const;

const pick = (payload: JsonObject, keys: readonly string[]): number | null => {
  for (const k of keys) {
    const v = num(payload[k]);
    if (v !== null) return v;
  }
  return null;
};
const pickText = (payload: JsonObject, keys: readonly string[]): string | null => {
  for (const k of keys) {
    const v = text(payload[k]);
    if (v !== null) return v;
  }
  return null;
};

const ALIASES: Readonly<Record<string, readonly (readonly [string, readonly string[]])[]>> = {
  discover_docs: [["documents found", ["docs", "documents", "files"]]],
  upload_gcs: [
    ["uploaded", ["uploaded", "uploaded_count", "files_uploaded"]],
    ["already in GCS", ["skipped_existing", "unchanged", "skipped"]],
    ["total files", ["total", "files", "docs"]],
    ["bytes", ["bytes", "total_bytes", "bytes_uploaded"]],
  ],
  import_documents: [
    ["imported (success)", ["success_count", "imported", "success"]],
    ["failed", ["failure_count", "failed", "failures"]],
    ["total", ["total_count", "total"]],
    ["operation time", ["operation_ms", "import_ms", "poll_ms"]],
    ["polls", ["polls"]],
  ],
  verify_counts: [
    ["documents in data store", ["document_count", "documents", "count"]],
    ["expected", ["expected", "expected_count", "local_docs"]],
    ["indexed", ["indexed"]],
    ["outside corpus", ["outside_corpus"]],
  ],
};

const TEXT_ALIASES: Readonly<Record<string, readonly (readonly [string, readonly string[]])[]>> = {
  import_documents: [["operation", ["operation", "operation_name", "operation_id"]], ["reconciliation", ["reconciliation_mode"]]],
  verify_counts: [["data store state", ["data_store_state", "state"]]],
};

/** Key/value rows for a managed ingest stage payload (only fields actually present); empty for other stages. */
export function managedIngestItems(stageId: string, payload: JsonObject | null): (readonly [string, string])[] {
  if (!payload) return [];
  const out: (readonly [string, string])[] = [];
  for (const [label, keys] of ALIASES[stageId] ?? []) {
    const v = pick(payload, keys);
    if (v !== null) out.push([label, label === "operation time" ? formatMs(v) : v.toLocaleString()]);
  }
  for (const [label, keys] of TEXT_ALIASES[stageId] ?? []) {
    const v = pickText(payload, keys);
    if (v !== null) out.push([label, v]);
  }
  return out;
}

/** Import error samples verbatim (`error_samples` / `errors`: strings or `{message}` objects), bounded. */
export function importErrorSamples(payload: JsonObject | null, max = 10): string[] {
  if (!payload) return [];
  const raw = Array.isArray(payload.error_samples) ? payload.error_samples : Array.isArray(payload.errors) ? payload.errors : [];
  return raw
    .slice(0, max)
    .map((e) => (typeof e === "string" ? e : isObj(e) ? text(e.message) ?? JSON.stringify(e) : null))
    .filter((s): s is string => s !== null && s.length > 0);
}

/** Node summary for managed ingest stages ("uploaded=45", "success=45 · failed=0", "documents=45"); null otherwise. */
export function managedIngestSummary(stageId: string, payload: JsonObject | null): string | null {
  if (!payload) return null;
  if (stageId === "upload_gcs") {
    const up = pick(payload, ["uploaded", "uploaded_count", "files_uploaded"]);
    const skip = pick(payload, ["skipped_existing", "unchanged", "skipped"]);
    const parts = [up !== null ? `uploaded=${up}` : null, skip !== null ? `unchanged=${skip}` : null].filter(Boolean);
    return parts.length ? parts.join(" · ") : null;
  }
  if (stageId === "import_documents") {
    const ok = pick(payload, ["success_count", "imported", "success"]);
    const bad = pick(payload, ["failure_count", "failed", "failures"]);
    const parts = [ok !== null ? `success=${ok}` : null, bad !== null ? `failed=${bad}` : null].filter(Boolean);
    return parts.length ? parts.join(" · ") : null;
  }
  if (stageId === "verify_counts") {
    const docs = pick(payload, ["document_count", "documents"]);
    return docs !== null ? `documents=${docs}` : null;
  }
  return null;
}

/**
 * Rail order for an ingestion job: the level's declared stages first (managed: discover → upload → import →
 * verify, then the skipped canonical stages), then anything else the stream reported.
 */
export function ingestStageOrder(levelId: string, canonical: readonly string[], seen: readonly string[]): string[] {
  const declared = levelId === "managed_vertex" ? [...MANAGED_INGEST_STAGES, ...MANAGED_SKIPPED_STAGES] : [...canonical];
  for (const s of seen) if (!declared.includes(s)) declared.push(s);
  return declared;
}
