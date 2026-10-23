/**
 * DEV-ONLY Level 5 (Managed Vertex AI Search) fixture. Imported exclusively behind `if (import.meta.env.DEV)` so it
 * tree-shakes out of `npm run build`. Every value is a placeholder following the PROVISIONAL Level 5 contract
 * (retrieve: request / total_size / results[].extractive_segments / attribution_token / api_ms; synthesize mode
 * `vertex_ai_search_summary` or `model_over_segments`; page-level citations); every frame is flagged
 * `synthetic_dev_fixture: true` and the Shell shows the "DEV FIXTURE — not real data" banner.
 * `?mock=1` plays the managed-summary mode; `?mock=1&mock_mode=model` plays a catalog model over the segments.
 * Resource names deliberately contain a fake `projects/<digits>` so safe-mode masking can be checked on screen.
 */

import type { ArchitectureDescriptor, BattleEvent, IngestEvent, JsonObject } from "../../types/arena";
import type { TimedFixtureEvent } from "./level1_run";

const ENGINE = "managed_vertex";
const T0 = Date.parse("2026-10-23T14:00:00.000Z");
const F = true as const;
const SOL = "solicitation_01";
const ts = (offsetMs: number): string => new Date(T0 + offsetMs).toISOString();

const QUERY =
  "According to the Multi-Region VPC topology diagram in Solicitation 01 (03_Technical_Exhibit_Architecture.pdf) and Amendment 0001, what are the Primary and Failover GCP regions, the Cloud HA VPN BGP ASN, the private connectivity mechanism to BigQuery/Vertex AI, and the amended RTO/RPO targets?";
const SERVING_CONFIG =
  "projects/000000000000/locations/global/collections/default_collection/engines/devfixture-engine/servingConfigs/default_search";

const SEARCH_REQUEST: JsonObject = {
  serving_config: SERVING_CONFIG,
  query: QUERY,
  page_size: 5,
  content_search_spec: {
    extractive_content_spec: { max_extractive_segment_count: 3, return_extractive_segment_score: true },
    summary_spec: { summary_result_count: 5, include_citations: true, ignore_non_summary_seeking_query: false },
  },
};

const seg = (content: string, page_number: number, relevance_score: number): JsonObject => ({ content, page_number, relevance_score });

const RESULTS: JsonObject[] = [
  {
    document_id: "devfix-doc-03",
    doc_name: "03_Technical_Exhibit_Architecture.pdf",
    title: "Technical Exhibit — Architecture (dev fixture)",
    uri: "gs://devfixture-bucket/sample_docs/03_Technical_Exhibit_Architecture.pdf",
    extractive_segments: [
      seg("[DEV FIXTURE segment] Multi-Region VPC topology: Primary region us-central1, failover region us-east4 (warm standby).", 1, 0.94),
      seg("[DEV FIXTURE segment] Cloud HA VPN with Cloud Router BGP ASN 64512 peering to on-premises ASN 65001.", 1, 0.88),
      seg("[DEV FIXTURE segment] Private Service Connect endpoints provide private access to BigQuery and Vertex AI.", 2, 0.81),
    ],
  },
  {
    document_id: "devfix-doc-06",
    doc_name: "06_Amendment_0001_Scope_Update.pdf",
    title: "Amendment 0001 — Scope Update (dev fixture)",
    uri: "gs://devfixture-bucket/sample_docs/06_Amendment_0001_Scope_Update.pdf",
    extractive_segments: [seg("[DEV FIXTURE segment] Clause SEC-4.2.1 is hereby superseded: RTO 15 minutes, RPO 0 minutes.", 1, 0.9)],
  },
  {
    document_id: "devfix-doc-01",
    doc_name: "01_RFP_Base_Solicitation.pdf",
    title: "RFP Base Solicitation (dev fixture)",
    uri: "gs://devfixture-bucket/sample_docs/01_RFP_Base_Solicitation.pdf",
    extractive_segments: [seg("[DEV FIXTURE segment] SEC-4.2.1 Disaster recovery: RTO 4 hours, RPO 1 hour (base).", 1, 0.62)],
  },
  {
    document_id: "devfix-doc-05",
    doc_name: "05_SLA_and_QASP_Requirements.pdf",
    title: "SLA and QASP Requirements (dev fixture)",
    uri: "gs://devfixture-bucket/sample_docs/05_SLA_and_QASP_Requirements.pdf",
    extractive_segments: [seg("[DEV FIXTURE segment] | Metric | Target | RTO | 4 hours |", 2, 0.41)],
  },
];

