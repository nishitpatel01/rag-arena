/**
 * Level 5 (Managed Vertex AI Search) helper tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: managed retrieve / synthesize parsers + mode detection, summary reference → citation linking, node summaries,
 * the picker rule for the `vertex-ai-search-summary` pseudo model, ingest gating for managed_vertex, managed ingest
 * stage order + counts, the compare-teaser baseline pair, safe-mode masking of data store / engine resource names,
 * and the DEV fixtures (both synthesize modes + the managed import job) through the shared reducer.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MANAGED_INGEST_STAGES,
  MANAGED_SKIPPED_STAGES,
  MAX_SEGMENTS_PER_RESULT,
  importErrorSamples,
  ingestStageOrder,
  managedIngestItems,
  managedIngestSummary,
  managedRetrieveSummary,
  managedSynthesizeSummary,
  parseManagedRetrieve,
  parseManagedSynthesize,
  referenceCitationIndex,
  resultPages,
  segmentCount,
  synthesizeModeLabel,
  topRelevance,
  truncateToken,
} from "../src/features/levels/level5Payloads";
import { parseHybridRetrieve, parseRerank, summarizeStage } from "../src/features/levels/stagePayloads";
import { parseSynthesizeL3 } from "../src/features/levels/level3Payloads";
import {
  MANAGED_SUMMARY_MODEL,
  allowedLevels,
  levelRestriction,
  loadSelection,
  optionStates,
  providerLabel,
  serializeSelection,
  validateSelection,
} from "../src/features/levels/modelSelection";
import { ingestPillText, isManagedLevel, managedStoreFor, runGateFor, tierCountFor } from "../src/features/ingest/ingestStatus";
import { baselinePair, lowerEnabledLevels, teaserTitle } from "../src/features/levels/compareTeaserModel";
import { MASK_PROJECT_ID, MASK_PROJECT_NUMBER, maskDeep, maskIdentity } from "../src/lib/safeMode";
import { INITIAL_STREAM_STATE, reduceStreamEvent } from "../src/features/levels/model";
import { LEVEL5_DEV_ARCHITECTURE, level5DevFixture, level5IngestFixture } from "../src/dev/fixtures/level5_run";
import type { BattleEvent, Citation, IngestStatusResponse, ModelCatalogEntry, ModelsResponse } from "../src/types/arena";

const RETRIEVE = {
  request: { serving_config: "projects/123456789012/locations/global/collections/default_collection/engines/e1/servingConfigs/default_search", query: "q" },
  total_size: 37,
  attribution_token: "abcdefghijklmnopqrstuvwxyz0123456789",
  api_ms: 412.4,
  results: [
    { document_id: "d1", doc_name: "a.pdf", title: "A", uri: "gs://b/a.pdf", extractive_segments: [{ content: "x", page_number: 3, relevance_score: 0.5 }, { content: "y", page_number: 1, relevance_score: 0.9 }] },
    { document_id: "d2", doc_name: "b.pdf", extractive_segments: [] },
    { doc_name: "no id" },
    "garbage",
  ],
};

test("parseManagedRetrieve: rows, pages, top score, invalid rows, token truncation", () => {
  const v = parseManagedRetrieve(RETRIEVE);
  assert.ok(v);
  assert.equal(v.results.length, 2);
  assert.equal(v.invalidRows, 2);
  assert.equal(v.totalSize, 37);
  assert.equal(v.apiMs, 412.4);
  assert.equal(v.results[0].rank, 1);
  assert.equal(resultPages(v.results[0]), "p.1, p.3");
  assert.equal(topRelevance(v.results[0]), 0.9);
  assert.equal(resultPages(v.results[1]), "—");
  assert.equal(topRelevance(v.results[1]), null);
  assert.equal(segmentCount(v), 2);
  assert.equal(truncateToken(v.attributionToken), "abcdefghij…456789 (36 chars)");
  assert.equal(truncateToken(null), "—");
  assert.equal(truncateToken("short"), "short");
  assert.ok(v.request);
  assert.equal(managedRetrieveSummary(v), "results=2 · segments=2 · total=37 · 412 ms");
});

test("parseManagedRetrieve: never matches Level 1–3 retrieve or rerank payloads, bounds segments", () => {
  assert.equal(parseManagedRetrieve(null), null);
  assert.equal(parseManagedRetrieve({ rows_returned: 5 }), null);
  assert.equal(parseManagedRetrieve({ candidates: [], results: [] }), null);
  // L2 rerank-like `results` without managed markers is not a managed retrieve.
  assert.equal(parseManagedRetrieve({ results: [{ chunk_id: "c", rrf_rank: 1, rerank_rank: 1, rerank_score: 0.5 }] }), null);
  // …and the managed payload does not parse as hybrid retrieve / rerank.
  assert.equal(parseHybridRetrieve(RETRIEVE), null);
  const many = Array.from({ length: MAX_SEGMENTS_PER_RESULT + 3 }, (_, i) => ({ content: `s${i}`, page_number: 1, relevance_score: 0.1 }));
  const v = parseManagedRetrieve({ total_size: 1, results: [{ document_id: "d", extractive_segments: [...many, { page_number: 2 }] }] });
  assert.ok(v);
  assert.equal(v.results[0].segments.length, MAX_SEGMENTS_PER_RESULT);
  assert.equal(v.results[0].invalidSegments, 4);
  assert.equal(parseRerank({ results: [] })?.results.length, 0); // rerank parser unaffected
});

test("parseManagedSynthesize: summary mode, references, skipped reasons verbatim", () => {
  const v = parseManagedSynthesize({
    mode: "vertex_ai_search_summary",
    summary: "Answer [1] [2]",
    references: [{ document_id: "d1", doc_name: "a.pdf", page_number: 3 }, { doc_name: "b.pdf" }, { page_number: 2 }, 7],
    model_version: "stable",
    summary_skipped_reasons: ["OUT_OF_DOMAIN_QUERY_IGNORED", "", 4],
  });
  assert.ok(v);
  assert.equal(v.mode, "vertex_ai_search_summary");
  assert.equal(v.summary, "Answer [1] [2]");
  assert.deepEqual(v.references.map((r) => [r.index, r.docName, r.pageNumber]), [[1, "a.pdf", 3], [2, "b.pdf", null]]);
  assert.equal(v.invalidReferences, 2);
  assert.deepEqual(v.skippedReasons, ["OUT_OF_DOMAIN_QUERY_IGNORED"]);
  assert.equal(v.modelVersion, "stable");
  assert.equal(synthesizeModeLabel(v), "Vertex AI Search summary");
  assert.equal(managedSynthesizeSummary(v), "managed summary · refs=2 · skipped: 1");
});

test("parseManagedSynthesize: model_over_segments mode, inference, unknown mode verbatim, L1–3 untouched", () => {
  const m = parseManagedSynthesize({ mode: "model_over_segments", model: "gemini-4-argon", prompt_tokens: 10, output_tokens: 2 });
  assert.ok(m);
  assert.equal(m.mode, "model_over_segments");
  assert.equal(synthesizeModeLabel(m, (id) => (id === "gemini-4-argon" ? "Gemini 4 Argon" : id)), "Gemini 4 Argon over extractive segments");
  assert.equal(managedSynthesizeSummary(m), "gemini-4-argon · over segments");
  // No mode key but managed-summary keys → summary mode inferred.
  assert.equal(parseManagedSynthesize({ summary: "s", summary_skipped_reasons: [] })?.mode, "vertex_ai_search_summary");
  const odd = parseManagedSynthesize({ mode: "something_new" });
  assert.equal(odd?.mode, null);
  assert.equal(odd ? synthesizeModeLabel(odd) : null, "mode: something_new");
  // Level 1–3 synthesize payloads are not Level 5.
  assert.equal(parseManagedSynthesize({ model: "gemini-4-argon", prompt_tokens: 1, output_tokens: 1 }), null);
  assert.equal(parseManagedSynthesize({ authority_headers: [], element_mix: { table: 1 } }), null);
  assert.ok(parseSynthesizeL3({ authority_headers: [], element_mix: { table: 1 } }));
});

test("summarizeStage dispatches Level 5 retrieve / synthesize / ingest summaries", () => {
  assert.equal(summarizeStage("retrieve", RETRIEVE), "results=2 · segments=2 · total=37 · 412 ms");
  assert.equal(summarizeStage("synthesize", { mode: "vertex_ai_search_summary", summary: "s", references: [{ doc_name: "a" }] }), "managed summary · refs=1");
  assert.equal(summarizeStage("upload_gcs", { uploaded: 3, skipped_existing: 42 }), "uploaded=3 · unchanged=42");
  assert.equal(summarizeStage("import_documents", { success_count: 45, failure_count: 0 }), "success=45 · failed=0");
  assert.equal(summarizeStage("verify_counts", { document_count: 45 }), "documents=45");
});

const cit = (doc: string, page: number, chunk = `${doc}#${page}`): Citation => ({
  chunk_id: chunk, doc_name: doc, solicitation_id: "s", page_number: page, section_id: null, element_type: "narrative_text",
  bbox: { page, x0: 0, y0: 0, x1: 1, y1: 1 }, excerpt: "e", bbox_source: "page",
});

test("referenceCitationIndex: same doc+page first, then same doc, else -1", () => {
  const cits = [cit("a.pdf", 1), cit("b.pdf", 2), cit("a.pdf", 3)];
  assert.equal(referenceCitationIndex({ index: 1, documentId: null, docName: "a.pdf", pageNumber: 3 }, cits), 2);
  assert.equal(referenceCitationIndex({ index: 2, documentId: null, docName: "a.pdf", pageNumber: 9 }, cits), 0);
  assert.equal(referenceCitationIndex({ index: 3, documentId: null, docName: "a.pdf", pageNumber: null }, cits), 0);
  assert.equal(referenceCitationIndex({ index: 4, documentId: null, docName: "zzz.pdf", pageNumber: 1 }, cits), -1);
  assert.equal(referenceCitationIndex({ index: 5, documentId: "b.pdf#2", docName: null, pageNumber: null }, cits), 1);
});

const entry = (id: string, provider: string, roles: string[], extra: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry => ({
  id, label: id, provider, location: "global", roles, input_usd_per_1m: null, output_usd_per_1m: null, price_source: null,
  price_as_of: null, available: true, unavailable_reason: null, verified_at: null, ...extra,
});

const CATALOG: ModelsResponse = {
  models: [
    entry("gemini-4-argon", "vertex_gemini", ["synthesis", "judge"]),
    entry("claude-opus-5-5", "anthropic_vertex", ["synthesis", "judge"]),
    entry(MANAGED_SUMMARY_MODEL, "vertex_ai_search", ["synthesis"]),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "replay",
  recorded_pairs: [],
};

test("picker rule: vertex-ai-search-summary is synthesis-only and valid only on managed_vertex", () => {
  const summary = CATALOG.models[2];
  assert.deepEqual(allowedLevels(summary), ["managed_vertex"]);
  assert.equal(allowedLevels(CATALOG.models[0]), null);
  assert.equal(levelRestriction(summary, "managed_vertex"), null);
  assert.match(levelRestriction(summary, "level1_naive") ?? "", /valid only on Level 5/);
  assert.equal(levelRestriction(summary, null), null);
  // Server-declared `levels` wins over the id rule.
  assert.match(levelRestriction(entry("x", "vertex_gemini", ["synthesis"], { levels: ["level2_hybrid"] }), "level1_naive") ?? "", /valid only on level2_hybrid/);
  assert.equal(providerLabel("vertex_ai_search"), "Vertex AI Search (managed)");

  const l1 = optionStates(CATALOG, "synthesis", "claude-opus-5-5", "level1_naive").find((o) => o.entry.id === MANAGED_SUMMARY_MODEL);
  assert.ok(l1 && l1.disabled && l1.restricted);
  const l5 = optionStates(CATALOG, "synthesis", "claude-opus-5-5", "managed_vertex").find((o) => o.entry.id === MANAGED_SUMMARY_MODEL);
  assert.ok(l5 && !l5.disabled);
  assert.equal(optionStates(CATALOG, "judge", null, "managed_vertex").some((o) => o.entry.id === MANAGED_SUMMARY_MODEL), false);

  const sel = { synthesis: MANAGED_SUMMARY_MODEL, judge: "claude-opus-5-5" };
  assert.equal(validateSelection(CATALOG, sel, "managed_vertex"), null);
  assert.match(validateSelection(CATALOG, sel, "level2_hybrid") ?? "", /cannot run on this level/);
  // A stored Level 5 selection falls back to defaults on Level 1 but is kept on Level 5.
  assert.deepEqual(loadSelection(CATALOG, serializeSelection(sel), "level1_naive"), { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" });
  assert.deepEqual(loadSelection(CATALOG, serializeSelection(sel), "managed_vertex"), sel);
});

const status = (extra: Partial<IngestStatusResponse>): IngestStatusResponse => ({ counts_by_tier: { naive: 10 }, index_states: {}, last_runs: [], ...extra });

test("ingest gating for managed_vertex reads GET /ingest/status → managed_vertex", () => {
  assert.equal(isManagedLevel("managed_vertex"), true);
  assert.equal(isManagedLevel("level1_naive"), false);
  // Not reported → info, not blocked.
  const none = runGateFor("managed_vertex", status({}), null);
  assert.equal(none.blocked, false);
  assert.equal(none.count, null);
  assert.match(none.note ?? "", /not reported/);
  // Reported with documents.
  const ok = status({ managed_vertex: { document_count: 45, data_store_state: "ACTIVE" } });
  assert.deepEqual(managedStoreFor(ok), { documents: 45, state: "ACTIVE", reason: null });
  assert.equal(tierCountFor("managed_vertex", ok), 45);
  const g = runGateFor("managed_vertex", ok, null);
  assert.deepEqual([g.blocked, g.count, g.tone], [false, 45, "ok"]);
  assert.equal(ingestPillText("managed_vertex", g, ok), "data store: 45 documents · ACTIVE");
  // Zero documents → blocked.
  const zero = status({ managed_vertex: { document_count: 0, data_store_state: "ACTIVE" } });
  assert.equal(runGateFor("managed_vertex", zero, null).blocked, true);
  // Unavailable → not blocked, reason shown.
  const un = status({ managed_vertex: { available: false, reason: "replay mode: no Discovery Engine calls" } });
  const gu = runGateFor("managed_vertex", un, null);
  assert.deepEqual([gu.blocked, gu.note], [false, "replay mode: no Discovery Engine calls"]);
  // Legacy numeric counts_by_tier.managed_vertex still honoured.
  assert.equal(tierCountFor("managed_vertex", status({ counts_by_tier: { managed_vertex: 7 } })), 7);
  // BigQuery levels unchanged.
  assert.equal(ingestPillText("level1_naive", runGateFor("level1_naive", ok, null), ok), "naive tier: 10 chunks");
  assert.equal(runGateFor("level2_hybrid", ok, null).blocked, true);
});

test("managed ingest: stage order, alias-tolerant counts, error samples", () => {
  assert.deepEqual(ingestStageOrder("managed_vertex", ["discover_docs", "parse"], ["discover_docs", "upload_gcs", "custom_stage"]), [
    ...MANAGED_INGEST_STAGES, ...MANAGED_SKIPPED_STAGES, "custom_stage",
  ]);
  assert.deepEqual(ingestStageOrder("level1_naive", ["discover_docs", "parse"], ["parse", "x"]), ["discover_docs", "parse", "x"]);
  assert.deepEqual(managedIngestItems("upload_gcs", { uploaded: 3, skipped_existing: 42, total: 45 }), [["uploaded", "3"], ["already in GCS", "42"], ["total files", "45"]]);
  assert.deepEqual(managedIngestItems("import_documents", { imported: 45, failures: 1, operation: "op/1" }), [["imported (success)", "45"], ["failed", "1"], ["operation", "op/1"]]);
  assert.deepEqual(managedIngestItems("verify_counts", { document_count: 45, data_store_state: "ACTIVE" }), [["documents in data store", "45"], ["data store state", "ACTIVE"]]);
  assert.deepEqual(managedIngestItems("parse", { pages: 1 }), []);
  assert.deepEqual(managedIngestItems("discover_docs", { docs: 45 }), [["documents found", "45"]]);
  assert.equal(managedIngestSummary("upload_gcs", {}), null);
  assert.deepEqual(importErrorSamples({ error_samples: ["bad pdf", { message: "too large" }, { code: 3 }] }), ["bad pdf", "too large", '{"code":3}']);
  assert.deepEqual(importErrorSamples(null), []);
});

test("compare teaser on Level 5: covers L1–L4 and substitutes the default synthesis model for the managed summary", () => {
  const levels = ["level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex"].map((id, i) => ({ level_id: id, order: i + 1, enabled: true }));
  const lower = lowerEnabledLevels(levels, { order: 5 });
  assert.deepEqual(lower.map((l) => l.engine), ["level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic"]);
  assert.equal(teaserTitle(lower), "Same question on Levels 1–4");
  assert.deepEqual(baselinePair(CATALOG, MANAGED_SUMMARY_MODEL, "claude-opus-5-5"), { synthesisModel: "gemini-4-argon", judgeModel: "claude-opus-5-5", substituted: true });
  assert.deepEqual(baselinePair(CATALOG, "gemini-4-argon", "claude-opus-5-5"), { synthesisModel: "gemini-4-argon", judgeModel: "claude-opus-5-5", substituted: false });
  assert.deepEqual(baselinePair(null, MANAGED_SUMMARY_MODEL, "j"), { synthesisModel: MANAGED_SUMMARY_MODEL, judgeModel: "j", substituted: false });
});

test("safe mode masks data store / engine resource names (project number and project id)", () => {
  const health = { project_id: "acme-proj-42", gcs_bucket: "acme-proj-42-rag-arena-corpus" };
  const ds = "projects/987654321098/locations/global/collections/default_collection/dataStores/rag-arena-ds";
  assert.equal(maskIdentity(ds, health), `projects/${MASK_PROJECT_NUMBER}/locations/global/collections/default_collection/dataStores/rag-arena-ds`);
  const byId = "projects/acme-proj-42/locations/global/collections/default_collection/engines/rag-arena-engine/servingConfigs/default_search";
  assert.equal(maskIdentity(byId, health), `projects/${MASK_PROJECT_ID}/locations/global/collections/default_collection/engines/rag-arena-engine/servingConfigs/default_search`);
  const masked = maskDeep({ request: { serving_config: ds }, results: [{ uri: "gs://acme-proj-42-rag-arena-corpus/sample_docs/a.pdf" }] }, health);
  const text = JSON.stringify(masked);
  assert.equal(/projects\/\d{6,}/.test(text), false);
  assert.equal(text.includes("acme-proj-42"), false);
  // Status entry (data store name) masked too.
  const st = maskDeep(status({ managed_vertex: { data_store: ds, document_count: 1 } }), health);
  assert.equal(st.managed_vertex?.data_store?.includes("987654321098"), false);
});

function playBattle(events: readonly BattleEvent[]) {
  return events.reduce((s, e) => reduceStreamEvent(s, e), INITIAL_STREAM_STATE);
}

test("DEV fixture: both synthesize modes reduce to a complete managed_vertex timeline", () => {
  for (const mode of ["summary", "model"] as const) {
    const fx = level5DevFixture(mode);
    assert.ok(fx.every((f) => f.event.synthetic_dev_fixture === true));
    const state = playBattle(fx.map((f) => f.event));
    const tl = state.timelines.managed_vertex;
    assert.ok(tl, mode);
    assert.equal(state.complete, true);
    assert.deepEqual(tl.stageOrder, ["query_prep", "retrieve", "synthesize", "evaluate"]);
    const r = parseManagedRetrieve(tl.stages.retrieve.payload);
    assert.ok(r && r.results.length === 4 && segmentCount(r) === 6);
    const s = parseManagedSynthesize(tl.stages.synthesize.payload);
    assert.equal(s?.mode, mode === "summary" ? "vertex_ai_search_summary" : "model_over_segments");
    assert.ok(tl.answer && tl.answer.citations.every((c) => c.bbox_source === "page"));
    assert.equal(state.synthesisModel, mode === "summary" ? MANAGED_SUMMARY_MODEL : "gemini-4-argon");
  }
  // Every query stage maps to ≥ 1 descriptor node; ingest stages too.
  for (const stage of ["query_prep", "retrieve", "synthesize", "evaluate"]) assert.ok(LEVEL5_DEV_ARCHITECTURE.nodes.some((n) => n.stage_ids.includes(stage)), stage);
  for (const stage of [...MANAGED_INGEST_STAGES, ...MANAGED_SKIPPED_STAGES]) assert.ok(LEVEL5_DEV_ARCHITECTURE.ingest_nodes.some((n) => n.stage_ids.includes(stage)), stage);
});

test("DEV ingest fixture: managed stages done, BigQuery stages skipped with the verbatim reason", () => {
  const state = level5IngestFixture().reduce((s, f) => reduceStreamEvent(s, f.event as unknown as BattleEvent), INITIAL_STREAM_STATE);
  const tl = state.timelines.managed_vertex;
  assert.ok(tl);
  for (const s of MANAGED_INGEST_STAGES) assert.equal(tl.stages[s]?.status, "done", s);
  for (const s of MANAGED_SKIPPED_STAGES) {
    assert.equal(tl.stages[s]?.status, "skipped", s);
    assert.equal(tl.stages[s]?.skipReason, "managed by Vertex AI Search");
  }
  assert.equal(state.complete, true);
});

test("backend shape (in-progress contract): api_request, row rank, summary payload without mode, raw API references", () => {
  const r = parseManagedRetrieve({
    api_request: { query: "q", pageSize: 10 }, total_size: 12, rows_returned: 1, segments_returned: 1, attempts: 1,
    results: [{ rank: 3, document_id: "abc", doc_name: "x.pdf", uri: "${GCS_CORPUS_BUCKET}/x.pdf", snippets: [], extractive_segments: [{ content: "c", page_number: 2, relevance_score: null }] }],
  });
  assert.ok(r);
  assert.deepEqual(r.request, { query: "q", pageSize: 10 });
  assert.equal(r.results[0].rank, 3);
  assert.equal(topRelevance(r.results[0]), null);

  const s = parseManagedSynthesize({
    model: MANAGED_SUMMARY_MODEL, provider: "vertex_ai_search", answer: "Regions are us-central1 [1].", generated_in_stage: "retrieve",
    model_version_requested: "stable", model_version_used: "not returned by the API", summary_skipped_reasons: [],
    references: [{ title: "Technical Exhibit", document: "projects/123456789012/locations/global/collections/default_collection/dataStores/ds/branches/0/documents/abc", uri: "gs://bkt/sample_docs/03_Technical_Exhibit_Architecture.pdf", chunkContents: [{ content: "x", pageIdentifier: "4" }] }],
    citation_source: "summary_citations", prompt_tokens: 0, output_tokens: 0,
  });
  assert.ok(s);
  assert.equal(s.mode, "vertex_ai_search_summary");
  assert.equal(s.summary, "Regions are us-central1 [1].");
  assert.equal(s.modelVersionRequested, "stable");
  assert.equal(s.modelVersion, "not returned by the API");
  assert.deepEqual(s.references.map((x) => [x.documentId, x.docName, x.pageNumber]), [["abc", "03_Technical_Exhibit_Architecture.pdf", 4]]);
  assert.equal(referenceCitationIndex(s.references[0], [cit("03_Technical_Exhibit_Architecture.pdf", 1), cit("03_Technical_Exhibit_Architecture.pdf", 4)]), 1);

  const m = parseManagedSynthesize({ model: "gemini-4-argon", prompt_tokens: 900, output_tokens: 80, context_source: "Vertex AI Search extractive segments", context_segments: 12 });
  assert.equal(m?.mode, "model_over_segments");
});

test("catalog: allowed_levels and request pricing from the backend", async () => {
  const { formatModelPrice } = await import("../src/features/levels/modelSelection");
  const e = entry(MANAGED_SUMMARY_MODEL, "vertex_ai_search", ["synthesis"], { allowed_levels: ["managed_vertex"], usd_per_1k_requests: 4, input_usd_per_1m: 0, output_usd_per_1m: 0, price_as_of: "2026-10-23" });
  assert.deepEqual(allowedLevels(e), ["managed_vertex"]);
  assert.equal(formatModelPrice(e), "$4 per 1,000 requests · as of 2026-10-23");
  assert.equal(allowedLevels(entry("gemini-4-argon", "vertex_gemini", ["synthesis"], { allowed_levels: [] })), null);
});

test("final contract: reference title without .pdf links to the citation by file stem", () => {
  const s = parseManagedSynthesize({ provider: "vertex_ai_search", answer: "a [1]", references: [{ title: "03_Technical_Exhibit_Architecture", document_id: "46e3", uri: "" }] });
  assert.ok(s);
  assert.deepEqual(s.references.map((r) => [r.documentId, r.docName]), [["46e3", "03_Technical_Exhibit_Architecture"]]);
  assert.equal(referenceCitationIndex(s.references[0], [cit("01_RFP_Base_Solicitation.pdf", 1), cit("03_Technical_Exhibit_Architecture.pdf", 1)]), 1);
});
