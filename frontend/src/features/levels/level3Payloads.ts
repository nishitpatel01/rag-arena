/**
 * Pure parsers for the Level 3 (layout-aware multimodal) stage payloads — no React.
 *
 * Published contract (2026-10-22; unknown fields ignored, missing fields hidden):
 * - `query_prep` adds `element_intent: string[]` and `intent_matches: {intent: keyword[]}`
 * - `retrieve` adds `embedding_column`, `element_types {type:n}`, `layout_sources {source:n}`, `amendment_candidates`
 * - `synthesize` adds `authority_headers: [{chunk_id, header, amendment_number, effective_date, supersedes[],
 *   superseded_by[], element_type, layout_source}]` (context order), `element_mix {type:n}`, `context_order`
 * - ingest `parse` payload `{pages, tables, diagrams, figures, vision_pages:[{doc_name, page, title, components,
 *   connections, regions, …, transcription}], vision_failed_pages:[{doc_name, page, reason, status, error_type, error}],
 *   layout_source_counts, docai_ms, vision_ms, processor_version, processor_version_channel, vision_model, truncated?}`
 * Retrieve / rerank per-row element metadata is parsed in `stagePayloads.ts` via `lib/elements.parseElementMeta`.
 */

import type { JsonObject } from "../../types/arena";
import { ELEMENT_TYPES, elementLabel, layoutSourceLabel, parseAmendmentNumber } from "../../lib/elements";
import { formatMs } from "../../lib/format";

/** Safety caps on list sizes taken from one payload. */
export const MAX_INTENTS = 20;
export const MAX_AUTHORITY_HEADERS = 50;
export const MAX_LAYOUT_SOURCES = 20;
export const MAX_VISION_ROWS = 100;
export const MAX_KEYWORDS = 20;

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const count = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);
const ms = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim().length > 0 ? v.trim() : null);

/** `{key: n}` → `[key, n][]` with non-negative integer counts, payload order, bounded; null when not an object. */
export function countEntries(v: unknown, max: number = MAX_LAYOUT_SOURCES): (readonly [string, number])[] | null {
  if (!isObj(v)) return null;
  return Object.entries(v)
    .slice(0, max)
    .flatMap(([k, n]) => {
      const c = count(n);
      return c === null ? [] : [[k, c] as const];
    });
}

function stringList(v: unknown, max: number): string[] | null {
  if (!Array.isArray(v)) return null;
  return v
    .slice(0, max)
    .filter((s): s is string => typeof s === "string" && s.trim().length > 0)
    .map((s) => s.trim());
}

// ---------------------------------------------------------------------------
// query_prep
// ---------------------------------------------------------------------------

/** `element_intent` strings verbatim (e.g. ["diagram", "table"]); null when the key is absent. */
export function parseElementIntent(payload: JsonObject | null): string[] | null {
  if (!payload) return null;
  return stringList(payload.element_intent, MAX_INTENTS);
}

/** `intent_matches` {intent: [keywords that triggered it]}; empty map when absent. */
export function parseIntentMatches(payload: JsonObject | null): ReadonlyMap<string, readonly string[]> {
  const out = new Map<string, readonly string[]>();
  if (!payload || !isObj(payload.intent_matches)) return out;
  for (const [intent, kws] of Object.entries(payload.intent_matches).slice(0, MAX_INTENTS)) {
    const list = stringList(kws, MAX_KEYWORDS);
    if (list) out.set(intent, list);
  }
  return out;
}

// ---------------------------------------------------------------------------
// retrieve (Level 3 summary keys)
// ---------------------------------------------------------------------------

export interface RetrieveL3Summary {
  readonly embeddingColumn: string | null;
  readonly elementTypes: readonly (readonly [string, number])[] | null;
  readonly layoutSources: readonly (readonly [string, number])[] | null;
  readonly amendmentCandidates: number | null;
}

/** Null when none of the Level 3 retrieve summary keys are present (Level 2 payloads). */
export function parseRetrieveL3Summary(payload: JsonObject | null): RetrieveL3Summary | null {
  if (!payload) return null;
  const v: RetrieveL3Summary = {
    embeddingColumn: text(payload.embedding_column),
    elementTypes: countEntries(payload.element_types),
    layoutSources: countEntries(payload.layout_sources),
    amendmentCandidates: count(payload.amendment_candidates),
  };
  return v.embeddingColumn === null && v.elementTypes === null && v.layoutSources === null && v.amendmentCandidates === null ? null : v;
}

/** Lines like "element types: TABLE 8 · DIAGRAM 3 · TEXT 39" (only parts that were reported). */
export function retrieveL3Lines(v: RetrieveL3Summary): string[] {
  const lines: string[] = [];
  if (v.embeddingColumn) lines.push(`embedding column: ${v.embeddingColumn}`);
  if (v.elementTypes) lines.push(`element types: ${sortByElementOrder(v.elementTypes).map(([k, n]) => `${elementLabel(k)} ${n}`).join(" · ") || "none"}`);
  if (v.layoutSources) lines.push(`layout sources: ${v.layoutSources.map(([k, n]) => `${layoutSourceLabel(k)} ${n}`).join(" · ") || "none"}`);
  if (v.amendmentCandidates !== null) lines.push(`amendment candidates: ${v.amendmentCandidates}`);
  return lines;
}

