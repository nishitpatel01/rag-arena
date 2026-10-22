/**
 * Level 3 (layout-aware multimodal) element helpers — pure, no React, unit-tested in tests/level3.test.ts.
 *
 * Shared by stage views (retrieve / rerank badges, filter chips), the answer citations and the document
 * viewer (region overlay label, diagram transcription box). Values come verbatim from the server; unknown
 * element types / layout sources are shown as-is (never coerced into a known label).
 */

import type { BoundingBox } from "../types/arena";

export type ElementType = "narrative_text" | "table" | "diagram";

/** Display order for chips and the element-mix bar. */
export const ELEMENT_TYPES: readonly ElementType[] = ["table", "diagram", "narrative_text"];

export const ELEMENT_LABEL: Readonly<Record<ElementType, string>> = {
  table: "TABLE",
  diagram: "DIAGRAM",
  narrative_text: "TEXT",
};

export type LayoutSource = "documentai_layout" | "gemini_vision" | "pymupdf";

export const LAYOUT_SOURCE_LABEL: Readonly<Record<LayoutSource, string>> = {
  documentai_layout: "Document AI layout",
  gemini_vision: "Gemini vision",
  pymupdf: "PyMuPDF",
};

/**
 * Who located a chunk's bbox (backend contract): Document AI v1.5 returns no boxes, so most boxes are
 * found on the PDF text layer; "page" means no region was located (whole page).
 */
export type BboxSource = "documentai" | "pdf_text_layer" | "page";

export const BBOX_SOURCE_LABEL: Readonly<Record<BboxSource, string>> = {
  documentai: "Document AI region",
  pdf_text_layer: "Located on PDF text layer",
  page: "Whole page — region not located",
};

export function isBboxSource(v: unknown): v is BboxSource {
  return v === "documentai" || v === "pdf_text_layer" || v === "page";
}

/** "Document AI region" / "Located on PDF text layer" / "Whole page — region not located"; unknown verbatim. */
export function bboxSourceLabel(raw: string): string {
  return isBboxSource(raw) ? BBOX_SOURCE_LABEL[raw] : `bbox_source: ${raw}`;
}

/** Per-row element metadata carried by Level 3 retrieve / rerank candidates (all optional). */
export interface ElementMeta {
  /** Raw `element_type` string (known or not); null when absent. */
  readonly elementType: string | null;
  readonly layoutSource: string | null;
  readonly amendmentNumber: number | null;
  readonly amendsSection: string | null;
  /** Raw `bbox_source` ("documentai" | "pdf_text_layer" | "page" | unknown); null when absent. */
  readonly bboxSource: string | null;
}

export const EMPTY_ELEMENT_META: ElementMeta = { elementType: null, layoutSource: null, amendmentNumber: null, amendsSection: null, bboxSource: null };

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const nonEmpty = (v: unknown): string | null => (typeof v === "string" && v.trim().length > 0 ? v.trim() : null);

export function isElementType(v: unknown): v is ElementType {
  return v === "narrative_text" || v === "table" || v === "diagram";
}

export function isLayoutSource(v: unknown): v is LayoutSource {
  return v === "documentai_layout" || v === "gemini_vision" || v === "pymupdf";
}

/** Badge text: TABLE / DIAGRAM / TEXT for known types, the raw value upper-cased otherwise. */
export function elementLabel(raw: string): string {
  return isElementType(raw) ? ELEMENT_LABEL[raw] : raw.toUpperCase();
}

/** "Document AI layout" / "Gemini vision" / "PyMuPDF"; unknown sources verbatim. */
export function layoutSourceLabel(raw: string): string {
  return isLayoutSource(raw) ? LAYOUT_SOURCE_LABEL[raw] : raw;
}

/** Amendment numbers arrive as integers (1) or zero-padded strings ("0001"); 0 / absent = base document. */
export function parseAmendmentNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isInteger(v) && v >= 0) return v;
  if (typeof v === "string" && /^\d{1,6}$/.test(v.trim())) return Number(v.trim());
  return null;
}

export function parseElementMeta(item: unknown): ElementMeta {
  if (!isObj(item)) return EMPTY_ELEMENT_META;
  return {
    elementType: nonEmpty(item.element_type),
    layoutSource: nonEmpty(item.layout_source),
    amendmentNumber: parseAmendmentNumber(item.amendment_number),
    amendsSection: nonEmpty(item.amends_section),
    bboxSource: nonEmpty(item.bbox_source),
  };
}

export function hasElementMeta(meta: ElementMeta): boolean {
  return meta.elementType !== null || meta.layoutSource !== null || (meta.amendmentNumber ?? 0) > 0 || meta.amendsSection !== null || meta.bboxSource !== null;
}