const CITATIONS = [
  { chunk_id: "devfix-doc-03#p1", doc_name: "03_Technical_Exhibit_Architecture.pdf", solicitation_id: SOL, page_number: 1, section_id: null, element_type: "narrative_text", bbox_source: "page", bbox: { page: 1, x0: 0, y0: 0, x1: 1, y1: 1 }, excerpt: "Multi-Region VPC topology: Primary region us-central1, failover region us-east4" },
  { chunk_id: "devfix-doc-06#p1", doc_name: "06_Amendment_0001_Scope_Update.pdf", solicitation_id: SOL, page_number: 1, section_id: null, element_type: "narrative_text", bbox_source: "page", bbox: { page: 1, x0: 0, y0: 0, x1: 1, y1: 1 }, excerpt: "Clause SEC-4.2.1 is hereby superseded" },
  { chunk_id: "devfix-doc-03#p2", doc_name: "03_Technical_Exhibit_Architecture.pdf", solicitation_id: SOL, page_number: 2, section_id: null, element_type: "narrative_text", bbox_source: "page", bbox: { page: 2, x0: 0, y0: 0, x1: 1, y1: 1 }, excerpt: "Private Service Connect endpoints provide private access" },
];

type Ev = BattleEvent & { readonly synthetic_dev_fixture: true };
const ev = (e: BattleEvent): Ev => ({ ...e, synthetic_dev_fixture: F }) as Ev;

const SUMMARY_TEXT =
  "[DEV FIXTURE] The primary region is us-central1 and the failover region is us-east4 [1]. Cloud HA VPN uses BGP ASN 64512 [1]; Private Service Connect provides private access to BigQuery and Vertex AI [3]. Amendment 0001 supersedes SEC-4.2.1 with RTO 15 minutes and RPO 0 minutes [2].";

