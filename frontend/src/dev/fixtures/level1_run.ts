/**
 * DEV-ONLY fixture. Imported exclusively behind `if (import.meta.env.DEV)` so it tree-shakes out of
 * `npm run build`. Durations and payload numbers are placeholders that follow the SSE contract shape;
 * every frame is flagged `synthetic_dev_fixture: true` and the Shell renders a warning banner while
 * it plays. It exists so the stage rail / architecture diagram can be exercised without a backend.
 */

import type { BattleEvent } from "../../types/arena";

export interface TimedFixtureEvent {
  /** Milliseconds after the previous event at which this one is delivered. */
  readonly t_rel: number;
  readonly event: BattleEvent & { readonly synthetic_dev_fixture: true };
}

const RUN_ID = "devfixture-level1-0001";
const ENGINE = "level1_naive";
const T0 = Date.parse("2026-10-20T20:00:00.000Z");

function ts(offsetMs: number): string {
  return new Date(T0 + offsetMs).toISOString();
}

const SQL = [
  "SELECT base.chunk_id, base.doc_name, base.page_number, base.section_id, base.element_type,",
  "       base.bbox, base.content, distance",
  "FROM VECTOR_SEARCH(",
  "  (SELECT * FROM `@project.rag_arena.document_chunks` WHERE tier_profile = 'naive'),",
  "  'embedding', (SELECT @query_embedding AS embedding),",
  "  top_k => 5, distance_type => 'COSINE')",
  "ORDER BY distance ASC",
].join("\n");

const F = true as const;

