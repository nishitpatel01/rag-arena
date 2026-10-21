/**
 * Document viewer helper tests — pure URL building, rect projection and honest labels.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  clampPage,
  dpiForZoom,
  highlightLabel,
  highlightSource,
  highlightsUrl,
  pageImageUrl,
  rectToPercent,
  type HighlightResponse,
} from "../src/lib/documents";

const CIT = {
  solicitation_id: "solicitation_01",
  doc_name: "04_Pricing Schedule&CLINs.pdf",
  excerpt: "  SCHEDULE B: CONTRACT LINE ITEM ".padEnd(900, "x"),
};

const HL = (matched: boolean, rects = matched ? [{ x0: 0.1, y0: 0.1, x1: 0.5, y1: 0.2 }] : []): HighlightResponse => ({
  page_width: 612, page_height: 792, page_count: 3, rects, method: "pymupdf.search_for", matched, query_used: matched ? "q" : null, attempts: 1,
});

test("documents: URLs encode params and bound the highlight text to 600 chars", () => {
  const img = new URL(pageImageUrl(CIT, 2, 110), "http://x");
  assert.equal(img.pathname, "/api/v1/documents/page");
  assert.equal(img.searchParams.get("doc_name"), "04_Pricing Schedule&CLINs.pdf");
  assert.equal(img.searchParams.get("page"), "2");
  assert.equal(img.searchParams.get("dpi"), "110");
  const hl = new URL(highlightsUrl(CIT, 1), "http://x");
  assert.equal(hl.pathname, "/api/v1/documents/highlights");
  assert.equal(hl.searchParams.get("text")?.length, 600);
  assert.ok(hl.searchParams.get("text")?.startsWith("SCHEDULE B"));
});

test("documents: zoom → dpi is clamped to the backend 200 DPI ceiling", () => {
  assert.equal(dpiForZoom(100), 110);
  assert.equal(dpiForZoom(150), 165);
  assert.equal(dpiForZoom(200), 200);
});

test("documents: rectToPercent clamps into the page and rejects degenerate/malformed boxes", () => {
  assert.deepEqual(rectToPercent({ x0: 0.1, y0: 0.25, x1: 0.6, y1: 0.5 }), { left: "10.000%", top: "25.000%", width: "50.000%", height: "25.000%" });
  assert.deepEqual(rectToPercent({ x0: -0.2, y0: 0.9, x1: 1.4, y1: 1.2 }), { left: "0.000%", top: "90.000%", width: "100.000%", height: "10.000%" });
  assert.equal(rectToPercent({ x0: 0.5, y0: 0.5, x1: 0.5, y1: 0.7 }), null);
  assert.equal(rectToPercent({ x0: Number.NaN, y0: 0, x1: 1, y1: 1 }), null);
  assert.equal(rectToPercent(null), null);
});

test("documents: highlight provenance is honest (match / stored bbox / failure / other page)", () => {
  assert.equal(highlightSource(1, 1, HL(true), null), "text_match");
  assert.equal(highlightSource(1, 1, HL(false), null), "stored_bbox");
  assert.equal(highlightSource(1, 1, HL(true, []), null), "stored_bbox"); // matched without rects never claims a match
  assert.equal(highlightSource(1, 1, null, "HTTP 404"), "lookup_failed");
  assert.equal(highlightSource(1, 1, null, null), "loading");
  assert.equal(highlightSource(2, 1, HL(true), null), "other_page");
  assert.equal(highlightLabel("text_match"), "Highlighted by text match (PyMuPDF search_for)");
  assert.equal(highlightLabel("stored_bbox"), "Excerpt not located on page — showing stored chunk bbox");
});

test("documents: page navigation stays within bounds", () => {
  assert.equal(clampPage(0, 3), 1);
  assert.equal(clampPage(4, 3), 3);
  assert.equal(clampPage(2, 3), 2);
  assert.equal(clampPage(9, null), 9);
});