/** Known element types first (TABLE, DIAGRAM, TEXT), then unknown keys in payload order. */
function sortByElementOrder(entries: readonly (readonly [string, number])[]): (readonly [string, number])[] {
  const rank = (k: string): number => {
    const i = (ELEMENT_TYPES as readonly string[]).indexOf(k);
    return i === -1 ? ELEMENT_TYPES.length : i;
  };
  return entries.map((e, i) => ({ e, i })).sort((a, b) => rank(a.e[0]) - rank(b.e[0]) || a.i - b.i).map((x) => x.e);
}

// ---------------------------------------------------------------------------
// synthesize
// ---------------------------------------------------------------------------

export interface AuthorityHeader {
  readonly chunkId: string | null;
  /** Header text exactly as sent in the synthesis prompt. */
  readonly header: string;
  readonly amendmentNumber: number | null;
  readonly effectiveDate: string | null;
  readonly supersedes: readonly string[];
  readonly supersededBy: readonly string[];
  readonly elementType: string | null;
  readonly layoutSource: string | null;
}

/** Element counts in display order (known types first); `[type, n]` pairs verbatim from the payload. */
export type ElementMix = readonly (readonly [string, number])[];

export interface SynthesizeL3View {
  /** Authority headers in context order; null when absent. */
  readonly authorityHeaders: readonly AuthorityHeader[] | null;
  readonly elementMix: ElementMix | null;
  /** Server's description of the context ordering, e.g. "latest amendment first, then rerank order". */
  readonly contextOrder: string | null;
  readonly invalidHeaders: number;
}

function parseAuthorityHeader(item: unknown): AuthorityHeader | null {
  if (typeof item === "string") {
    const header = text(item);
    return header ? { chunkId: null, header, amendmentNumber: null, effectiveDate: null, supersedes: [], supersededBy: [], elementType: null, layoutSource: null } : null;
  }
  if (!isObj(item)) return null;
  const header = text(item.header);
  if (!header) return null;
  return {
    chunkId: text(item.chunk_id),
    header,
    amendmentNumber: parseAmendmentNumber(item.amendment_number),
    effectiveDate: text(item.effective_date),
    supersedes: stringList(item.supersedes, MAX_KEYWORDS) ?? [],
    supersededBy: stringList(item.superseded_by, MAX_KEYWORDS) ?? [],
    elementType: text(item.element_type),
    layoutSource: text(item.layout_source),
  };
}

/** Null when the payload carries neither `authority_headers` nor `element_mix` (Levels 1–2). */
export function parseSynthesizeL3(payload: JsonObject | null): SynthesizeL3View | null {
  if (!payload) return null;
  let authorityHeaders: AuthorityHeader[] | null = null;
  let invalidHeaders = 0;
  if (Array.isArray(payload.authority_headers)) {
    const raw = payload.authority_headers.slice(0, MAX_AUTHORITY_HEADERS);
    invalidHeaders = payload.authority_headers.length - raw.length;
    authorityHeaders = [];
    for (const item of raw) {
      const h = parseAuthorityHeader(item);
      if (h) authorityHeaders.push(h);
      else invalidHeaders += 1;
    }
  }
  const mixEntries = countEntries(payload.element_mix);
  const elementMix = mixEntries && mixEntries.length > 0 ? sortByElementOrder(mixEntries) : null;
  if (authorityHeaders === null && elementMix === null) return null;
  return { authorityHeaders, elementMix, contextOrder: text(payload.context_order), invalidHeaders };
}

export interface MixSegment {
  readonly type: string;
  readonly label: string;
  readonly count: number;
  /** 0–1 share of the reported total (0 when the total is 0). */
  readonly fraction: number;
}

/** Bar segments in display order; only types the server reported. */
export function elementMixSegments(mix: ElementMix): MixSegment[] {
  const total = mix.reduce((s, [, n]) => s + n, 0);
  return mix.map(([type, n]) => ({ type, label: elementLabel(type), count: n, fraction: total > 0 ? n / total : 0 }));
}

/** "DIAGRAM 1 · TEXT 4". */
export function elementMixLine(mix: ElementMix): string {
  return elementMixSegments(mix)
    .map((s) => `${s.label} ${s.count}`)
    .join(" · ");
}

// ---------------------------------------------------------------------------
// Ingest `parse` stage
// ---------------------------------------------------------------------------

export interface VisionPageRow {
  readonly docName: string;
  readonly page: number | null;
  readonly title: string | null;
  readonly components: number | null;
  readonly connections: number | null;
  readonly regions: number | null;
  readonly visionMs: number | null;
}

export interface VisionFailedRow {
  readonly docName: string;
  readonly page: number | null;
  readonly reason: string | null;
  readonly errorType: string | null;
  readonly error: string | null;
}