export const LEVEL1_DEV_FIXTURE: readonly TimedFixtureEvent[] = [
  {
    t_rel: 0,
    event: { event: "run_started", run_id: RUN_ID, engines: [ENGINE], demo_mode: "dev_fixture", question_id: "q_exact_clin_lookup", synthesis_model: "gemini-3.8-flash", judge_model: "claude-opus-5-5", ts: ts(0), synthetic_dev_fixture: F },
  },
  {
    t_rel: 150,
    event: { event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "query_prep", label: "Query preparation", service: "engine (local)", request: { original_query: "What is the base hourly rate for CLIN-0004-AB?" }, ts: ts(150), synthetic_dev_fixture: F },
  },
  {
    t_rel: 400,
    event: { event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "query_prep", duration_ms: 3, payload: { original_query: "What is the base hourly rate for CLIN-0004-AB?", prepared_query: "What is the base hourly rate for CLIN-0004-AB?", expansions: [] }, ts: ts(550), synthetic_dev_fixture: F },
  },
  {
    t_rel: 100,
    event: { event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "embed_query", label: "Embed query", service: "Vertex AI · gemini-embedding-2", request: { model: "gemini-embedding-2", task_type: "RETRIEVAL_QUERY", text_chars: 46 }, ts: ts(650), synthetic_dev_fixture: F },
  },
  {
    t_rel: 1800,
    event: { event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "embed_query", duration_ms: 1760, payload: { model: "gemini-embedding-2", dimensions: 3072, token_count: 14 }, ts: ts(2450), synthetic_dev_fixture: F },
  },
  {
    t_rel: 80,
    event: { event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "retrieve", label: "Vector search", service: "BigQuery VECTOR_SEARCH", request: { sql: SQL, params: { top_k: 5, tier_profile: "naive" } }, ts: ts(2530), synthetic_dev_fixture: F },
  },
  {
    t_rel: 2600,
    event: { event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "retrieve", duration_ms: 2540, payload: { sql: SQL, rows_returned: 5, top_k: 5, scores: [0.81, 0.77, 0.74, 0.7, 0.66], bytes_processed: 13_041_664 }, ts: ts(5070), synthetic_dev_fixture: F },
  },
  {
    t_rel: 60,
    event: { event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "synthesize", label: "Answer synthesis", service: "Vertex AI · Gemini 3.8 Flash (dev fixture)", request: { model: "gemini-3.8-flash", thinking_level: "high", context_chunks: 5 }, ts: ts(5130), synthetic_dev_fixture: F },
  },
  {
    t_rel: 3200,
    event: { event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "synthesize", duration_ms: 3150, payload: { model: "gemini-3.8-flash", thinking_level: "high", prompt_tokens: 1842, output_tokens: 96 }, ts: ts(8280), synthetic_dev_fixture: F },
  },
  {
    t_rel: 50,
    event: {
      event: "answer_complete", run_id: RUN_ID, engine: ENGINE,
      answer: "[DEV FIXTURE] CLIN-0004-AB (Cloud Security Architect Labor) has a base rate of $210.00/hr for 2,000 hours in Year 1.",
      citations: [
        { chunk_id: "devfix-chunk-01", doc_name: "04_Pricing_Schedule_CLINs.pdf", solicitation_id: "solicitation_01", page_number: 2, section_id: "CLIN-0004-AB", element_type: "table", bbox: { page: 2, x0: 0.08, y0: 0.31, x1: 0.92, y1: 0.47 }, excerpt: "CLIN-0004-AB | Cloud Security Architect Labor | 2,000 hrs | $210.00/hr | $420,000.00" },
        { chunk_id: "devfix-chunk-02", doc_name: "04_Pricing_Schedule_CLINs.pdf", solicitation_id: "solicitation_01", page_number: 1, section_id: "1.2", element_type: "narrative_text", bbox: { page: 1, x0: 0.1, y0: 0.55, x1: 0.9, y1: 0.63 }, excerpt: "Rates are firm-fixed for the base period unless superseded by amendment." },
      ],
      latency_ms: 7480, cost_usd: 0.00231,
      usage: { prompt_tokens: 1842, output_tokens: 96, embedding_tokens: 14, model: "gemini-3.8-flash" },
      flags: { reranker_used: false, adk_runner_used: false, layout_source: "pymupdf" },
      ts: ts(8330), synthetic_dev_fixture: F,
    },
  },
  {
    t_rel: 100,
    event: { event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "evaluate", label: "LLM-as-a-judge", service: "Vertex AI · Claude Opus 5.5 (dev fixture)", request: { judge_model: "claude-opus-5-5", max_tokens: 1024 }, ts: ts(8430), synthetic_dev_fixture: F },
  },
  {
    t_rel: 2400,
    event: { event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "evaluate", duration_ms: 2370, payload: { judge_model: "claude-opus-5-5", groundedness: 0.5, qa_relevance: 0.5, context_precision: 0.5, reference_answer_used: true }, ts: ts(10800), synthetic_dev_fixture: F },
  },
  {
    t_rel: 20,
    event: { event: "eval_complete", run_id: RUN_ID, engine: ENGINE, metrics: { groundedness: 0.5, qa_relevance: 0.5, context_precision: 0.5 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 2370, ts: ts(10820), synthetic_dev_fixture: F },
  },
  {
    t_rel: 20,
    event: { event: "battle_complete", run_id: RUN_ID, engines_completed: [ENGINE], engines_failed: [], ts: ts(10840), synthetic_dev_fixture: F },
  },
];

/** Minimal architecture descriptor for the fixture, used only when `GET /api/v1/levels` is unreachable in dev. */
export const LEVEL1_DEV_ARCHITECTURE = {
  level_id: "level1_naive" as const,
  nodes: [
    { id: "user", label: "User question", service: "Browser", kind: "actor" as const, col: 0, row: 1, stage_ids: ["query_prep"] },
    { id: "embed", label: "Query embedding", service: "Vertex AI · gemini-embedding-2", kind: "model" as const, col: 1, row: 1, stage_ids: ["embed_query"] },
    { id: "bq", label: "document_chunks", service: "BigQuery VECTOR_SEARCH", kind: "store" as const, col: 2, row: 1, stage_ids: ["retrieve"] },
    { id: "llm", label: "Answer synthesis", service: "Vertex AI · gemini-4-argon", kind: "model" as const, col: 3, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI · gemini-4-argon", kind: "model" as const, col: 4, row: 1, stage_ids: ["evaluate"] },
  ],
  edges: [
    { from: "user", to: "embed", label: "text" },
    { from: "embed", to: "bq", label: "3072-d vector" },
    { from: "bq", to: "llm", label: "top-5 chunks" },
    { from: "llm", to: "judge", label: "answer + context" },
  ],
  ingest_nodes: [
    { id: "gcs", label: "GCS corpus", service: "Cloud Storage", kind: "store" as const, col: 0, row: 0, stage_ids: ["discover_docs"] },
    { id: "parse", label: "Parser", service: "PyMuPDF", kind: "compute" as const, col: 1, row: 0, stage_ids: ["parse"] },
    { id: "chunk", label: "Chunker 360/60", service: "engine", kind: "compute" as const, col: 2, row: 0, stage_ids: ["chunk"] },
    { id: "embed", label: "Embeddings", service: "Vertex AI · gemini-embedding-2", kind: "model" as const, col: 3, row: 0, stage_ids: ["embed"] },
    { id: "bq", label: "document_chunks", service: "BigQuery load", kind: "store" as const, col: 4, row: 0, stage_ids: ["load_bigquery", "verify_counts"] },
    { id: "idx", label: "Vector index", service: "BigQuery VECTOR INDEX", kind: "index" as const, col: 5, row: 0, stage_ids: ["build_indexes"] },
  ],
  ingest_edges: [
    { from: "gcs", to: "parse" },
    { from: "parse", to: "chunk" },
    { from: "chunk", to: "embed" },
    { from: "embed", to: "bq" },
    { from: "bq", to: "idx" },
  ],
};
