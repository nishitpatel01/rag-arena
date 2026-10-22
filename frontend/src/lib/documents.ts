/**
 * Document viewer client: real page PNGs and excerpt highlights from /api/v1/documents/*.
 * Pure helpers (URL building, rect projection, labels) are unit-tested in tests/documents.test.ts.
 */

import type { BoundingBox, Citation } from "../types/arena";

export const DOCUMENTS_BASE = "/api/v1/documents";
export const ZOOM_LEVELS = [100, 150, 200] as const;
export type ZoomLevel = (typeof ZOOM_LEVELS)[number];
export const BASE_DPI = 110;
export const THUMB_DPI = 72;
const MAX_DPI = 200;
const MAX_HIGHLIGHT_TEXT = 600;
const FETCH_TIMEOUT_MS = 20_000;
const MAX_CACHE_ENTRIES = 50;

export interface NormalizedRect {
  readonly x0: number;
  readonly y0: number;
  readonly x1: number;
  readonly y1: number;
}

export interface HighlightResponse {
  readonly page_width: number;
  readonly page_height: number;
  readonly page_count: number;
  readonly rects: readonly NormalizedRect[];
  readonly method: string;
  readonly matched: boolean;
  readonly query_used: string | null;
  readonly attempts: number;
}

export interface PercentBox {
  readonly left: string;
  readonly top: string;
  readonly width: string;
  readonly height: string;
}

/** DPI for a zoom level: crisp at higher zoom, clamped to the backend's 200 DPI ceiling. */
export function dpiForZoom(zoom: ZoomLevel): number {
  return Math.min(MAX_DPI, Math.round((BASE_DPI * zoom) / 100));
}

function docParams(c: Pick<Citation, "solicitation_id" | "doc_name">, page: number): URLSearchParams {
  return new URLSearchParams({ solicitation_id: c.solicitation_id, doc_name: c.doc_name, page: String(page) });
}

export function pageImageUrl(c: Pick<Citation, "solicitation_id" | "doc_name">, page: number, dpi: number): string {
  const p = docParams(c, page);
  p.set("dpi", String(dpi));
  return `${DOCUMENTS_BASE}/page?${p.toString()}`;
}

export function highlightsUrl(c: Pick<Citation, "solicitation_id" | "doc_name" | "excerpt">, page: number): string {
  const p = docParams(c, page);
  p.set("text", c.excerpt.trim().slice(0, MAX_HIGHLIGHT_TEXT));
  return `${DOCUMENTS_BASE}/highlights?${p.toString()}`;
}

/** Clamp a normalized rect into the unit square and express it as CSS percentages; null when degenerate. */
export function rectToPercent(r: NormalizedRect | BoundingBox | null | undefined): PercentBox | null {
  if (!r || ![r.x0, r.y0, r.x1, r.y1].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const clamp = (v: number): number => Math.max(0, Math.min(1, v));
  const x0 = clamp(r.x0), y0 = clamp(r.y0), x1 = clamp(r.x1), y1 = clamp(r.y1);
  if (x1 <= x0 || y1 <= y0) return null;
  const pct = (v: number): string => `${(v * 100).toFixed(3)}%`;
  return { left: pct(x0), top: pct(y0), width: pct(x1 - x0), height: pct(y1 - y0) };
}

export type HighlightSource = "text_match" | "stored_bbox" | "lookup_failed" | "other_page" | "loading";

/**
 * Honest provenance label for what the overlay shows (spec P5). `regionLabel` (Level 3 table / diagram
 * citations, e.g. "Gemini vision diagram region") names the layout region drawn instead of the generic
 * stored chunk bbox; with a text match the region outline is drawn in addition and named too.
 */
export function highlightLabel(source: HighlightSource, regionLabel: string | null = null): string {
  switch (source) {
    case "text_match":
      return regionLabel
        ? `Highlighted by text match (PyMuPDF search_for) · dashed outline: ${regionLabel}`
        : "Highlighted by text match (PyMuPDF search_for)";
    case "stored_bbox":
      return regionLabel
        ? `Excerpt not located on page by text match — showing ${regionLabel}`
        : "Excerpt not located on page — showing stored chunk bbox";
    case "lookup_failed":
      return regionLabel ? `Highlight lookup failed — showing ${regionLabel}` : "Highlight lookup failed — showing stored chunk bbox";
    case "other_page":
      return "Highlights are shown only on the cited page";
    case "loading":
      return "Locating excerpt on page…";
  }
}

export function highlightSource(
  viewingPage: number,
  citedPage: number,
  hl: HighlightResponse | null,
  hlError: string | null
): HighlightSource {
  if (viewingPage !== citedPage) return "other_page";
  if (hlError) return "lookup_failed";
  if (!hl) return "loading";
  return hl.matched && hl.rects.length > 0 ? "text_match" : "stored_bbox";
}

/** Clamp a requested page into [1, pageCount] (pageCount unknown → only the lower bound applies). */
export function clampPage(page: number, pageCount: number | null): number {
  const lower = Math.max(1, Math.floor(page));
  return pageCount && pageCount > 0 ? Math.min(lower, pageCount) : lower;
}

export class DocumentFetchError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "DocumentFetchError";
    this.status = status;
  }
}

async function fetchBounded(url: string, signal: AbortSignal | undefined): Promise<Response> {
  const controller = new AbortController();
  const onAbort = (): void => controller.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  const timer = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      const text = (await response.text()).slice(0, 400);
      throw new DocumentFetchError(response.status, `HTTP ${response.status} ${response.statusText}: ${text || "(empty body)"} — GET ${url}`);
    }
    return response;
  } catch (err) {
    if (controller.signal.aborted && !signal?.aborted) {
      throw new DocumentFetchError(0, `timed out after ${FETCH_TIMEOUT_MS / 1000} s — GET ${url}`);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}

export interface PageImage {
  readonly objectUrl: string;
  readonly pageCount: number | null;
}

/** Fetch a rendered page as a blob URL (caller revokes it); surfaces the HTTP error verbatim. */
export async function fetchPageImage(url: string, signal?: AbortSignal): Promise<PageImage> {
  const response = await fetchBounded(url, signal);
  const count = Number(response.headers.get("X-Page-Count"));
  const blob = await response.blob();
  return { objectUrl: URL.createObjectURL(blob), pageCount: Number.isInteger(count) && count > 0 ? count : null };
}

const highlightCache = new Map<string, HighlightResponse>();

export async function fetchHighlights(url: string, signal?: AbortSignal): Promise<HighlightResponse> {
  const cached = highlightCache.get(url);
  if (cached) return cached;
  const response = await fetchBounded(url, signal);
  const body = (await response.json()) as HighlightResponse;
  if (typeof body !== "object" || body === null || !Array.isArray(body.rects) || typeof body.matched !== "boolean") {
    throw new DocumentFetchError(response.status, `Malformed highlights response — GET ${url}`);
  }
  if (highlightCache.size >= MAX_CACHE_ENTRIES) {
    const oldest = highlightCache.keys().next().value;
    if (oldest !== undefined) highlightCache.delete(oldest);
  }
  highlightCache.set(url, body);
  return body;
}