export interface IngestParseView {
  readonly pages: number | null;
  readonly tables: number | null;
  readonly diagrams: number | null;
  readonly figures: number | null;
  /** Count of transcribed pages (`vision_pages` array length, or a bare number on older payloads). */
  readonly visionPages: number | null;
  readonly visionPageRows: readonly VisionPageRow[];
  readonly visionFailedPages: readonly VisionFailedRow[];
  /** Verbatim `layout_source_counts` entries with integer counts, payload order. */
  readonly layoutSourceCounts: readonly (readonly [string, number])[];
  readonly docaiMs: number | null;
  readonly visionMs: number | null;
  /** Document AI processor version verbatim. */
  readonly processorVersion: string | null;
  /** `processor_version_channel` verbatim, e.g. "release candidate (preview)" / "stable". */
  readonly processorVersionChannel: string | null;
  readonly visionModel: string | null;
  /** The telemetry layer cut the payload to its bounds (`truncated: true`). */
  readonly truncated: boolean;
}

const L3_PARSE_KEYS = ["tables", "diagrams", "vision_pages", "vision_failed_pages", "layout_source_counts", "docai_ms", "vision_ms", "processor_version", "processor_version_channel"] as const;

function parseVisionPage(item: unknown): VisionPageRow | null {
  if (!isObj(item)) return null;
  const docName = text(item.doc_name);
  if (!docName) return null;
  return { docName, page: count(item.page), title: text(item.title), components: count(item.components), connections: count(item.connections), regions: count(item.regions), visionMs: ms(item.vision_ms) };
}

function parseVisionFailure(item: unknown): VisionFailedRow | null {
  if (!isObj(item)) return null;
  const docName = text(item.doc_name);
  if (!docName) return null;
  return { docName, page: count(item.page), reason: text(item.reason), errorType: text(item.error_type), error: text(item.error) };
}

function rows<T>(v: unknown, parse: (x: unknown) => T | null): T[] {
  return Array.isArray(v) ? v.slice(0, MAX_VISION_ROWS).flatMap((x) => (parse(x) ? [parse(x) as T] : [])) : [];
}

/** True when the channel string marks a release-candidate / preview processor version. */
export function isReleaseCandidateChannel(channel: string | null): boolean {
  return channel !== null && /release candidate|preview/i.test(channel);
}

/** Null unless the payload has at least one Level 3 parse key (Levels 1–2 parse payloads keep the generic view). */
export function parseIngestParse(payload: JsonObject | null): IngestParseView | null {
  if (!payload || !L3_PARSE_KEYS.some((k) => k in payload)) return null;
  const visionPageRows = rows(payload.vision_pages, parseVisionPage);
  return {
    pages: count(payload.pages),
    tables: count(payload.tables),
    diagrams: count(payload.diagrams),
    figures: count(payload.figures),
    visionPages: Array.isArray(payload.vision_pages) ? payload.vision_pages.length : count(payload.vision_pages),
    visionPageRows,
    visionFailedPages: rows(payload.vision_failed_pages, parseVisionFailure),
    layoutSourceCounts: countEntries(payload.layout_source_counts) ?? [],
    docaiMs: ms(payload.docai_ms),
    visionMs: ms(payload.vision_ms),
    processorVersion: text(payload.processor_version),
    processorVersionChannel: text(payload.processor_version_channel),
    visionModel: text(payload.vision_model),
    truncated: payload.truncated === true,
  };
}

/** Key/value rows for the parse summary; absent values are omitted (never shown as zero). */
export function ingestParseItems(view: IngestParseView): (readonly [string, string])[] {
  const out: (readonly [string, string])[] = [];
  const add = (k: string, v: number | null, f: (n: number) => string = (n) => n.toLocaleString()): void => {
    if (v !== null) out.push([k, f(v)]);
  };
  add("pages", view.pages);
  add("tables (Document AI)", view.tables);
  add("figures (Document AI)", view.figures);
  add("diagrams (Gemini vision)", view.diagrams);
  add("vision pages transcribed", view.visionPages);
  if (view.visionFailedPages.length > 0) out.push(["vision pages failed", String(view.visionFailedPages.length)]);
  add("Document AI time", view.docaiMs, formatMs);
  add("vision time", view.visionMs, formatMs);
  if (view.processorVersion !== null) out.push(["Document AI processor_version", view.processorVersion]);
  if (view.processorVersionChannel !== null) out.push(["processor_version_channel", view.processorVersionChannel]);
  if (view.visionModel !== null) out.push(["vision model", view.visionModel]);
  for (const [src, n] of view.layoutSourceCounts) out.push([`layout_source · ${layoutSourceLabel(src)}`, n.toLocaleString()]);
  return out;
}

/** One-line diagram node summary for the Level 3 parse stage: "pages=526 · tables=41 · diagrams=9". */
export function ingestParseSummary(view: IngestParseView): string {
  const parts: string[] = [];
  if (view.pages !== null) parts.push(`pages=${view.pages.toLocaleString()}`);
  if (view.tables !== null) parts.push(`tables=${view.tables.toLocaleString()}`);
  if (view.diagrams !== null) parts.push(`diagrams=${view.diagrams.toLocaleString()}`);
  return parts.join(" · ");
}
