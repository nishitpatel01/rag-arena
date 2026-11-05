/**
 * Level 6 (Managed Vertex AI RAG Engine, `rag_engine`) helper tests — run with `npm test`.
 * Covers: RetrieveContexts & VertexRagStore / model-over-contexts parsers, stage summaries,
 * model-picker restriction for `vertex-rag-engine-grounded`, 6-level `/compare` choices,
 * `/ingest/status` -> `rag_engine` run gating & pills, managed ingest stage order + RagCorpus
 * count aliases, upload workspace level wiring, layout source labels, and safe-mode masking.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  GROUNDED_MODE,
  MAX_RAG_CONTEXTS,
  MODEL_OVER_CONTEXTS_MODE,
  contextPageLabel,
  parseRagEngineRetrieve,
  parseRagEngineSynthesize,
  ragEngineRetrieveSummary,
  ragEngineSynthesizeSummary,
  ragSynthesizeModeLabel,
} from "../src/features/levels/level6Payloads";
import { ingestStageOrder, managedIngestItems, managedIngestSummary } from "../src/features/levels/level5Payloads";
import { parseManagedRetrieve, parseManagedSynthesize } from "../src/features/levels/level5Payloads";
import { parseHybridRetrieve, summarizeStage } from "../src/features/levels/stagePayloads";
import {
  MANAGED_SUMMARY_MODEL,
  RAG_ENGINE_GROUNDED_MODEL,
  RAG_ENGINE_LEVEL,
  allowedLevels,
  levelRestriction,
  optionStates,
  providerLabel,
  restrictedOptionSuffix,
  validateSelection,
} from "../src/features/levels/modelSelection";
import { ingestPillText, isManagedLevel, ragCorpusFor, runGateFor, tierCountFor } from "../src/features/ingest/ingestStatus";
import { baselinePair, lowerEnabledLevels, teaserTitle } from "../src/features/levels/compareTeaserModel";
import { levelChoices } from "../src/features/compare/compareModel";
import { UPLOAD_INGEST_LEVELS, dataLevelFor, isWorkspaceLevelReady, uploadRunGateFor } from "../src/features/uploads/uploadModel";
import { isLayoutSource, layoutSourceLabel } from "../src/lib/elements";
import { LEVEL_IDS, type IngestStatusResponse, type LevelDescriptor, type ModelCatalogEntry, type ModelsResponse } from "../src/types/arena";
import { MASK_BUCKET, MASK_PROJECT_ID, MASK_PROJECT_NUMBER, maskDeep } from "../src/lib/safeMode";

const RAG_RETRIEVE = {
  api_request: {
    vertexRagStore: {
      ragResources: [{ ragCorpus: "projects/${GCP_PROJECT_ID}/locations/us-central1/ragCorpora/${RAG_ENGINE_CORPUS_ID}" }],
    },
    query: { text: "What is CLIN 0001?", similarityTopK: 10 },
  },
  corpus: "rag-arena-procurement",
  location: "us-central1",
  embedding_model: "text-embedding-005",
  rows_returned: 2,
  api_ms: 184,
  attempts: 1,
  retrieval_api_fee_usd: 0,
  solicitation_filter: "all corpus files",
  contexts: [
    {
      rank: 1,
      chunk_id: "rag_solicitation_01_04_Pricing.pdf_01",
      doc_name: "04_Pricing_Schedule_CLINs.pdf",
      solicitation_id: "solicitation_01",
      uri: "gs://${GCS_CORPUS_BUCKET}/procurement_corpus/solicitation_01_cloud/04_Pricing_Schedule_CLINs.pdf",
      page_number: 2,
      last_page: 3,
      page_source: "page_span",
      score: 0.8123,
      text: "CLIN 0001 Cloud Hosting Base Year: $120,000.00",
    },
    {
      rank: 2,
      chunk_id: "rag_solicitation_01_01_RFP.pdf_02",
      doc_name: "01_RFP_Cover.pdf",
      solicitation_id: "solicitation_01",
      uri: "gs://${GCS_CORPUS_BUCKET}/procurement_corpus/solicitation_01_cloud/01_RFP_Cover.pdf",
      page_number: 1,
      last_page: 1,
      page_source: "fallback_page_1",
      score: 0.654,
      text: "Cover page instructions.",
    },
    { doc_name: "missing_text_and_id" },
    null,
  ],
};

test("LEVEL_IDS and layoutSource include Level 6 rag_engine", () => {
  assert.equal(LEVEL_IDS.length, 8);
  assert.equal(LEVEL_IDS[5], "rag_engine");
  assert.equal(isLayoutSource("vertex_ai_rag_engine"), true);
  assert.equal(layoutSourceLabel("vertex_ai_rag_engine"), "Vertex AI RAG Engine");
});

test("parseRagEngineRetrieve: parses contexts, page spans, bounds, and never collides with L1–L5", () => {
  const v = parseRagEngineRetrieve(RAG_RETRIEVE);
  assert.ok(v);
  assert.equal(v.contexts.length, 2);
  assert.equal(v.invalidRows, 2);
  assert.equal(v.corpus, "rag-arena-procurement");
  assert.equal(v.location, "us-central1");
  assert.equal(v.embeddingModel, "text-embedding-005");
  assert.equal(v.apiMs, 184);
  assert.equal(v.retrievalApiFeeUsd, 0);
  assert.equal(contextPageLabel(v.contexts[0]), "p.2–3");
  assert.equal(contextPageLabel(v.contexts[1]), "p.1");
  assert.equal(ragEngineRetrieveSummary(v), "contexts=2 · us-central1 · 184 ms");
  assert.equal(summarizeStage("retrieve", RAG_RETRIEVE), "contexts=2 · us-central1 · 184 ms");

  // Disjoint from L2/L3 hybrid retrieve and L5 managed retrieve
  assert.equal(parseHybridRetrieve(RAG_RETRIEVE), null);
  assert.equal(parseManagedRetrieve(RAG_RETRIEVE), null);
  assert.equal(parseRagEngineRetrieve(null), null);
  assert.equal(parseRagEngineRetrieve({ candidates: [] }), null);
  assert.equal(parseRagEngineRetrieve({ results: [], total_size: 1 }), null);

  // Explicit bound on context rows
  const many = Array.from({ length: MAX_RAG_CONTEXTS + 4 }, (_, i) => ({
    chunk_id: `c_${i}`,
    doc_name: "a.pdf",
    text: `chunk ${i}`,
    page_number: 1,
  }));
  const capped = parseRagEngineRetrieve({ contexts: many });
  assert.ok(capped);
  assert.equal(capped.contexts.length, MAX_RAG_CONTEXTS);
  assert.equal(capped.invalidRows, 4);
});

test("parseRagEngineSynthesize: distinguishes VertexRagStore grounded vs catalog model over RagCorpus contexts", () => {
  const grounded = parseRagEngineSynthesize({
    model: "vertex-rag-engine-grounded",
    underlying_gemini_model: "gemini-3.8-flash",
    provider: "vertex_rag_engine",
    corpus: "rag-arena-procurement",
    thinking_level: "MEDIUM",
    prompt_tokens: 420,
    output_tokens: 95,
    thinking_tokens: 40,
    grounding_chunks_count: 5,
  });
  assert.ok(grounded);
  assert.equal(grounded.mode, GROUNDED_MODE);
  assert.equal(grounded.groundingChunksCount, 5);
  const labelFor = (id: string | null) => (id === "gemini-3.8-flash" ? "Gemini 3.8 Flash" : id === "gemini-4-argon" ? "Gemini 4 Argon" : id);
  assert.equal(ragSynthesizeModeLabel(grounded, labelFor), "VertexRagStore grounded (Gemini 3.8 Flash)");
  assert.equal(ragEngineSynthesizeSummary(grounded), "VertexRagStore · grounding_chunks=5");
  assert.equal(summarizeStage("synthesize", { provider: "vertex_rag_engine", grounding_chunks_count: 3 }), "VertexRagStore · grounding_chunks=3");

  const overContexts = parseRagEngineSynthesize({
    model: "gemini-4-argon",
    provider: "vertex_gemini",
    context_source: "Vertex AI RAG Engine RetrieveContexts",
    context_chunks: 10,
    corpus: "rag-arena-procurement",
    prompt_tokens: 900,
    output_tokens: 120,
  });
  assert.ok(overContexts);
  assert.equal(overContexts.mode, MODEL_OVER_CONTEXTS_MODE);
  assert.equal(ragSynthesizeModeLabel(overContexts, labelFor), "Gemini 4 Argon over RagCorpus contexts");
  assert.equal(ragEngineSynthesizeSummary(overContexts), "gemini-4-argon · contexts=10");

  // Disjoint from Level 5 and Levels 1–3
  assert.equal(parseManagedSynthesize({ provider: "vertex_rag_engine", grounding_chunks_count: 2 }), null);
  assert.equal(parseRagEngineSynthesize({ mode: "vertex_ai_search_summary", summary: "x" }), null);
  assert.equal(parseRagEngineSynthesize({ model: "gemini-4-argon", prompt_tokens: 10, output_tokens: 5 }), null);
});

const mkEntry = (id: string, provider: string, roles: string[], extra: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry => ({
  id,
  label: id,
  provider,
  location: "us-central1",
  roles,
  input_usd_per_1m: 0.5,
  output_usd_per_1m: 3.0,
  price_source: null,
  price_as_of: null,
  available: true,
  unavailable_reason: null,
  verified_at: null,
  ...extra,
});

const CATALOG_6: ModelsResponse = {
  models: [
    mkEntry("gemini-4-argon", "vertex_gemini", ["synthesis", "judge"]),
    mkEntry("claude-opus-5-5", "anthropic_vertex", ["synthesis", "judge"]),
    mkEntry(MANAGED_SUMMARY_MODEL, "vertex_ai_search", ["synthesis"], { allowed_levels: ["managed_vertex"] }),
    mkEntry(RAG_ENGINE_GROUNDED_MODEL, "vertex_rag_engine", ["synthesis"], { allowed_levels: ["rag_engine"] }),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "live",
  recorded_pairs: [],
};

test("model picker & Compare rules: vertex-rag-engine-grounded is Level 6 only, and all 6 levels fit in Compare", () => {
  assert.equal(providerLabel("vertex_rag_engine"), "Vertex AI RAG Engine (grounded)");
  const grounded = CATALOG_6.models[3];
  const vais = CATALOG_6.models[2];
  assert.deepEqual(allowedLevels(grounded), [RAG_ENGINE_LEVEL]);
  assert.equal(restrictedOptionSuffix(grounded), " — Level 6 only");
  assert.equal(restrictedOptionSuffix(vais), " — Level 5 only");
  assert.equal(levelRestriction(grounded, "rag_engine"), null);
  assert.match(levelRestriction(grounded, "level1_naive") ?? "", /Level 6/);

  const onL6 = optionStates(CATALOG_6, "synthesis", "claude-opus-5-5", "rag_engine").find((o) => o.entry.id === RAG_ENGINE_GROUNDED_MODEL);
  assert.equal(onL6?.disabled, false);
  const onL5 = optionStates(CATALOG_6, "synthesis", "claude-opus-5-5", "managed_vertex").find((o) => o.entry.id === RAG_ENGINE_GROUNDED_MODEL);
  assert.equal(onL5?.disabled, true);
  assert.equal(onL5?.restricted, true);

  assert.equal(validateSelection(CATALOG_6, { synthesis: RAG_ENGINE_GROUNDED_MODEL, judge: "claude-opus-5-5" }, "rag_engine"), null);
  assert.match(validateSelection(CATALOG_6, { synthesis: RAG_ENGINE_GROUNDED_MODEL, judge: "claude-opus-5-5" }, "managed_vertex") ?? "", /Level 6/);

  // CompareTeaser baseline substitution works for Level 6 grounded model and lists Levels 1–5
  assert.deepEqual(baselinePair(CATALOG_6, RAG_ENGINE_GROUNDED_MODEL, "claude-opus-5-5"), {
    synthesisModel: "gemini-4-argon",
    judgeModel: "claude-opus-5-5",
    substituted: true,
  });
  const allLevels: LevelDescriptor[] = LEVEL_IDS.map((id, i) => ({
    level_id: id,
    label: `Level ${i + 1}`,
    order: i + 1,
    enabled: true,
    stages: [],
    architecture: { level_id: id, nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] },
  }));
  const lower = lowerEnabledLevels(allLevels, allLevels[5]);
  assert.equal(lower.length, 5);
  assert.equal(teaserTitle(lower), "Same question on Levels 1–5");

  // Compare page offers all 8 levels (including Level 6 rag_engine and Levels 7–8 ScaNN)
  const choices = levelChoices(allLevels, CATALOG_6, { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" }, "q1");
  assert.equal(choices.length, 8);
  assert.equal(choices[5].levelId, "rag_engine");
  assert.equal(choices[5].disabledReason, null);
});

const mkStatus = (patch: Partial<IngestStatusResponse>): IngestStatusResponse => ({
  counts_by_tier: {},
  index_states: {},
  last_runs: [],
  ...patch,
});

test("ingest gating & stage aliases for rag_engine read status.rag_engine and RagCorpus file counts", () => {
  assert.equal(isManagedLevel("rag_engine"), true);
  assert.deepEqual(ingestStageOrder("rag_engine", ["discover_docs", "parse"], []), [
    "discover_docs",
    "upload_gcs",
    "import_documents",
    "verify_counts",
    "parse",
    "chunk",
    "embed",
    "load_bigquery",
    "build_indexes",
  ]);

  const ready = mkStatus({
    rag_engine: {
      available: true,
      corpus_state: "ACTIVE",
      db_mode: "spanner_basic",
      location: "us-central1",
      corpus_display_name: "rag-arena-procurement",
      file_count: 45,
      active_files: 45,
      embedding_model: "text-embedding-005",
      chunk_size: 512,
      chunk_overlap: 100,
    },
  });
  assert.equal(ragCorpusFor(ready)?.files, 45);
  assert.equal(tierCountFor("rag_engine", ready), 45);
  const gateReady = runGateFor("rag_engine", ready, null);
  assert.equal(gateReady.blocked, false);
  assert.equal(ingestPillText("rag_engine", gateReady, ready), "RagCorpus: 45 files · ACTIVE (spanner_basic)");

  const empty = mkStatus({ rag_engine: { available: true, corpus_state: "ACTIVE", file_count: 0, active_files: 0 } });
  assert.equal(runGateFor("rag_engine", empty, null).blocked, true);

  const notProv = mkStatus({ rag_engine: { available: true, corpus_state: "NOT_FOUND", file_count: 0, active_files: 0 } });
  const gateNotProv = runGateFor("rag_engine", notProv, null);
  assert.equal(gateNotProv.blocked, true);
  assert.match(gateNotProv.note ?? "", /provision_rag_engine\.py/);

  const replay = mkStatus({ rag_engine: { available: false, reason: "DEMO_MODE=replay: no GCP calls" } });
  assert.equal(runGateFor("rag_engine", replay, null).blocked, false);

  // Managed ingest stage items & summaries handle RagEngine field names
  const impItems = managedIngestItems("import_documents", { imported_count: 25, skipped_count: 20, failed_count: 0, batches: 2, polls: 4 });
  assert.deepEqual(impItems, [
    ["imported (success)", "25"],
    ["already imported (skipped)", "20"],
    ["failed", "0"],
    ["batches", "2"],
    ["polls", "4"],
  ]);
  assert.equal(managedIngestSummary("import_documents", { imported_count: 45, failed_count: 0 }), "success=45 · failed=0");
  assert.equal(managedIngestSummary("verify_counts", { file_count: 45, active_files: 45 }), "files=45");
});

test("upload workspace wiring & safe-mode masking cover rag_engine", () => {
  assert.equal(UPLOAD_INGEST_LEVELS.includes("rag_engine"), true);
  assert.equal(dataLevelFor("rag_engine"), "rag_engine");
  const ws = {
    id: "upl_0123456789ab",
    created_at: "2026-11-03T00:00:00Z",
    files: [{ id: "f_0123456789ab", name: "Pricing.pdf", status: "ready" as const, pages: 2 }],
    levels: { rag_engine: { status: "completed" as const, chunks: 1, tier_profile: "rag_engine" } },
  };
  assert.equal(isWorkspaceLevelReady(ws, "rag_engine"), true);
  const g = uploadRunGateFor("rag_engine", ws);
  assert.equal(g.blocked, false);
  assert.equal(g.count, 1);

  const raw = {
    uri: "gs://secret-corpus-bkt/procurement_corpus/a.pdf",
    corpus: "projects/12345/locations/us-central1/ragCorpora/999",
    project: "secret-proj",
  };
  const masked = maskDeep(raw, { project_id: "secret-proj", gcs_bucket: "secret-corpus-bkt", upload_bucket: "secret-upl-bkt" });
  assert.equal(masked.uri, `gs://${MASK_BUCKET}/procurement_corpus/a.pdf`);
  assert.equal(masked.corpus, `projects/${MASK_PROJECT_NUMBER}/locations/us-central1/ragCorpora/999`);
  assert.equal(masked.project, MASK_PROJECT_ID);
});