/** "Amendment 0001 · supersedes SEC-4.2"; null for the base document (number 0/absent and no amends_section). */
export function amendmentLabel(amendmentNumber: number | null, amendsSection: string | null): string | null {
  const n = amendmentNumber !== null && amendmentNumber > 0 ? amendmentNumber : null;
  if (n === null && amendsSection === null) return null;
  const head = n === null ? "Amendment" : `Amendment ${String(n).padStart(4, "0")}`;
  return amendsSection ? `${head} · supersedes ${amendsSection}` : head;
}

// ---------------------------------------------------------------------------
// Element-type filter chips
// ---------------------------------------------------------------------------

export type ElementFilter = "all" | ElementType | "other";

export type ElementCounts = Readonly<Record<ElementType | "other", number>>;

function bucketOf(elementType: string | null): ElementType | "other" {
  return elementType !== null && isElementType(elementType) ? elementType : "other";
}

export function countElementTypes(rows: readonly { readonly elementType: string | null }[]): ElementCounts {
  const out: Record<ElementType | "other", number> = { table: 0, diagram: 0, narrative_text: 0, other: 0 };
  for (const r of rows) out[bucketOf(r.elementType)] += 1;
  return out;
}

export function matchesElementFilter(elementType: string | null, filter: ElementFilter): boolean {
  return filter === "all" || bucketOf(elementType) === filter;
}

/** Chips to render: "all" plus every bucket with ≥1 row ("other" only when present). */
export function elementFilterOptions(counts: ElementCounts, total: number): readonly { readonly filter: ElementFilter; readonly label: string; readonly count: number }[] {
  const out: { filter: ElementFilter; label: string; count: number }[] = [{ filter: "all", label: "ALL", count: total }];
  for (const t of ELEMENT_TYPES) if (counts[t] > 0) out.push({ filter: t, label: ELEMENT_LABEL[t], count: counts[t] });
  if (counts.other > 0) out.push({ filter: "other", label: "OTHER / NOT REPORTED", count: counts.other });
  return out;
}

// ---------------------------------------------------------------------------
// Citations: region overlay + diagram transcription
// ---------------------------------------------------------------------------

export interface CitationLike {
  readonly element_type?: string | null;
  readonly layout_source?: string | null;
  readonly bbox_source?: string | null;
  readonly bbox?: BoundingBox | null;
  readonly region_bbox?: BoundingBox | null;
}

export interface CitationRegion {
  readonly kind: "table" | "diagram";
  /**
   * Names who identified the element AND who located the box, e.g. "Document AI table region" (only when
   * bbox_source is "documentai"), "Gemini vision diagram · located on PDF text layer",
   * "Document AI layout table · whole page — region not located".
   */
  readonly label: string;
  readonly box: BoundingBox;
  /** Raw bbox_source (null when the server did not report it). */
  readonly bboxSource: string | null;
}

const validBox = (b: unknown): b is BoundingBox =>
  isObj(b) && ["x0", "y0", "x1", "y1"].every((k) => typeof b[k] === "number" && Number.isFinite(b[k] as number)) && (b.x1 as number) > (b.x0 as number) && (b.y1 as number) > (b.y0 as number);

/** Overlay label from the element identity (layout_source) and the box provenance (bbox_source). Never
 * implies Document AI drew a box unless bbox_source is "documentai" (spec P5). */
export function regionLabel(kind: string, layoutSource: string | null, bboxSource: string | null): string {
  const element = layoutSource ? `${layoutSourceLabel(layoutSource)} ${kind}` : kind;
  if (bboxSource === "documentai") return `Document AI ${kind} region`;
  if (bboxSource === "pdf_text_layer") return `${element} · located on PDF text layer`;
  if (bboxSource === "page") return `${element} · whole page — region not located`;
  if (bboxSource) return `${element} · ${bboxSourceLabel(bboxSource)}`;
  return `${element} · region source not reported`;
}

/**
 * Region overlay for table / diagram citations, labelled honestly by who identified the element and who
 * located the box (spec P5). Prefers a dedicated `region_bbox` when the server sends one, else the
 * citation `bbox`. Null for narrative text or when no valid box exists.
 */
export function citationRegion(c: CitationLike): CitationRegion | null {
  const kind = c.element_type === "table" || c.element_type === "diagram" ? c.element_type : null;
  if (!kind) return null;
  const box = validBox(c.region_bbox) ? c.region_bbox : validBox(c.bbox) ? c.bbox : null;
  if (!box) return null;
  const bboxSource = nonEmpty(c.bbox_source);
  return { kind, label: regionLabel(kind, nonEmpty(c.layout_source), bboxSource), box, bboxSource };
}

/** Title of the transcription box for diagram citations; null for non-diagram citations. */
export function transcriptionTitle(c: CitationLike): string | null {
  if (c.element_type !== "diagram") return null;
  const src = nonEmpty(c.layout_source);
  if (src === "gemini_vision") return "Diagram transcription (Gemini vision)";
  return src ? `Diagram text (${layoutSourceLabel(src)})` : "Diagram text (layout source not reported)";
}