/** Level 5 dev fixture; `mode` selects the synthesize path. */
export function level5DevFixture(mode: "summary" | "model"): readonly TimedFixtureEvent[] {
  const runId = mode === "summary" ? "devfixture-level5-summary" : "devfixture-level5-model";
  const synth = mode === "summary" ? "vertex-ai-search-summary" : "gemini-4-argon";
  const base = { run_id: runId, engine: ENGINE } as const;
  const synthesizeStarted = mode === "summary"
    ? { label: "Managed summary (Vertex AI Search)", service: "Vertex AI Search · summary (dev fixture)", request: { mode: "vertex_ai_search_summary", summary_result_count: 5, include_citations: true } }
    : { label: "Answer synthesis over extractive segments", service: "Vertex AI · Gemini 4 Argon (dev fixture)", request: { mode: "model_over_segments", model: "gemini-4-argon", segments: 6 } };
  const synthesizePayload: JsonObject = mode === "summary"
    ? {
        mode: "vertex_ai_search_summary",
        summary: SUMMARY_TEXT,
        references: [
          { document_id: "devfix-doc-03", doc_name: "03_Technical_Exhibit_Architecture.pdf", page_number: 1 },
          { document_id: "devfix-doc-06", doc_name: "06_Amendment_0001_Scope_Update.pdf", page_number: 1 },
          { document_id: "devfix-doc-03", doc_name: "03_Technical_Exhibit_Architecture.pdf", page_number: 2 },
          { document_id: "devfix-doc-01", doc_name: "01_RFP_Base_Solicitation.pdf" },
        ],
        model_version: "devfixture-summary-model",
        summary_skipped_reasons: [],
      }
    : { mode: "model_over_segments", model: "gemini-4-argon", thinking_level: "high", prompt_tokens: 1180, output_tokens: 142, segments_used: 6 };
  const answer = mode === "summary" ? SUMMARY_TEXT : "[DEV FIXTURE] Gemini over segments: us-central1 primary, us-east4 failover; HA VPN BGP ASN 64512; Private Service Connect; Amendment 0001 RTO 15 min / RPO 0 min.";
  return [
    { t_rel: 0, event: ev({ event: "run_started", run_id: runId, engines: [ENGINE], demo_mode: "dev_fixture", question_id: "q_vpc_failover_diagram", synthesis_model: synth, judge_model: "claude-opus-5-5", ts: ts(0) }) },
    { t_rel: 150, event: ev({ ...base, event: "stage_started", stage: "query_prep", label: "Query preparation", service: "engine (local)", request: { original_query: QUERY }, ts: ts(150) }) },
    { t_rel: 200, event: ev({ ...base, event: "stage_completed", stage: "query_prep", duration_ms: 2, payload: { original_query: QUERY, prepared_query: QUERY, expansions: [] }, ts: ts(350) }) },
    { t_rel: 60, event: ev({ ...base, event: "stage_started", stage: "retrieve", label: "Vertex AI Search (extractive segments)", service: "Discovery Engine SearchService.search (dev fixture)", request: SEARCH_REQUEST, ts: ts(410) }) },
    {
      t_rel: 1300,
      event: ev({
        ...base, event: "stage_completed", stage: "retrieve", duration_ms: 1288,
        payload: { request: SEARCH_REQUEST, total_size: 37, results: RESULTS, attribution_token: "devfixtureAttributionTokenABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", api_ms: 1241, serving_config: SERVING_CONFIG },
        ts: ts(1700),
      }),
    },
    { t_rel: 40, event: ev({ ...base, event: "stage_started", stage: "synthesize", ...synthesizeStarted, ts: ts(1740) }) },
    { t_rel: mode === "summary" ? 900 : 2200, event: ev({ ...base, event: "stage_completed", stage: "synthesize", duration_ms: mode === "summary" ? 870 : 2180, payload: synthesizePayload, ts: ts(3900) }) },
    {
      t_rel: 30,
      event: ev({
        ...base, event: "answer_complete", answer, citations: CITATIONS, latency_ms: 3950, cost_usd: mode === "summary" ? 0.004 : 0.0121,
        usage: { prompt_tokens: mode === "summary" ? 0 : 1180, output_tokens: mode === "summary" ? 0 : 142, embedding_tokens: 0, model: synth },
        flags: { reranker_used: false, adk_runner_used: false, layout_source: null },
        ts: ts(3950),
      }),
    },
    { t_rel: 40, event: ev({ ...base, event: "stage_started", stage: "evaluate", label: "LLM-as-a-judge", service: "Vertex AI · Claude Opus 5.5 (dev fixture)", request: { model: "claude-opus-5-5" }, ts: ts(3990) }) },
    { t_rel: 1000, event: ev({ ...base, event: "stage_completed", stage: "evaluate", duration_ms: 980, payload: { model: "claude-opus-5-5", input_tokens: 1650, output_tokens: 140 }, ts: ts(4970) }) },
    { t_rel: 20, event: ev({ ...base, event: "eval_complete", metrics: { groundedness: 0.95, qa_relevance: 0.9, context_precision: 0.8 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 980, rationale: "[DEV FIXTURE] synthetic rationale", ts: ts(4990) }) },
    { t_rel: 20, event: ev({ event: "battle_complete", run_id: runId, engines_completed: [ENGINE], engines_failed: [], ts: ts(5010) }) },
  ];
}

/** Mirrors the expected Level 5 descriptor (design metadata only). */
export const LEVEL5_DEV_ARCHITECTURE: ArchitectureDescriptor = {
  level_id: "managed_vertex",
  nodes: [
    { id: "user", label: "User question", service: "Browser", kind: "actor", col: 0, row: 1, stage_ids: ["query_prep"] },
    { id: "ds", label: "Search + extractive segments", service: "Vertex AI Search", kind: "store", col: 1, row: 1, stage_ids: ["retrieve"] },
    { id: "summary", label: "Answer synthesis", service: "Vertex AI Search summary / catalog model", kind: "model", col: 2, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI · claude-opus-5-5", kind: "model", col: 3, row: 1, stage_ids: ["evaluate"] },
  ],
  edges: [
    { from: "user", to: "ds", label: "search request" },
    { from: "ds", to: "summary", label: "segments" },
    { from: "summary", to: "judge", label: "answer" },
  ],
  ingest_nodes: [
    { id: "corpus", label: "Local corpus", service: "Local PDFs", kind: "store", col: 0, row: 0, stage_ids: ["discover_docs"] },
    { id: "gcs", label: "Corpus bucket", service: "Cloud Storage", kind: "store", col: 1, row: 0, stage_ids: ["upload_gcs"] },
    { id: "import", label: "Managed import", service: "Vertex AI Search data store", kind: "process", col: 2, row: 0, stage_ids: ["import_documents", "parse", "chunk", "embed", "load_bigquery", "build_indexes"] },
    { id: "verify", label: "Document count", service: "Vertex AI Search", kind: "store", col: 3, row: 0, stage_ids: ["verify_counts"] },
  ],
  ingest_edges: [
    { from: "corpus", to: "gcs", label: "PDFs" },
    { from: "gcs", to: "import", label: "GCS URIs" },
    { from: "import", to: "verify", label: "documents" },
  ],
};

export interface TimedIngestFixtureEvent {
  readonly t_rel: number;
  readonly event: IngestEvent & { readonly synthetic_dev_fixture: true };
}

/** DEV-only managed import job (discover → upload → import → verify; BigQuery stages skipped). */
export function level5IngestFixture(): readonly TimedIngestFixtureEvent[] {
  const job = "devfixture-ing-level5";
  const base = { job_id: job, engine: ENGINE } as const;
  const skip = (stage: string, at: number): TimedIngestFixtureEvent => ({
    t_rel: 20,
    event: { ...base, event: "stage_skipped", stage, reason: "managed by Vertex AI Search", ts: ts(at), synthetic_dev_fixture: F } as unknown as TimedIngestFixtureEvent["event"],
  });
  const e = (x: IngestEvent): TimedIngestFixtureEvent["event"] => ({ ...x, synthetic_dev_fixture: F }) as TimedIngestFixtureEvent["event"];
  return [
    { t_rel: 0, event: e({ event: "job_started", job_id: job, level: ENGINE, tier_profile: "managed_vertex", ts: ts(0) }) },
    { t_rel: 100, event: e({ ...base, event: "stage_started", stage: "discover_docs", label: "Discover corpus", service: "Local corpus", request: { corpus_dir: "data/sample_docs/procurement_corpus" }, ts: ts(100) }) },
    { t_rel: 200, event: e({ ...base, event: "stage_completed", stage: "discover_docs", duration_ms: 4, payload: { docs: 45 }, ts: ts(300) }) },
    { t_rel: 50, event: e({ ...base, event: "stage_started", stage: "upload_gcs", label: "Upload to Cloud Storage", service: "Cloud Storage (dev fixture)", request: { prefix: "gs://devfixture-bucket/sample_docs/" }, ts: ts(350) }) },
    { t_rel: 900, event: e({ ...base, event: "stage_completed", stage: "upload_gcs", duration_ms: 880, payload: { uploaded: 3, skipped_existing: 42, total: 45, bytes: 1_254_400 }, ts: ts(1250) }) },
    { t_rel: 50, event: e({ ...base, event: "stage_started", stage: "import_documents", label: "Import into data store", service: "Discovery Engine DocumentService.import_documents (dev fixture)", request: { parent: "projects/000000000000/locations/global/collections/default_collection/dataStores/devfixture-store/branches/default_branch", reconciliation_mode: "FULL" }, ts: ts(1300) }) },
    skip("parse", 1320), skip("chunk", 1340), skip("embed", 1360), skip("load_bigquery", 1380), skip("build_indexes", 1400),
    { t_rel: 1500, event: e({ ...base, event: "stage_completed", stage: "import_documents", duration_ms: 1520, payload: { success_count: 45, failure_count: 0, operation: "projects/000000000000/locations/global/operations/import-documents-devfixture", operation_ms: 1490 }, ts: ts(2900) }) },
    { t_rel: 50, event: e({ ...base, event: "stage_started", stage: "verify_counts", label: "Verify document count", service: "Discovery Engine DocumentService.list_documents (dev fixture)", request: {}, ts: ts(2950) }) },
    { t_rel: 300, event: e({ ...base, event: "stage_completed", stage: "verify_counts", duration_ms: 290, payload: { document_count: 45, expected: 45, data_store_state: "ACTIVE (dev fixture)" }, ts: ts(3250) }) },
    { t_rel: 30, event: e({ event: "job_complete", job_id: job, counts_by_tier: { managed_vertex: 45 }, index_states: { data_store: "ACTIVE (dev fixture)" }, ts: ts(3280) }) },
  ];
}
