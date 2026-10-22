/**
 * Pure parsers for the Level 3 (layout-aware multimodal) stage payloads — no React.
 *
 * Provisional contract (code defensively: unknown fields ignored, missing fields hidden):
 * - `query_prep` adds `element_intent: string[]`
 * - `synthesize` adds `authority_headers: string[]` and `element_mix: {table, diagram, narrative_text}`
 * - ingest `parse` stage payload `{pages, tables, diagrams, vision_pages, layout_source_counts, docai_ms, vision_ms,
 *   processor_version, <release-candidate flag>}` (flag key not yet published: `release_candidate`,
 *   `is_release_candidate` or `processor_release_candidate` accepted)
 * Retrieve / rerank per-row element metadata is parsed in `stagePayloads.ts` via `lib/elements.parseElementMeta`.
 */

import type { JsonObject } from "../../types/arena";
import { ELEMENT_LABEL, ELEMENT_TYPES, layoutSourceLabel, type ElementType } from "../../lib/elements";
import { formatMs } from "../../lib/format";

/** Safety caps on list sizes taken from one payload. */
export const MAX_INTENTS = 20;
export const MAX_AUTHORITY_HEADERS = 50;
export const MAX_LAYOUT_SOURCES = 20;

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const count = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);
const ms = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

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

// ---------------------------------------------------------------------------
// synthesize
// ---------------------------------------------------------------------------

export type ElementMix = Readonly<Record<ElementType, number | null>>;

export interface SynthesizeL3View {
  /** Authority headers in the order the server sent them; null when absent. */
  readonly authorityHeaders: readonly string[] | null;
  readonly elementMix: ElementMix | null;
}

function parseElementMix(v: unknown): ElementMix | null {
  if (!isObj(v)) return null;
  const mix = { table: count(v.table), diagram: count(v.diagram), narrative_text: count(v.narrative_text) };
  return mix.table === null && mix.diagram === null && mix.narrative_text === null ? null : mix;
}

/** Null when the payload carries neither `authority_headers` nor `element_mix` (Levels 1–2). */
export function parseSynthesizeL3(payload: JsonObject | null): SynthesizeL3View | null {
  if (!payload) return null;
  const authorityHeaders = stringList(payload.authority_headers, MAX_AUTHORITY_HEADERS);
  const elementMix = parseElementMix(payload.element_mix);
  if (authorityHeaders === null && elementMix === null) return null;
  return { authorityHeaders, elementMix };
}

export interface MixSegment {
  readonly type: ElementType;
  readonly label: string;
  readonly count: number;
  /** 0–1 share of the reported total (0 when the total is 0). */
  readonly fraction: number;
}

/** Bar segments in display order; types the server did not report are omitted. */
export function elementMixSegments(mix: ElementMix): MixSegment[] {
  const present = ELEMENT_TYPES.filter((t) => mix[t] !== null);
  const total = present.reduce((s, t) => s + (mix[t] ?? 0), 0);
  return present.map((t) => ({ type: t, label: ELEMENT_LABEL[t], count: mix[t] ?? 0, fraction: total > 0 ? (mix[t] ?? 0) / total : 0 }));
}

/** "TABLE 2 · DIAGRAM 1 · TEXT 2". */
export function elementMixLine(mix: ElementMix): string {
  return elementMixSegments(mix)
    .map((s) => `${s.label} ${s.count}`)
    .join(" · ");
}

// ---------------------------------------------------------------------------
// Ingest `parse` stage
// ---------------------------------------------------------------------------

export interface IngestParseView {
  readonly pages: number | null;
  readonly tables: number | null;
  readonly diagrams: number | null;
  readonly visionPages: number | null;
  /** Verbatim `layout_source_counts` entries with integer counts, payload order. */
  readonly layoutSourceCounts: readonly (readonly [string, number])[];
  readonly docaiMs: number | null;
  readonly visionMs: number | null;
  /** Document AI processor version verbatim (e.g. "pretrained-layout-parser-v1.5-…"). */
  readonly processorVersion: string | null;
  /** True when the server flags the processor version as a release candidate; null when not reported. */
  readonly releaseCandidate: boolean | null;
}

const RC_KEYS = ["release_candidate", "is_release_candidate", "processor_release_candidate"] as const;
const L3_PARSE_KEYS = ["tables", "diagrams", "vision_pages", "layout_source_counts", "docai_ms", "vision_ms", "processor_version", ...RC_KEYS] as const;

function releaseCandidateFlag(payload: JsonObject): boolean | null {
  for (const k of RC_KEYS) if (typeof payload[k] === "boolean") return payload[k] as boolean;
  return null;
}

/** Null unless the payload has at least one Level 3 parse key (Levels 1–2 parse payloads keep the generic view). */
export function parseIngestParse(payload: JsonObject | null): IngestParseView | null {
  if (!payload || !L3_PARSE_KEYS.some((k) => k in payload)) return null;
  const rawCounts = isObj(payload.layout_source_counts) ? Object.entries(payload.layout_source_counts).slice(0, MAX_LAYOUT_SOURCES) : [];
  const layoutSourceCounts = rawCounts.flatMap(([k, v]) => {
    const n = count(v);
    return n === null ? [] : [[k, n] as const];
  });
  return {
    pages: count(payload.pages),
    tables: count(payload.tables),
    diagrams: count(payload.diagrams),
    visionPages: count(payload.vision_pages),
    layoutSourceCounts,
    docaiMs: ms(payload.docai_ms),
    visionMs: ms(payload.vision_ms),
    processorVersion: typeof payload.processor_version === "string" && payload.processor_version.trim() ? payload.processor_version.trim() : null,
    releaseCandidate: releaseCandidateFlag(payload),
  };
}

/** Key/value rows for the parse summary; absent values are omitted (never shown as zero). */
export function ingestParseItems(view: IngestParseView): (readonly [string, string])[] {
  const rows: (readonly [string, string])[] = [];
  const add = (k: string, v: number | null, f: (n: number) => string = (n) => n.toLocaleString()): void => {
    if (v !== null) rows.push([k, f(v)]);
  };
  add("pages", view.pages);
  add("tables (Document AI)", view.tables);
  add("diagrams", view.diagrams);
  add("vision pages (Gemini)", view.visionPages);
  add("Document AI time", view.docaiMs, formatMs);
  add("vision time", view.visionMs, formatMs);
  if (view.processorVersion !== null) rows.push(["Document AI processor_version", view.processorVersion]);
  if (view.releaseCandidate !== null) rows.push(["release candidate", view.releaseCandidate ? "yes — release-candidate processor version" : "no"]);
  for (const [src, n] of view.layoutSourceCounts) rows.push([`layout_source · ${layoutSourceLabel(src)}`, n.toLocaleString()]);
  return rows;
}

/** One-line diagram node summary for the Level 3 parse stage: "tables=12 · diagrams=4 · vision=4p". */
export function ingestParseSummary(view: IngestParseView): string {
  const parts: string[] = [];
  if (view.pages !== null) parts.push(`pages=${view.pages.toLocaleString()}`);
  if (view.tables !== null) parts.push(`tables=${view.tables.toLocaleString()}`);
  if (view.diagrams !== null) parts.push(`diagrams=${view.diagrams.toLocaleString()}`);
  return parts.join(" · ");
}
