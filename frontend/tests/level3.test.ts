/**
 * Level 3 (layout-aware multimodal) helper tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: element / layout / amendment labels, element-type filters, citation region + transcription labels,
 * Level 3 stage payload parsers (element_intent, authority headers, element mix, ingest parse summary),
 * rerank ← retrieve metadata join, the generalised compare teaser, the DEV fixture, and safe-mode masking.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  EXCERPT_FALLBACK_NOTE,
  amendmentLabel,
  bboxSourceLabel,
  citationRegion,
  diagramTranscription,
  countElementTypes,
  elementFilterOptions,
  elementLabel,
  layoutSourceLabel,
  matchesElementFilter,
  parseAmendmentNumber,
  parseElementMeta,
  transcriptionTitle,
} from "../src/lib/elements";
import {
  elementMixLine,
  elementMixSegments,
  ingestParseItems,
  ingestParseSummary,
  isReleaseCandidateChannel,
  parseElementIntent,
  parseIngestParse,
  parseIntentMatches,
  parseRetrieveL3Summary,
  parseSynthesizeL3,
  retrieveL3Lines,
} from "../src/features/levels/level3Payloads";
import { anyElementMeta, joinElementMeta, parseHybridRetrieve, parseRerank, retrieveMetaByChunk, summarizeStage } from "../src/features/levels/stagePayloads";
import { highlightLabel } from "../src/lib/documents";
import { baselinesFor, lowerEnabledLevels, teaserMatrix, teaserTitle } from "../src/features/levels/compareTeaserModel";
import { MAX_RETRIEVED_CHUNKS, deriveIngestProgress, reduceStreamEvents, retrievedChunkTexts, type TimelineEvent } from "../src/features/levels/model";
import { maskDeep, MASK_BUCKET, MASK_PROJECT_ID, MASK_PROJECT_NUMBER } from "../src/lib/safeMode";
import { LEVEL3_DEV_FIXTURE } from "../src/dev/fixtures/level3_run";
import type { Citation, JsonObject, LeaderboardResponse, ModelsResponse } from "../src/types/arena";

const BOX = { page: 1, x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.7 };

// ---------------------------------------------------------------------------
// labels
// ---------------------------------------------------------------------------

test("element and layout source labels: known values mapped, unknown values verbatim", () => {
  assert.equal(elementLabel("table"), "TABLE");
  assert.equal(elementLabel("diagram"), "DIAGRAM");
  assert.equal(elementLabel("narrative_text"), "TEXT");
  assert.equal(elementLabel("list_item"), "LIST_ITEM");
  assert.equal(layoutSourceLabel("documentai_layout"), "Document AI layout");
  assert.equal(layoutSourceLabel("gemini_vision"), "Gemini vision");
  assert.equal(layoutSourceLabel("pymupdf"), "PyMuPDF");
  assert.equal(layoutSourceLabel("ocr_v9"), "ocr_v9");
});

test("amendment chip: zero-padded number + superseded section; base document has no chip", () => {
  assert.equal(amendmentLabel(1, "SEC-4.2"), "Amendment 0001 · supersedes SEC-4.2");
  assert.equal(amendmentLabel(12, null), "Amendment 0012");
  assert.equal(amendmentLabel(0, null), null);
  assert.equal(amendmentLabel(null, null), null);
  assert.equal(amendmentLabel(null, "SEC-5.1"), "Amendment · supersedes SEC-5.1");
  assert.equal(parseAmendmentNumber("0001"), 1);
  assert.equal(parseAmendmentNumber(2), 2);
  assert.equal(parseAmendmentNumber(-1), null);
  assert.equal(parseAmendmentNumber("A1"), null);
  assert.equal(parseAmendmentNumber(1.5), null);
});

test("parseElementMeta reads the four Level 3 row fields and ignores blanks / wrong types", () => {
  assert.deepEqual(parseElementMeta({ element_type: "table", layout_source: "documentai_layout", amendment_number: "0001", amends_section: "SEC-4.2", bbox_source: "pdf_text_layer" }), {
    elementType: "table",
    layoutSource: "documentai_layout",
    amendmentNumber: 1,
    amendsSection: "SEC-4.2",
    bboxSource: "pdf_text_layer",
  });
  const empty = { elementType: null, layoutSource: null, amendmentNumber: null, amendsSection: null, bboxSource: null };
  assert.deepEqual(parseElementMeta({ element_type: " ", layout_source: 5, amendment_number: null }), empty);
  assert.deepEqual(parseElementMeta(null), empty);
});

test("element filter: counts per bucket, unknown types fall into 'other', chips only for present buckets", () => {
  const rows = [{ elementType: "table" }, { elementType: "diagram" }, { elementType: "diagram" }, { elementType: "narrative_text" }, { elementType: null }, { elementType: "title" }];
  const counts = countElementTypes(rows);
  assert.deepEqual(counts, { table: 1, diagram: 2, narrative_text: 1, other: 2 });
  assert.ok(matchesElementFilter("table", "all"));
  assert.ok(matchesElementFilter("diagram", "diagram"));
  assert.ok(!matchesElementFilter("table", "diagram"));
  assert.ok(matchesElementFilter(null, "other"));
  assert.ok(matchesElementFilter("title", "other"));
  assert.deepEqual(
    elementFilterOptions(counts, rows.length).map((o) => `${o.label}:${o.count}`),
    ["ALL:6", "TABLE:1", "DIAGRAM:2", "TEXT:1", "OTHER / NOT REPORTED:2"]
  );
  assert.deepEqual(
    elementFilterOptions({ table: 0, diagram: 3, narrative_text: 0, other: 0 }, 3).map((o) => o.filter),
    ["all", "diagram"]
  );
});

// ---------------------------------------------------------------------------
// citations: region overlay + transcription
// ---------------------------------------------------------------------------

test("citation region labels: element identity from layout_source, box provenance from bbox_source (never implies Document AI drew it)", () => {
  assert.equal(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox_source: "documentai", bbox: BOX })?.label, "Document AI table region");
  assert.equal(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox_source: "pdf_text_layer", bbox: BOX })?.label, "Document AI layout table · located on PDF text layer");
  assert.equal(citationRegion({ element_type: "diagram", layout_source: "gemini_vision", bbox_source: "pdf_text_layer", bbox: BOX })?.label, "Gemini vision diagram · located on PDF text layer");
  assert.equal(citationRegion({ element_type: "diagram", layout_source: "gemini_vision", bbox_source: "page", bbox: BOX })?.label, "Gemini vision diagram · whole page — region not located");
  assert.equal(citationRegion({ element_type: "diagram", layout_source: "gemini_vision", bbox: BOX })?.label, "Gemini vision diagram · region source not reported");
  assert.equal(citationRegion({ element_type: "diagram", layout_source: null, bbox_source: "weird", bbox: BOX })?.label, "diagram · bbox_source: weird");
  for (const src of ["pdf_text_layer", "page", null]) {
    assert.ok(!/^Document AI (table|diagram) region/.test(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox_source: src, bbox: BOX })?.label ?? ""), String(src));
  }
  assert.equal(citationRegion({ element_type: "narrative_text", layout_source: "documentai_layout", bbox: BOX }), null);
  assert.equal(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox: { page: 1, x0: 0.5, y0: 0.2, x1: 0.4, y1: 0.7 } }), null, "degenerate box");
  const region = { page: 1, x0: 0.2, y0: 0.3, x1: 0.8, y1: 0.6 };
  assert.deepEqual(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox: BOX, region_bbox: region })?.box, region, "region_bbox preferred");
  assert.deepEqual(citationRegion({ element_type: "table", layout_source: "documentai_layout", bbox: BOX, region_bbox: null })?.box, BOX);
  assert.equal(bboxSourceLabel("documentai"), "Document AI region");
  assert.equal(bboxSourceLabel("pdf_text_layer"), "Located on PDF text layer");
  assert.equal(bboxSourceLabel("page"), "Whole page — region not located");
});

test("transcription box title is honest about the layout source", () => {
  assert.equal(transcriptionTitle({ element_type: "diagram", layout_source: "gemini_vision" }), "Diagram transcription (Gemini vision)");
  assert.equal(transcriptionTitle({ element_type: "diagram", layout_source: "documentai_layout" }), "Diagram text (Document AI layout)");
  assert.equal(transcriptionTitle({ element_type: "diagram" }), "Diagram text (layout source not reported)");
  assert.equal(transcriptionTitle({ element_type: "table", layout_source: "gemini_vision" }), null);
});

test("diagram transcription: full text from retrieved_chunks by chunk_id; excerpt fallback flagged", () => {
  const citation = { chunk_id: "c_diag", excerpt: "Architecture Specification … [DIAGRAM transcription] Diagram: VECTOR DIAGRAM …" };
  const full = "Architecture Specification ARCH-TOPOLOGY-01\n[DIAGRAM transcription · gemini-4-argon]\n" + "- component (box)\n".repeat(80);
  const chunks = [{ chunk_id: "other", content: "nope" }, { chunk_id: "c_diag", content: full }];
  assert.deepEqual(diagramTranscription(citation, chunks), { text: full, full: true });
  assert.deepEqual(diagramTranscription(citation, [{ chunk_id: "other", content: "nope" }]), { text: citation.excerpt, full: false }, "no match → excerpt");
  assert.deepEqual(diagramTranscription(citation, [{ chunk_id: "c_diag", content: "   " }]), { text: citation.excerpt, full: false }, "blank content → excerpt");
  assert.deepEqual(diagramTranscription(citation, undefined), { text: citation.excerpt, full: false });
  assert.equal(EXCERPT_FALLBACK_NOTE, "(excerpt — full text not in stream)");
});

test("answer_complete.retrieved_chunks kept on the timeline as {chunk_id, content}; malformed rows dropped; bounded", () => {
  assert.deepEqual(retrievedChunkTexts([{ chunk_id: "a", content: "x", score: 1 }, { chunk_id: "", content: "y" }, { chunk_id: "b" }, null, "s"]), [{ chunk_id: "a", content: "x" }]);
  assert.deepEqual(retrievedChunkTexts(undefined), []);
  assert.equal(retrievedChunkTexts(Array.from({ length: MAX_RETRIEVED_CHUNKS + 5 }, (_, i) => ({ chunk_id: `c${i}`, content: "t" }))).length, MAX_RETRIEVED_CHUNKS);
  const R = "run_x";
  const T = "2026-10-22T00:00:00Z";
  const state = reduceStreamEvents([
    { event: "run_started", run_id: R, engines: ["level3_multimodal"], ts: T },
    { event: "answer_complete", run_id: R, engine: "level3_multimodal", answer: "a", citations: [], latency_ms: 1, cost_usd: 0, usage: { prompt_tokens: 0, output_tokens: 0, embedding_tokens: 0 }, flags: { reranker_used: true, adk_runner_used: false }, retrieved_chunks: [{ chunk_id: "d", content: "full diagram text" }], ts: T },
  ] as unknown as TimelineEvent[]);
  assert.deepEqual(state.timelines.level3_multimodal?.answer?.retrievedChunks, [{ chunk_id: "d", content: "full diagram text" }]);
});

test("highlight label names the region instead of the generic stored bbox; text match still preferred", () => {
  const r = "Gemini vision diagram · located on PDF text layer";
  assert.equal(highlightLabel("text_match", r), `Highlighted by text match (PyMuPDF search_for) · dashed outline: ${r}`);
  assert.equal(highlightLabel("stored_bbox", r), `Excerpt not located on page by text match — showing ${r}`);
  assert.equal(highlightLabel("lookup_failed", r), `Highlight lookup failed — showing ${r}`);
  assert.equal(highlightLabel("stored_bbox"), "Excerpt not located on page — showing stored chunk bbox");
  assert.equal(highlightLabel("other_page", r), "Highlights are shown only on the cited page");
});

// ---------------------------------------------------------------------------
// stage payloads
// ---------------------------------------------------------------------------

test("element_intent: verbatim list, blanks dropped, null when absent", () => {
  assert.deepEqual(parseElementIntent({ element_intent: ["diagram", " table ", "", 3] }), ["diagram", "table"]);
  assert.deepEqual(parseElementIntent({ element_intent: [] }), []);
  assert.equal(parseElementIntent({ expansions: [] }), null);
  assert.equal(parseElementIntent(null), null);
});

test("query_prep intent_matches: keywords per intent; empty map when absent", () => {
  const m = parseIntentMatches({ element_intent: ["diagram"], intent_matches: { diagram: ["diagram", "topology", 4], table: "bad" } });
  assert.deepEqual([...m.entries()], [["diagram", ["diagram", "topology"]]]);
  assert.equal(parseIntentMatches({}).size, 0);
});

test("retrieve Level 3 summary: embedding column, element/layout counts, amendment candidates; null on Level 2", () => {
  const v = parseRetrieveL3Summary({ embedding_column: "embedding_multimodal", element_types: { diagram: 3, narrative_text: 39, table: 8 }, layout_sources: { gemini_vision: 3, documentai_layout: 47 }, amendment_candidates: 8 });
  assert.ok(v);
  assert.deepEqual(retrieveL3Lines(v), [
    "embedding column: embedding_multimodal",
    "element types: TABLE 8 · DIAGRAM 3 · TEXT 39",
    "layout sources: Gemini vision 3 · Document AI layout 47",
    "amendment candidates: 8",
  ]);
  assert.equal(parseRetrieveL3Summary({ rows_returned: 50, candidates: [] }), null);
});

test("synthesize: authority header objects keep context order with supersedes chips; element mix → segments", () => {
  const v = parseSynthesizeL3({
    authority_headers: [
      { chunk_id: "a1", header: "[AMENDMENT 0001 · SUPERSEDES SEC-4.2.1] [TEXT] 06.pdf", amendment_number: 1, effective_date: "2026-03-15", supersedes: ["SEC-4.2.1", "CLIN-0004-AB"], superseded_by: [], element_type: "narrative_text", layout_source: "documentai_layout" },
      { chunk_id: "b1", header: "[BASE] [DIAGRAM transcription] 03.pdf", amendment_number: 0, supersedes: [], superseded_by: ["AMD-0001"], element_type: "diagram", layout_source: "gemini_vision" },
      { chunk_id: "bad" },
      "[legacy string header]",
    ],
    element_mix: { narrative_text: 4, diagram: 1 },
    context_order: "latest amendment first, then rerank order",
  });
  assert.ok(v?.authorityHeaders);
  assert.deepEqual(v.authorityHeaders.map((h) => h.header), ["[AMENDMENT 0001 · SUPERSEDES SEC-4.2.1] [TEXT] 06.pdf", "[BASE] [DIAGRAM transcription] 03.pdf", "[legacy string header]"]);
  assert.deepEqual(v.authorityHeaders[0].supersedes, ["SEC-4.2.1", "CLIN-0004-AB"]);
  assert.deepEqual(v.authorityHeaders[1].supersededBy, ["AMD-0001"]);
  assert.equal(v.authorityHeaders[0].amendmentNumber, 1);
  assert.equal(v.invalidHeaders, 1, "header-less object counted, not shown");
  assert.equal(v.contextOrder, "latest amendment first, then rerank order");
  assert.ok(v.elementMix);
  assert.equal(elementMixLine(v.elementMix), "DIAGRAM 1 · TEXT 4", "known order, only reported types");
  assert.deepEqual(elementMixSegments(v.elementMix).map((s) => [s.type, s.fraction]), [["diagram", 0.2], ["narrative_text", 0.8]]);
  assert.equal(parseSynthesizeL3({ model: "gemini-4-argon", prompt_tokens: 10 }), null, "Level 1–2 synthesize payload → generic view");
  const odd = parseSynthesizeL3({ element_mix: { figure: 2, table: -1 } });
  assert.deepEqual(odd?.elementMix, [["figure", 2]], "unknown types verbatim, invalid counts dropped");
  assert.equal(elementMixLine(odd?.elementMix ?? []), "FIGURE 2");
  assert.equal(summarizeStage("synthesize", { element_mix: { table: 1, diagram: 2, narrative_text: 2 } }), "TABLE 1 · DIAGRAM 2 · TEXT 2");
});

test("ingest parse summary: published contract (vision_pages[], failures, processor channel, truncated)", () => {
  const v = parseIngestParse({
    docs: 45, pages: 526, tables: 41, diagrams: 2, figures: 5,
    vision_pages: [
      { doc_name: "03.pdf", page: 1, title: "Multi-Region VPC", components: 6, connections: 5, regions: 2, vision_ms: 9100, transcription: { title: "x", components: [] } },
      { doc_name: "02.pdf", page: 2, title: null, components: 4, connections: 3, regions: 0, vision_ms: 8000 },
    ],
    vision_failed_pages: [{ doc_name: "07.pdf", page: 3, reason: "vector drawings", status: "failed", error_type: "VisionTranscriptionError", error: "no labelled components" }],
    layout_source_counts: { documentai_layout: 517, gemini_vision: 2, bad: "x" },
    docai_ms: 61234, vision_ms: 17100,
    processor_version: "pretrained-layout-parser-v1.5-2025-08-25", processor_version_channel: "release candidate (preview)", vision_model: "gemini-4-argon",
    truncated: true,
  });
  assert.ok(v);
  assert.equal(v.visionPages, 2);
  assert.equal(v.visionPageRows[0].title, "Multi-Region VPC");
  assert.equal(v.visionFailedPages[0].errorType, "VisionTranscriptionError");
  assert.equal(v.truncated, true);
  assert.ok(isReleaseCandidateChannel(v.processorVersionChannel));
  assert.ok(!isReleaseCandidateChannel("stable"));
  assert.deepEqual(v.layoutSourceCounts, [["documentai_layout", 517], ["gemini_vision", 2]]);
  assert.deepEqual(ingestParseItems(v).map(([k]) => k), [
    "pages",
    "tables (Document AI)",
    "figures (Document AI)",
    "diagrams (Gemini vision)",
    "vision pages transcribed",
    "vision pages failed",
    "Document AI time",
    "vision time",
    "Document AI processor_version",
    "processor_version_channel",
    "vision model",
    "layout_source · Document AI layout",
    "layout_source · Gemini vision",
  ]);
  assert.equal(ingestParseSummary(v), "pages=526 · tables=41 · diagrams=2");
  assert.equal(summarizeStage("parse", { pages: 526, tables: 41, diagrams: 9 }), "pages=526 · tables=41 · diagrams=9");
  assert.equal(parseIngestParse({ docs: 45, pages: 526, parser: "pymupdf" }), null, "Level 1–2 parse payload → generic view");
  const sparse = parseIngestParse({ tables: 3 });
  assert.ok(sparse);
  assert.deepEqual(ingestParseItems(sparse), [["tables (Document AI)", "3"]]);
  assert.equal(sparse.truncated, false);
});

test("doc_progress tables/diagrams merge per document; null when never reported", () => {
  const J = "ing_1";
  const T = "2026-10-22T00:00:00Z";
  const events = [
    { event: "doc_progress", job_id: J, doc_name: "a.pdf", pages: 3, chunks: 0, tables: 2, diagrams: 1, ts: T },
    { event: "doc_progress", job_id: J, doc_name: "a.pdf", pages: 3, chunks: 9, ts: T },
    { event: "doc_progress", job_id: J, doc_name: "b.pdf", pages: 1, chunks: 4, ts: T },
    { event: "doc_progress", job_id: J, stage: "parse", step: "gemini_vision", doc_name: "a.pdf", pages: 0, chunks: 0, status: "ok", ts: T },
    { event: "doc_progress", job_id: J, stage: "parse", step: "gemini_vision", doc_name: "a.pdf", pages: 0, chunks: 0, status: "failed", error: "x", ts: T },
    { event: "doc_progress", job_id: J, stage: "parse", step: "gemini_vision", doc_name: "a.pdf", pages: 0, chunks: 0, status: "ok", ts: T },
  ] as unknown as TimelineEvent[];
  assert.deepEqual(deriveIngestProgress(events).docs, [
    { doc_name: "a.pdf", pages: 3, chunks: 9, tables: 2, diagrams: 1, visionOk: 2, visionFailed: 1 },
    { doc_name: "b.pdf", pages: 1, chunks: 4, tables: null, diagrams: null },
  ]);
});

const L3_CANDIDATES: JsonObject[] = [
  { chunk_id: "c1", doc_name: "03.pdf", page_number: 1, rrf_score: 0.03, vector_rank: 1, lexical_rank: 1, element_type: "diagram", layout_source: "gemini_vision", amendment_number: 0, amends_section: null },
  { chunk_id: "c2", doc_name: "06.pdf", page_number: 1, rrf_score: 0.02, vector_rank: 2, lexical_rank: null, element_type: "narrative_text", layout_source: "documentai_layout", amendment_number: 1, amends_section: "SEC-4.2.1" },
  { chunk_id: "c3", doc_name: "05.pdf", page_number: 2, rrf_score: 0.01, vector_rank: null, lexical_rank: 2, element_type: "table", layout_source: "documentai_layout" },
];

test("retrieve candidates carry element metadata; Level 2 payloads have none (no extra columns)", () => {
  const v = parseHybridRetrieve({ rows_returned: 3, candidates: L3_CANDIDATES });
  assert.ok(v);
  assert.ok(anyElementMeta(v.candidates));
  assert.deepEqual(v.candidates[1].meta, { elementType: "narrative_text", layoutSource: "documentai_layout", amendmentNumber: 1, amendsSection: "SEC-4.2.1", bboxSource: null });
  const l2 = parseHybridRetrieve({ candidates: [{ chunk_id: "x", rrf_score: 0.1, vector_rank: 1, lexical_rank: null }] });
  assert.ok(l2);
  assert.equal(anyElementMeta(l2.candidates), false);
});

test("rerank rows join missing element fields from retrieve by chunk_id; own fields win", () => {
  const byChunk = retrieveMetaByChunk({ candidates: L3_CANDIDATES });
  assert.equal(byChunk.size, 3);
  const rr = parseRerank({
    results: [
      { chunk_id: "c3", rrf_rank: 3, rerank_rank: 1, rerank_score: 0.9 },
      { chunk_id: "c1", rrf_rank: 1, rerank_rank: 2, rerank_score: 0.8, element_type: "table" },
      { chunk_id: "zz", rrf_rank: 9, rerank_rank: 3, rerank_score: 0.1 },
    ],
  });
  assert.ok(rr);
  const joined = rr.results.map((r) => joinElementMeta(r.meta, byChunk.get(r.chunkId)));
  assert.equal(joined[0].elementType, "table");
  assert.equal(joined[0].layoutSource, "documentai_layout");
  assert.equal(joined[1].elementType, "table", "result's own element_type wins");
  assert.equal(joined[1].layoutSource, "gemini_vision");
  assert.deepEqual(joined[2], { elementType: null, layoutSource: null, amendmentNumber: null, amendsSection: null, bboxSource: null }, "never invented");
  assert.equal(retrieveMetaByChunk(null).size, 0);
});

// ---------------------------------------------------------------------------
// generalised compare teaser
// ---------------------------------------------------------------------------

const LEVELS = [
  { level_id: "level3_multimodal", order: 3, enabled: true },
  { level_id: "level1_naive", order: 1, enabled: true },
  { level_id: "level2_hybrid", order: 2, enabled: true },
  { level_id: "level4_agentic", order: 4, enabled: false },
];

test("lower enabled levels: strictly lower order, ascending, disabled excluded", () => {
  assert.deepEqual(lowerEnabledLevels(LEVELS, { order: 3 }).map((l) => l.engine), ["level1_naive", "level2_hybrid"]);
  assert.deepEqual(lowerEnabledLevels(LEVELS, { order: 1 }), []);
  assert.deepEqual(lowerEnabledLevels(LEVELS, { order: 5 }).map((l) => l.short), ["Level 1", "Level 2", "Level 3"]);
  assert.equal(teaserTitle(lowerEnabledLevels(LEVELS, { order: 2 })), "Same question on Level 1");
  assert.equal(teaserTitle(lowerEnabledLevels(LEVELS, { order: 3 })), "Same question on Levels 1–2");
  assert.equal(teaserTitle([{ engine: "a", short: "Level 1", order: 1 }, { engine: "c", short: "Level 3", order: 3 }]), "Same question on Levels 1, 3");
});

test("teaser matrix: one baseline column per lower level, same question/pair lookups per level", () => {
  const catalog = {
    models: [],
    defaults: { synthesis_model: "s", judge_model: "j" },
    demo_mode: "replay",
    recorded_pairs: [
      { engine: "level1_naive", synthesis_model: "s", judge_model: "j", question_ids: ["q_vpc_failover_diagram"] },
      { engine: "level2_hybrid", synthesis_model: "s", judge_model: "j", question_ids: ["q_other"] },
    ],
  } as ModelsResponse;
  const lb: LeaderboardResponse = {
    groups: [
      { synthesis_model: "s", judge_model: "j", rows: [
        { engine: "level1_naive", runs: 8, avg_groundedness: 0.95, avg_qa_relevance: 0.69, avg_context_precision: 0.71, avg_latency_ms: 24600 },
        { engine: "level2_hybrid", runs: 8, avg_groundedness: 0.97, avg_qa_relevance: 0.84, avg_context_precision: 0.64, avg_latency_ms: 28600 },
      ] },
      { synthesis_model: "other", judge_model: "j", rows: [{ engine: "level2_hybrid", runs: 8, avg_groundedness: 0.1 }] },
    ],
  };
  const lower = lowerEnabledLevels(LEVELS, { order: 3 });
  const entries = baselinesFor(catalog, lb, lower, { synthesisModel: "s", judgeModel: "j", questionId: "q_vpc_failover_diagram" });
  assert.deepEqual(entries.map((e) => [e.level.engine, e.view.questionRecorded, e.view.runs]), [["level1_naive", true, 8], ["level2_hybrid", false, 8]]);
  const rows = teaserMatrix(entries, { groundedness: 1, qaRelevance: 0.95, contextPrecision: 0.85, latencyMs: 30000 });
  assert.deepEqual(rows.map((r) => r.key), ["groundedness", "qa_relevance", "context_precision", "latency"]);
  assert.deepEqual(rows[0].baselines, [0.95, 0.97]);
  assert.equal(rows[0].current, 1);
  assert.deepEqual(rows[3].baselines, [24600, 28600]);
  const unknownPair = baselinesFor(catalog, lb, lower, { synthesisModel: null, judgeModel: "j", questionId: "q" });
  assert.deepEqual(teaserMatrix(unknownPair, { groundedness: null, qaRelevance: null, contextPrecision: null, latencyMs: null })[0].baselines, [null, null]);
});

// ---------------------------------------------------------------------------
// DEV fixture + safe mode
// ---------------------------------------------------------------------------

test("Level 3 DEV fixture: every frame flagged synthetic, stages complete, diagram citation from Gemini vision", () => {
  assert.ok(LEVEL3_DEV_FIXTURE.every((f) => (f.event as unknown as { synthetic_dev_fixture?: boolean }).synthetic_dev_fixture === true));
  const state = reduceStreamEvents(LEVEL3_DEV_FIXTURE.map((f) => f.event) as unknown as TimelineEvent[]);
  const tl = state.timelines.level3_multimodal;
  assert.ok(tl && state.complete);
  for (const s of ["query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate"]) assert.equal(tl.stages[s]?.status, "done", s);
  assert.deepEqual(parseElementIntent(tl.stages.query_prep?.payload ?? null), ["diagram", "table"]);
  assert.ok(parseSynthesizeL3(tl.stages.synthesize?.payload ?? null)?.authorityHeaders?.length);
  const diagram = tl.answer?.citations.find((c) => c.element_type === "diagram");
  assert.ok(diagram);
  assert.equal(citationRegion(diagram)?.label, "Gemini vision diagram · located on PDF text layer");
  assert.equal(transcriptionTitle(diagram), "Diagram transcription (Gemini vision)");
});

test("safe mode masks identities inside Level 3 payload strings and citations", () => {
  const health = { project_id: "acme-proj-42", gcs_bucket: "acme-proj-42-rag-arena-corpus" };
  const synth = maskDeep({ authority_headers: [{ chunk_id: "acme-proj-42_c", header: "gs://acme-proj-42-rag-arena-corpus/03.pdf · acme-proj-42", supersedes: ["projects/123456789"] }], element_mix: { table: 1 } }, health);
  const v = parseSynthesizeL3(synth);
  assert.equal(v?.authorityHeaders?.[0].header, `gs://${MASK_BUCKET}/03.pdf · ${MASK_PROJECT_ID}`);
  assert.deepEqual(v?.authorityHeaders?.[0].supersedes, [`projects/${MASK_PROJECT_NUMBER}`]);
  const failed = parseIngestParse(maskDeep({ vision_failed_pages: [{ doc_name: "03.pdf", page: 1, error: "403 on projects/123456789 bucket acme-proj-42-rag-arena-corpus" }] }, health));
  assert.equal(failed?.visionFailedPages[0].error, `403 on projects/${MASK_PROJECT_NUMBER} bucket ${MASK_BUCKET}`);
  const parse = parseIngestParse(maskDeep({ tables: 1, layout_source_counts: { "acme-proj-42/documentai_layout": 4 } }, health));
  assert.deepEqual(parse?.layoutSourceCounts, [[`${MASK_PROJECT_ID}/documentai_layout`, 4]], "keys masked too");
  const intent = parseElementIntent(maskDeep({ element_intent: ["projects/123456789/diagram"] }, health));
  assert.deepEqual(intent, [`projects/${MASK_PROJECT_NUMBER}/diagram`]);
  const citation: Citation = {
    chunk_id: "acme-proj-42_c1", doc_name: "03.pdf", solicitation_id: "solicitation_01", page_number: 1, section_id: null,
    element_type: "diagram", layout_source: "gemini_vision", bbox: BOX, excerpt: "Diagram rendered from gs://acme-proj-42-rag-arena-corpus by projects/987654321",
  };
  const masked = maskDeep(citation, health);
  assert.equal(masked.excerpt, `Diagram rendered from gs://${MASK_BUCKET} by projects/${MASK_PROJECT_NUMBER}`);
  assert.equal(masked.chunk_id, `${MASK_PROJECT_ID}_c1`);
  const retrieve = parseHybridRetrieve(maskDeep({ candidates: [{ ...L3_CANDIDATES[1], amends_section: "acme-proj-42 SEC-4.2.1" }] }, health));
  assert.equal(retrieve?.candidates[0].meta.amendsSection, `${MASK_PROJECT_ID} SEC-4.2.1`);
  assert.ok(!JSON.stringify([synth, masked]).includes("acme-proj-42"));
});
