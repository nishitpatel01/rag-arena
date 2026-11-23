/**
 * Pure deterministic FCIS Enterprise TCO & Workload-Fit Engine (Client Mirror, Slice #1 v1.6.1).
 *
 * Mirrors `backend/app/core/tco.py` 1:1 so the Use-Case Architecture Wizard and Monthly TCO
 * Calculator respond in 0 ms on slider drags and work 100% offline / in `?mock=1`.
 *
 * Authenticity invariant (`spec.md` P3): NEVER hardcodes fake QA / Groundedness / Context Precision
 * scores. Recorded cohort metrics are joined dynamically from `LeaderboardResponse` (`GET /api/v1/leaderboard`)
 * and `LevelDescriptor[]` (`AppDataProvider`).
 */

import { LEVEL_IDS, type LeaderboardResponse, type LevelDescriptor, type LevelId } from "../../types/arena";

export const DATA_GRAVITY_OPTIONS = [
  "bigquery_warehouse",
  "postgres_oltp",
  "unstructured_pdfs",
  "managed_turnkey",
] as const;
export type DataGravity = (typeof DATA_GRAVITY_OPTIONS)[number];

export const LATENCY_SLA_OPTIONS = [
  "sub_second_retrieval",
  "interactive_rag",
  "deep_agentic_reasoning",
] as const;
export type LatencySla = (typeof LATENCY_SLA_OPTIONS)[number];

export const COMPLIANCE_POSTURE_OPTIONS = [
  "standard_serverless",
  "managed_turnkey",
  "cmek_vpc",
  "strict_document_acls",
] as const;
export type CompliancePosture = (typeof COMPLIANCE_POSTURE_OPTIONS)[number];

export const DOC_COMPLEXITY_OPTIONS = [
  "plain_text",
  "tables_diagrams",
  "multi_hop_math",
] as const;
export type DocComplexity = (typeof DOC_COMPLEXITY_OPTIONS)[number];

export const TCO_SYNTHESIS_MODELS = [
  "gemini-4-hydrogen",
  "gemini-4-carbon",
  "gemini-4-argon",
  "claude-sonnet-4-6",
  "claude-opus-5-5",
  "gemini-3.8-flash",
] as const;
export type TcoSynthesisModel = (typeof TCO_SYNTHESIS_MODELS)[number];

export function isTcoSynthesisModel(modelId: string): modelId is TcoSynthesisModel {
  return (TCO_SYNTHESIS_MODELS as readonly string[]).includes(modelId);
}

export type IdleMode = "serverless_zero" | "dedicated_cluster" | "dedicated_replica";
export type FitTier = "recommended" | "runner_up" | "viable" | "not_recommended" | "baseline";
export type CrossoverRegime = "serverless_favorable" | "crossover_zone" | "dedicated_scann_favorable";
export type TcoSortKey = "fit_score" | "monthly_tco" | "recorded_qa" | "level_order";

export const MONTHLY_QUERIES_BOUNDS = [1_000, 10_000_000] as const;
export const CORPUS_PAGES_BOUNDS = [100, 500_000] as const;

export const QUERY_QUICK_PICKS: readonly number[] = [5_000, 25_000, 100_000, 500_000, 2_000_000, 10_000_000];
export const CORPUS_QUICK_PICKS: readonly number[] = [500, 5_000, 25_000, 100_000, 500_000];

export interface WizardOptionMeta<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly hint: string;
}

export const DATA_GRAVITY_CHOICES: readonly WizardOptionMeta<DataGravity>[] = [
  { value: "unstructured_pdfs", label: "Unstructured PDFs (GCS)", hint: "RFPs, contracts, exhibits in Cloud Storage" },
  { value: "bigquery_warehouse", label: "BigQuery Warehouse", hint: "SQL-governed enterprise lakehouse tables" },
  { value: "postgres_oltp", label: "PostgreSQL / OLTP", hint: "Operational transactional app database" },
  { value: "managed_turnkey", label: "Managed Turnkey PaaS", hint: "Zero chunking or vector SQL maintenance" },
];

export const DOC_COMPLEXITY_CHOICES: readonly WizardOptionMeta<DocComplexity>[] = [
  { value: "plain_text", label: "Plain Text & Clauses", hint: "Narrative policies, standard MSAs, prose" },
  { value: "tables_diagrams", label: "Tables & Diagrams", hint: "Multi-column rate matrices & topology figures" },
  { value: "multi_hop_math", label: "Multi-Hop & Math", hint: "Superseding amendments + CLIN arithmetic" },
];

export const LATENCY_SLA_CHOICES: readonly WizardOptionMeta<LatencySla>[] = [
  { value: "sub_second_retrieval", label: "Sub-Second Retrieval", hint: "<400 ms ANN retrieval for live apps" },
  { value: "interactive_rag", label: "Interactive RAG", hint: "10–28 s full grounded synthesis" },
  { value: "deep_agentic_reasoning", label: "Deep Agentic Audit", hint: "30–60 s multi-tool verification loop" },
];

export const COMPLIANCE_POSTURE_CHOICES: readonly WizardOptionMeta<CompliancePosture>[] = [
  { value: "standard_serverless", label: "Serverless ($0 Idle)", hint: "Scale-to-zero idle compute; pay per query" },
  { value: "managed_turnkey", label: "Managed Turnkey SLA", hint: "Google-managed indexing & retrieval PaaS" },
  { value: "cmek_vpc", label: "CMEK + VPC-SC Strict", hint: "Regional residency, KMS keys & Private IP" },
  { value: "strict_document_acls", label: "Strict Document ACLs", hint: "Per-chunk RLS & group-filtered retrieval" },
];

export const SYNTHESIS_MODEL_CHOICES: readonly WizardOptionMeta<TcoSynthesisModel>[] = [
  { value: "gemini-4-hydrogen", label: "Gemini 4 Hydrogen", hint: "$1.50 / $7.50 per 1M (fast tier)" },
  { value: "gemini-4-carbon", label: "Gemini 4 Carbon", hint: "$2.00 / $10.00 per 1M (balanced)" },
  { value: "gemini-4-argon", label: "Gemini 4 Argon", hint: "$2.00 / $10.00 per 1M (flagship)" },
  { value: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", hint: "$3.00 / $15.00 per 1M (L1–L7)" },
  { value: "claude-opus-5-5", label: "Claude Opus 5.5", hint: "$4.00 / $20.00 per 1M (L1–L7)" },
  { value: "gemini-3.8-flash", label: "Gemini 3.8 Flash (Legacy)", hint: "$0.75 / $3.75 per 1M (recorded)" },
];

export interface TcoWorkload {
  readonly data_gravity: DataGravity;
  readonly monthly_queries: number;
  readonly corpus_pages: number;
  readonly latency_sla: LatencySla;
  readonly compliance_posture: CompliancePosture;
  readonly doc_complexity: DocComplexity;
  readonly synthesis_model: TcoSynthesisModel;
}

export const DEFAULT_WORKLOAD: TcoWorkload = {
  data_gravity: "unstructured_pdfs",
  monthly_queries: 25_000,
  corpus_pages: 5_000,
  latency_sla: "interactive_rag",
  compliance_posture: "standard_serverless",
  doc_complexity: "tables_diagrams",
  synthesis_model: "gemini-4-hydrogen",
};

export interface ScenarioPreset {
  readonly preset_id: string;
  readonly title: string;
  readonly subtitle: string;
  readonly workload: TcoWorkload;
}

export const ENTERPRISE_SCENARIO_PRESETS: readonly ScenarioPreset[] = [
  {
    preset_id: "procurement_multimodal",
    title: "RFP Tables, Exhibits & Diagrams",
    subtitle: "Unstructured PDFs with pricing schedules, SOC matrices, and architecture diagrams",
    workload: {
      data_gravity: "unstructured_pdfs",
      monthly_queries: 25_000,
      corpus_pages: 5_000,
      latency_sla: "interactive_rag",
      compliance_posture: "standard_serverless",
      doc_complexity: "tables_diagrams",
      synthesis_model: "gemini-4-hydrogen",
    },
  },
  {
    preset_id: "contract_audit_agentic",
    title: "Multi-Hop Contract & SLA Audit",
    subtitle: "Superseding amendments, cross-document rate cards, and verified financial math",
    workload: {
      data_gravity: "unstructured_pdfs",
      monthly_queries: 15_000,
      corpus_pages: 10_000,
      latency_sla: "deep_agentic_reasoning",
      compliance_posture: "cmek_vpc",
      doc_complexity: "multi_hop_math",
      synthesis_model: "gemini-4-argon",
    },
  },
  {
    preset_id: "bq_warehouse_rag",
    title: "BigQuery Lakehouse RAG",
    subtitle: "SQL-governed enterprise documents already curated in BigQuery at $0/mo idle",
    workload: {
      data_gravity: "bigquery_warehouse",
      monthly_queries: 50_000,
      corpus_pages: 20_000,
      latency_sla: "interactive_rag",
      compliance_posture: "standard_serverless",
      doc_complexity: "plain_text",
      synthesis_model: "gemini-4-carbon",
    },
  },
  {
    preset_id: "turnkey_managed_paas",
    title: "Zero-Ops Managed Enterprise PaaS",
    subtitle: "Turnkey GCS corpus import without maintaining SQL chunk tables or vector indexes",
    workload: {
      data_gravity: "managed_turnkey",
      monthly_queries: 30_000,
      corpus_pages: 10_000,
      latency_sla: "interactive_rag",
      compliance_posture: "managed_turnkey",
      doc_complexity: "plain_text",
      synthesis_model: "gemini-4-hydrogen",
    },
  },
  {
    preset_id: "high_qps_operational",
    title: "High-QPS Operational ScaNN (1M/mo)",
    subtitle: "Sub-second retrieval for customer-facing transactional apps at 1M queries/month",
    workload: {
      data_gravity: "postgres_oltp",
      monthly_queries: 1_000_000,
      corpus_pages: 100_000,
      latency_sla: "sub_second_retrieval",
      compliance_posture: "cmek_vpc",
      doc_complexity: "plain_text",
      synthesis_model: "gemini-4-hydrogen",
    },
  },
  {
    preset_id: "enterprise_document_acls",
    title: "Strict Document ACLs & RLS",
    subtitle: "Multi-group Finance, Legal & SOC2 corpora with row-level retrieval isolation at $0/mo idle",
    workload: {
      data_gravity: "bigquery_warehouse",
      monthly_queries: 50_000,
      corpus_pages: 25_000,
      latency_sla: "interactive_rag",
      compliance_posture: "strict_document_acls",
      doc_complexity: "tables_diagrams",
      synthesis_model: "gemini-4-hydrogen",
    },
  },
];

export interface RecordedCohortSnapshot {
  readonly synthesis_model: string;
  readonly judge_model: string;
  readonly runs: number;
  readonly questions_covered: number;
  readonly avg_qa_relevance: number | null;
  readonly avg_groundedness: number | null;
  readonly avg_context_precision: number | null;
  readonly avg_battle_score: number | null;
  readonly avg_latency_ms: number | null;
  readonly avg_cost_usd: number | null;
}

export interface CrossoverAnalysis {
  readonly vvs2_vs_vais_queries: number;
  readonly alloydb_vs_vais_queries: number;
  readonly vvs2_vs_bq_multimodal_queries: number;
  readonly regime: CrossoverRegime;
  readonly summary: string;
}

export interface ArchitectureTcoRow {
  readonly level_id: LevelId;
  readonly order: number;
  readonly label: string;
  readonly short_label: string;
  readonly gcp_services: readonly string[];
  readonly idle_mode: IdleMode;
  readonly capacity_units: number;
  readonly standing_monthly_usd: number;
  readonly standing_note: string;
  readonly storage_monthly_usd: number;
  readonly embedding_monthly_usd: number;
  readonly retrieval_scan_monthly_usd: number;
  readonly rerank_monthly_usd: number;
  readonly synthesis_monthly_usd: number;
  readonly query_variable_monthly_usd: number;
  readonly total_monthly_usd: number;
  readonly cost_per_1k_queries_usd: number;
  readonly one_time_ingestion_usd: number;
  readonly ingestion_note: string;
  readonly fit_score: number;
  readonly fit_tier: FitTier;
  readonly rationale: readonly string[];
  readonly tradeoffs: readonly string[];
  readonly recorded_cohort: RecordedCohortSnapshot | null;
  readonly alternate_cohort: RecordedCohortSnapshot | null;
}

export interface EnterpriseTcoEstimate {
  readonly workload: TcoWorkload;
  readonly recommended_level_id: LevelId;
  readonly runner_up_level_id: LevelId;
  readonly crossover: CrossoverAnalysis;
  readonly architectures: readonly ArchitectureTcoRow[];
}

// Published GCP pricing constants (mirroring `backend/app/core/pricing.py` & `backend/app/core/tco.py`).
const HOURS_PER_MONTH = 730;
const BYTES_PER_GIB = 1024 ** 3;
const BYTES_PER_TIB = 1024 ** 4;

const MODEL_RATES: Readonly<Record<string, { readonly inPer1M: number; readonly outPer1M: number; readonly per1kReq: number }>> = {
  "gemini-4-hydrogen": { inPer1M: 1.5, outPer1M: 7.5, per1kReq: 0 },
  "gemini-4-carbon": { inPer1M: 2.0, outPer1M: 10.0, per1kReq: 0 },
  "gemini-4-argon": { inPer1M: 2.0, outPer1M: 10.0, per1kReq: 0 },
  "claude-sonnet-4-6": { inPer1M: 3.0, outPer1M: 15.0, per1kReq: 0 },
  "claude-opus-5-5": { inPer1M: 4.0, outPer1M: 20.0, per1kReq: 0 },
  "gemini-3.8-flash": { inPer1M: 0.75, outPer1M: 3.75, per1kReq: 0 },
  "gemini-embedding-2": { inPer1M: 0.2, outPer1M: 0, per1kReq: 0 },
  "text-embedding-005": { inPer1M: 0.1, outPer1M: 0, per1kReq: 0 },
  "discovery-engine-rank": { inPer1M: 0, outPer1M: 0, per1kReq: 1.0 },
  "vertex-ai-search-enterprise": { inPer1M: 0, outPer1M: 0, per1kReq: 4.0 },
};

const BQ_SCAN_USD_PER_TIB = 6.25;
const BQ_MIN_SCAN_BYTES = 10 * 1024 * 1024;
const BQ_UNCACHED_QUERY_RATIO = 0.8;
const BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH = 0.02;
const GCS_STANDARD_USD_PER_GIB_MONTH = 0.02;
const PDF_BYTES_PER_PAGE = 120 * 1024;

const DOCAI_LAYOUT_USD_PER_1K_PAGES = 10.0;
const VISION_PROMPT_TOKENS_PER_DIAGRAM_PAGE = 1_800;
const VISION_OUTPUT_TOKENS_PER_DIAGRAM_PAGE = 900;

const VAIS_FREE_STORAGE_GIB = 10.0;
const VAIS_STORAGE_USD_PER_GIB_MONTH = 2.5;

const RAG_ENGINE_STORAGE_USD_PER_GIB_MONTH = 0.023;
export const RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD = 65.7;

const ALLOYDB_VCPU_USD_PER_HOUR = 0.06636;
const ALLOYDB_RAM_GIB_USD_PER_HOUR = 0.01126;
export const ALLOYDB_BASE_MONTHLY_USD =
  Math.round((2 * ALLOYDB_VCPU_USD_PER_HOUR + 16 * ALLOYDB_RAM_GIB_USD_PER_HOUR) * HOURS_PER_MONTH * 10_000) / 10_000;
const ALLOYDB_QUERIES_PER_UNIT_MONTH = 2_500_000;
const ALLOYDB_STORAGE_USD_PER_GIB_MONTH = 0.3;
const ALLOYDB_MIN_STORAGE_GIB = 10.0;

const VVS2_REPLICA_USD_PER_HOUR = 0.2688;
export const VVS2_BASE_REPLICA_MONTHLY_USD = Math.round(VVS2_REPLICA_USD_PER_HOUR * HOURS_PER_MONTH * 10_000) / 10_000;
const VVS2_QUERIES_PER_REPLICA_MONTH = 3_500_000;
const VVS2_STORAGE_USD_PER_GIB_MONTH = 0.04;
const VVS2_MIN_STORAGE_GIB = 1.0;

const ALLOWED_KEYS = new Set([
  "data_gravity",
  "monthly_queries",
  "corpus_pages",
  "latency_sla",
  "compliance_posture",
  "doc_complexity",
  "synthesis_model",
]);

function round2(val: number): number {
  return Math.floor(val * 100 + 0.5 + 1e-12) / 100;
}

function requireStrictInt(name: string, value: unknown, bounds: readonly [number, number]): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < bounds[0] || value > bounds[1]) {
    throw new Error(`Field '${name}' must be an integer in [${bounds[0]}, ${bounds[1]}], got ${String(value)}`);
  }
  return value;
}

function requireEnum<T extends string>(name: string, value: unknown, allowed: readonly T[]): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new Error(`Field '${name}' must be one of [${allowed.join(", ")}], got ${String(value)}`);
  }
  return value as T;
}

export function parseTcoWorkload(raw: Record<string, unknown> | null | undefined): TcoWorkload {
  if (raw == null) return DEFAULT_WORKLOAD;
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("Request body must be a JSON object");
  }
  for (const key of Object.keys(raw)) {
    if (!ALLOWED_KEYS.has(key)) {
      throw new Error(`Unknown workload field: '${key}'`);
    }
  }
  return {
    data_gravity: requireEnum("data_gravity", raw.data_gravity ?? DEFAULT_WORKLOAD.data_gravity, DATA_GRAVITY_OPTIONS),
    monthly_queries: requireStrictInt(
      "monthly_queries",
      raw.monthly_queries ?? DEFAULT_WORKLOAD.monthly_queries,
      MONTHLY_QUERIES_BOUNDS
    ),
    corpus_pages: requireStrictInt(
      "corpus_pages",
      raw.corpus_pages ?? DEFAULT_WORKLOAD.corpus_pages,
      CORPUS_PAGES_BOUNDS
    ),
    latency_sla: requireEnum("latency_sla", raw.latency_sla ?? DEFAULT_WORKLOAD.latency_sla, LATENCY_SLA_OPTIONS),
    compliance_posture: requireEnum(
      "compliance_posture",
      raw.compliance_posture ?? DEFAULT_WORKLOAD.compliance_posture,
      COMPLIANCE_POSTURE_OPTIONS
    ),
    doc_complexity: requireEnum(
      "doc_complexity",
      raw.doc_complexity ?? DEFAULT_WORKLOAD.doc_complexity,
      DOC_COMPLEXITY_OPTIONS
    ),
    synthesis_model: requireEnum(
      "synthesis_model",
      raw.synthesis_model ?? DEFAULT_WORKLOAD.synthesis_model,
      TCO_SYNTHESIS_MODELS
    ),
  };
}

/** Map a `0..1000` slider position onto a clean integer in `[min, max]` along a log10 curve. */
export function logSliderToValue(position: number, bounds: readonly [number, number]): number {
  const clamped = Math.max(0, Math.min(1000, Number.isFinite(position) ? position : 0));
  if (clamped === 0) return bounds[0];
  if (clamped === 1000) return bounds[1];
  const minLog = Math.log10(bounds[0]);
  const maxLog = Math.log10(bounds[1]);
  const raw = Math.pow(10, minLog + (clamped / 1000) * (maxLog - minLog));
  const step = raw >= 1_000_000 ? 50_000 : raw >= 100_000 ? 5_000 : raw >= 10_000 ? 500 : raw >= 1_000 ? 100 : 10;
  const snapped = Math.round(raw / step) * step;
  return Math.max(bounds[0], Math.min(bounds[1], snapped));
}

/** Inverse of `logSliderToValue`: convert a value in `[min, max]` into a `0..1000` slider position. */
export function valueToLogSlider(value: number, bounds: readonly [number, number]): number {
  const clamped = Math.max(bounds[0], Math.min(bounds[1], Number.isFinite(value) ? value : bounds[0]));
  if (clamped <= bounds[0]) return 0;
  if (clamped >= bounds[1]) return 1000;
  const minLog = Math.log10(bounds[0]);
  const maxLog = Math.log10(bounds[1]);
  return Math.round(((Math.log10(clamped) - minLog) / (maxLog - minLog)) * 1000);
}

interface LevelStaticMeta {
  readonly level_id: LevelId;
  readonly order: number;
  readonly default_label: string;
  readonly short_label: string;
  readonly gcp_services: readonly string[];
  readonly idle_mode: IdleMode;
  readonly embedding_model: string;
  readonly query_embed_tokens: number;
  readonly rank_counts_per_query: number;
  readonly prompt_tokens_per_query: number;
  readonly output_tokens_per_query: number;
  readonly ingest_embed_tokens_per_page: number;
}

const LEVEL_META: readonly LevelStaticMeta[] = [
  {
    level_id: "managed_vertex",
    order: 1,
    default_label: "Managed Vertex AI Search",
    short_label: "L1 · Vertex AI Search",
    gcp_services: ["Vertex AI Search (Enterprise)", "Cloud Storage Import", "Vertex AI Gemini"],
    idle_mode: "serverless_zero",
    embedding_model: "",
    query_embed_tokens: 0,
    rank_counts_per_query: 0,
    prompt_tokens_per_query: 3_600,
    output_tokens_per_query: 1_500,
    ingest_embed_tokens_per_page: 0,
  },
  {
    level_id: "rag_engine",
    order: 2,
    default_label: "Managed Vertex AI RAG Engine",
    short_label: "L2 · Vertex RAG Engine",
    gcp_services: ["Vertex AI RAG Engine", "RagManagedDb Serverless", "text-embedding-005", "Vertex AI Gemini"],
    idle_mode: "serverless_zero",
    embedding_model: "text-embedding-005",
    query_embed_tokens: 45,
    rank_counts_per_query: 0,
    prompt_tokens_per_query: 6_200,
    output_tokens_per_query: 1_650,
    ingest_embed_tokens_per_page: 500,
  },
  {
    level_id: "level1_naive",
    order: 3,
    default_label: "Naive Vector RAG (BigQuery)",
    short_label: "L3 · BigQuery Vector",
    gcp_services: ["BigQuery VECTOR_SEARCH", "Vertex AI gemini-embedding-2", "Vertex AI Gemini"],
    idle_mode: "serverless_zero",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 45,
    rank_counts_per_query: 0,
    prompt_tokens_per_query: 1_800,
    output_tokens_per_query: 1_500,
    ingest_embed_tokens_per_page: 540,
  },
  {
    level_id: "level2_hybrid",
    order: 4,
    default_label: "BigQuery Hybrid + Reranker",
    short_label: "L4 · BQ Hybrid + Rerank",
    gcp_services: ["BigQuery VECTOR_SEARCH + SEARCH()", "Discovery Engine Ranking API", "gemini-embedding-2"],
    idle_mode: "serverless_zero",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 45,
    rank_counts_per_query: 1,
    prompt_tokens_per_query: 3_600,
    output_tokens_per_query: 1_500,
    ingest_embed_tokens_per_page: 450,
  },
  {
    level_id: "alloydb_scann",
    order: 5,
    default_label: "AlloyDB PostgreSQL + ScaNN",
    short_label: "L5 · AlloyDB + ScaNN",
    gcp_services: ["AlloyDB for PostgreSQL (ScaNN + GIN)", "Discovery Engine Ranking API", "gemini-embedding-2"],
    idle_mode: "dedicated_cluster",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 45,
    rank_counts_per_query: 1,
    prompt_tokens_per_query: 3_600,
    output_tokens_per_query: 1_500,
    ingest_embed_tokens_per_page: 450,
  },
  {
    level_id: "vertex_vector_v2",
    order: 6,
    default_label: "Vertex AI Vector Search 2.0",
    short_label: "L6 · Vector Search 2.0",
    gcp_services: ["Vector Search 2.0 (DenseScann + RRF)", "Discovery Engine Ranking API", "gemini-embedding-2"],
    idle_mode: "dedicated_replica",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 45,
    rank_counts_per_query: 1,
    prompt_tokens_per_query: 3_600,
    output_tokens_per_query: 1_500,
    ingest_embed_tokens_per_page: 450,
  },
  {
    level_id: "level3_multimodal",
    order: 7,
    default_label: "Layout-Aware Multimodal RAG",
    short_label: "L7 · DocAI + Vision + BQ",
    gcp_services: ["Document AI Layout Parser", "Gemini Vision", "BigQuery Hybrid RRF", "Ranking API"],
    idle_mode: "serverless_zero",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 45,
    rank_counts_per_query: 1,
    prompt_tokens_per_query: 4_000,
    output_tokens_per_query: 1_550,
    ingest_embed_tokens_per_page: 520,
  },
  {
    level_id: "level4_agentic",
    order: 8,
    default_label: "Agentic RAG (Google ADK)",
    short_label: "L8 · Google ADK Agent",
    gcp_services: ["Google ADK Runner", "Document AI + Vision", "BigQuery Search + Amendments", "AST Calculator"],
    idle_mode: "serverless_zero",
    embedding_model: "gemini-embedding-2",
    query_embed_tokens: 160,
    rank_counts_per_query: 3.5,
    prompt_tokens_per_query: 19_500,
    output_tokens_per_query: 4_650,
    ingest_embed_tokens_per_page: 520,
  },
];

function diagramPageRatio(docComplexity: DocComplexity): number {
  if (docComplexity === "tables_diagrams") return 0.25;
  if (docComplexity === "multi_hop_math") return 0.2;
  return 0.05;
}

function bqScanBytesPerQuery(levelId: LevelId, corpusPages: number): number {
  if (levelId === "level1_naive") {
    const tableBytes = corpusPages * 250 * 1024;
    const selectivity = corpusPages >= 600 ? 0.08 : 1.0;
    return Math.max(BQ_MIN_SCAN_BYTES, tableBytes * selectivity) * BQ_UNCACHED_QUERY_RATIO;
  }
  if (levelId === "level2_hybrid") {
    const tableBytes = corpusPages * 28 * 1024;
    const selectivity = corpusPages >= 5_000 ? 0.15 : 1.0;
    return Math.max(BQ_MIN_SCAN_BYTES, tableBytes * selectivity) * BQ_UNCACHED_QUERY_RATIO;
  }
  if (levelId === "level3_multimodal") {
    const tableBytes = corpusPages * 62 * 1024;
    return Math.max(BQ_MIN_SCAN_BYTES, tableBytes) * BQ_UNCACHED_QUERY_RATIO;
  }
  if (levelId === "level4_agentic") {
    const tableBytes = corpusPages * 62 * 1024;
    return 4.5 * Math.max(BQ_MIN_SCAN_BYTES, tableBytes) * BQ_UNCACHED_QUERY_RATIO;
  }
  return 0;
}

function computeCrossover(workload: TcoWorkload): CrossoverAnalysis {
  const rankPerQ = MODEL_RATES["discovery-engine-rank"].per1kReq / 1_000;
  const embPerQ = (45 / 1_000_000) * MODEL_RATES["gemini-embedding-2"].inPer1M;
  const vaisPerQ = MODEL_RATES["vertex-ai-search-enterprise"].per1kReq / 1_000;

  const vaisMinusScannPerQ = Math.max(0.0001, vaisPerQ - (rankPerQ + embPerQ));
  const vvs2VsVais = Math.ceil(VVS2_BASE_REPLICA_MONTHLY_USD / vaisMinusScannPerQ);
  const alloydbVsVais = Math.ceil(
    (ALLOYDB_BASE_MONTHLY_USD + ALLOYDB_MIN_STORAGE_GIB * ALLOYDB_STORAGE_USD_PER_GIB_MONTH) / vaisMinusScannPerQ
  );

  const l3ScanPerQ = (bqScanBytesPerQuery("level3_multimodal", workload.corpus_pages) / BYTES_PER_TIB) * BQ_SCAN_USD_PER_TIB;
  const vvs2VsBqMm = Math.ceil(VVS2_BASE_REPLICA_MONTHLY_USD / Math.max(0.00001, l3ScanPerQ));

  const earliest = Math.min(vvs2VsVais, vvs2VsBqMm);
  let regime: CrossoverRegime;
  let summary: string;

  if (workload.monthly_queries < Math.floor(earliest * 0.85)) {
    regime = "serverless_favorable";
    summary =
      `At ${workload.monthly_queries.toLocaleString("en-US")} queries/mo, $0/mo-idle serverless architectures (L1–L4, L7–L8) minimize TCO; ` +
      `dedicated ScaNN crosses below VAIS at ~${vvs2VsVais.toLocaleString("en-US")} queries/mo and below BQ multimodal scan at ` +
      `~${vvs2VsBqMm.toLocaleString("en-US")} queries/mo (${workload.corpus_pages.toLocaleString("en-US")} pages).`;
  } else if (workload.monthly_queries <= Math.floor(Math.max(alloydbVsVais, earliest) * 1.15)) {
    regime = "crossover_zone";
    summary =
      `Near the monthly TCO crossover (~${earliest.toLocaleString("en-US")}–${alloydbVsVais.toLocaleString("en-US")} queries/mo): ` +
      `weigh sub-400ms dedicated ScaNN latency (L5/L6) against zero-ops serverless elasticity (L1/L2/L4/L7).`;
  } else {
    regime = "dedicated_scann_favorable";
    summary =
      `Above the ~${earliest.toLocaleString("en-US")} queries/mo crossover: dedicated ScaNN (L5 AlloyDB / L6 Vector Search 2.0) ` +
      `amortizes standing compute and avoids per-query VAIS ($4/1k) and BigQuery byte-scan fees.`;
  }

  return {
    vvs2_vs_vais_queries: vvs2VsVais,
    alloydb_vs_vais_queries: alloydbVsVais,
    vvs2_vs_bq_multimodal_queries: vvs2VsBqMm,
    regime,
    summary,
  };
}

function scoreWorkloadFit(
  meta: LevelStaticMeta,
  workload: TcoWorkload,
  crossover: CrossoverAnalysis
): { readonly fitScore: number; readonly rationale: readonly string[]; readonly tradeoffs: readonly string[] } {
  const lid = meta.level_id;
  if (lid === "level1_naive") {
    return {
      fitScore: 22,
      rationale: ["Minimal SQL baseline with $0/mo idle cost for smoke-testing dense embeddings."],
      tradeoffs: ["Misses exact CLIN/clause identifiers, scrambles PDF tables, and ignores superseding amendments."],
    };
  }

  let score = 30;
  const rationale: string[] = [];
  const tradeoffs: string[] = [];

  // 1. Data gravity alignment
  const dg = workload.data_gravity;
  if (dg === "bigquery_warehouse") {
    if (lid === "level2_hybrid") {
      score += 18;
      rationale.push("Keeps vector + lexical RRF retrieval inside BigQuery with zero cross-store ETL.");
    } else if (lid === "level3_multimodal" || lid === "level4_agentic") {
      score += 13;
      rationale.push("Stores layout/multimodal chunks natively in BigQuery alongside enterprise warehouse tables.");
    } else if (lid === "rag_engine") {
      score += 5;
    } else {
      score -= 5;
    }
  } else if (dg === "postgres_oltp") {
    if (lid === "alloydb_scann") {
      score += 22;
      rationale.push("Runs ScaNN cosine ANN + PostgreSQL GIN full-text RRF transactionally inside AlloyDB.");
    } else if (lid === "vertex_vector_v2") {
      score += 8;
      rationale.push("Pairs low-latency managed ScaNN ANN with operational microservices.");
    } else if (lid === "level2_hybrid") {
      score += 3;
    } else {
      score -= 5;
    }
  } else if (dg === "unstructured_pdfs") {
    if (lid === "level3_multimodal" || lid === "level4_agentic") {
      score += 14;
      rationale.push("Parses complex GCS PDFs with Document AI Layout Parser + Gemini Vision diagram transcription.");
    } else if (lid === "rag_engine" || lid === "managed_vertex") {
      score += 11;
      rationale.push("Imports unstructured PDFs directly from Cloud Storage without custom chunkers.");
    } else if (lid === "level2_hybrid" || lid === "vertex_vector_v2") {
      score += 6;
    }
  } else if (dg === "managed_turnkey") {
    if (lid === "rag_engine") {
      score += 20;
      rationale.push("Managed RagCorpus handles PDF chunking, text-embedding-005 indexing, and $0/query retrieval.");
    } else if (lid === "managed_vertex") {
      score += 18;
      rationale.push("Fully managed Vertex AI Search data store with turnkey extractive segments and console ops.");
    } else if (lid === "level2_hybrid") {
      score += 3;
    } else {
      score -= 6;
    }
  }

  // 2. Document complexity alignment
  const dc = workload.doc_complexity;
  if (dc === "plain_text") {
    if (
      lid === "level2_hybrid" ||
      lid === "rag_engine" ||
      lid === "managed_vertex" ||
      lid === "alloydb_scann" ||
      lid === "vertex_vector_v2"
    ) {
      score += 9;
      rationale.push("Narrative & clause text retrieves cleanly without needing Document AI + Vision ingest spend.");
    } else if (lid === "level3_multimodal") {
      score += 2;
      tradeoffs.push("Adds $10/1k-page Document AI Layout Parser cost that plain-text corpora rarely need.");
    } else if (lid === "level4_agentic") {
      score -= 6;
      tradeoffs.push("Multi-turn ADK agent overhead (~3.5x tokens) is unnecessary for single-hop plain text.");
    }
  } else if (dc === "tables_diagrams") {
    if (lid === "level3_multimodal") {
      score += 20;
      rationale.push("Preserves Markdown rate tables, transcribes architecture diagrams, and orders amendments first.");
    } else if (lid === "level4_agentic") {
      score += 12;
      rationale.push("Searches multimodal table/diagram chunks with self-verifying citation checks.");
    } else if (lid === "rag_engine" || lid === "managed_vertex") {
      score += 3;
      tradeoffs.push("Managed chunkers lack explicit Gemini Vision diagram transcription and amendment headers.");
    } else {
      score -= 10;
      tradeoffs.push("Flat text chunking loses multi-column PDF table structure and embedded vector diagrams.");
    }
  } else if (dc === "multi_hop_math") {
    if (lid === "level4_agentic") {
      score += 24;
      rationale.push("Google ADK loop chains check_amendments, AST-safe calculate_contract_pricing, and verify_citations.");
    } else if (lid === "level3_multimodal") {
      score += 10;
      rationale.push("Provides amendment authority headers and table structure, though single-pass without calculator.");
      tradeoffs.push("Single-pass synthesis lacks deterministic multi-solicitation arithmetic verification.");
    } else if (lid === "rag_engine") {
      score += 3;
      tradeoffs.push("Top-10 context recall is strong, but lacks ADK calculator and amendment-chain tools.");
    } else {
      score -= 10;
      tradeoffs.push("Single-pass retrieval misses multi-hop supersession chains and exact cross-doc math.");
    }
  }

  // 3. Latency SLA alignment
  const sla = workload.latency_sla;
  if (sla === "sub_second_retrieval") {
    if (lid === "alloydb_scann" || lid === "vertex_vector_v2") {
      score += 14;
      rationale.push("Sub-400ms warm ScaNN ANN + hybrid RRF retrieval built for real-time user-facing SLAs.");
    } else if (lid === "managed_vertex" || lid === "rag_engine") {
      score += 5;
    } else if (lid === "level2_hybrid" || lid === "level3_multimodal") {
      score -= 3;
      tradeoffs.push("BigQuery serverless query startup adds ~1–2s retrieval overhead vs in-memory ScaNN.");
    } else if (lid === "level4_agentic") {
      score -= 20;
      tradeoffs.push("Multi-turn ADK reasoning averages 44–47s, exceeding strict sub-second/low-latency SLAs.");
    }
  } else if (sla === "interactive_rag") {
    if (lid !== "level4_agentic") {
      score += 6;
    } else {
      score -= 5;
      tradeoffs.push("ADK multi-turn tool loop runs ~44–47s vs ~11–29s on single-pass architectures.");
    }
  } else if (sla === "deep_agentic_reasoning") {
    if (lid === "level4_agentic") {
      score += 14;
      rationale.push("SLA budget accommodates iterative ADK search, amendment lookup, and citation repair.");
    } else if (lid === "level3_multimodal" || lid === "rag_engine") {
      score += 6;
    }
  }

  // 4. Compliance & operations posture
  const cp = workload.compliance_posture;
  if (cp === "standard_serverless") {
    if (meta.idle_mode === "serverless_zero") {
      score += 6;
      if (workload.monthly_queries < crossover.vvs2_vs_vais_queries) {
        rationale.push("Scales to $0.00/mo standing compute when idle — pay strictly per query.");
      }
    } else if (workload.monthly_queries < crossover.vvs2_vs_vais_queries) {
      score -= 10;
      tradeoffs.push("24x7 standing cluster compute is underutilized below the ~66k queries/mo crossover.");
    }
  } else if (cp === "managed_turnkey") {
    if (lid === "rag_engine" || lid === "managed_vertex") {
      score += 10;
    } else if (lid === "level4_agentic" || lid === "alloydb_scann") {
      score -= 8;
      tradeoffs.push("Higher operational surface (custom DB/proxy or multi-tool agent orchestration).");
    }
  } else if (cp === "cmek_vpc") {
    if (lid !== "managed_vertex") {
      score += 6;
      if (lid === "alloydb_scann") {
        rationale.push("Supports regional Private IP / VPC-SC perimeters, PostgreSQL RLS, and CMEK encryption.");
      } else if (lid === "level2_hybrid" || lid === "level3_multimodal" || lid === "level4_agentic") {
        rationale.push("Supports regional BigQuery + GCS CMEK and row-level security policies.");
      }
    }
    if (
      lid === "level2_hybrid" ||
      lid === "level3_multimodal" ||
      lid === "level4_agentic" ||
      lid === "alloydb_scann" ||
      lid === "vertex_vector_v2"
    ) {
      tradeoffs.push("Verify Discovery Engine Ranking API regional endpoint alignment for strict single-region VPC-SC.");
    }
    if (lid === "managed_vertex") {
      score -= 5;
      tradeoffs.push("Default global Discovery Engine config requires regional datastore reconfiguration under strict residency.");
    }
  } else if (cp === "strict_document_acls") {
    if (lid === "level2_hybrid" || lid === "level3_multimodal" || lid === "level4_agentic") {
      score += 10;
      rationale.push("Enforces native BigQuery parameterized UNNEST(@caller_groups) & ROW ACCESS POLICY before synthesis.");
    } else if (lid === "alloydb_scann") {
      score += 10;
      rationale.push("Enforces native PostgreSQL Row-Level Security (CREATE POLICY + GIN allowed_groups) inside the DB engine.");
    } else if (lid === "vertex_vector_v2") {
      score += 8;
      rationale.push("Applies indexed filterFields=['allowed_groups'] $in pre-filtering on dense ScaNN and sparse RRF legs.");
    } else if (lid === "managed_vertex") {
      score += 7;
      rationale.push("Supports Discovery Engine allowed_groups filter expressions and enterprise IdP aclConfig.");
    } else if (lid === "rag_engine") {
      score -= 12;
      tradeoffs.push("Vertex AI RAG Engine enforces IAM at the RagCorpus boundary, not per-document chunk ACLs in a shared corpus.");
    }
  }

  // 5. Economic & corpus-scale crossover adjustments
  if (workload.monthly_queries >= 250_000) {
    if (lid === "vertex_vector_v2" || lid === "alloydb_scann") {
      score += 10;
      rationale.push("Zero per-query ANN scan fee beats per-query PaaS/scan charges past the monthly TCO crossover.");
    } else if (lid === "managed_vertex") {
      score -= 6;
      tradeoffs.push("$4.00/1k Vertex AI Search query pricing compounds rapidly at >=250k queries/month.");
    }
  }
  if (workload.corpus_pages >= 50_000 && (lid === "level3_multimodal" || lid === "level4_agentic")) {
    if (workload.monthly_queries >= 50_000) {
      score -= 8;
      tradeoffs.push("BigQuery allows 1 vector index/table, so unindexed multimodal brute-force scans become costly at >=50k pages.");
    }
  }

  if (lid === "level4_agentic" && workload.synthesis_model.startsWith("claude-")) {
    tradeoffs.unshift(
      "Level 8 ADK requires a Gemini 4 worker (Hydrogen, Carbon, or Argon); Claude models are judge-only on ADK."
    );
  }

  if (lid === "rag_engine" && tradeoffs.length === 0) {
    tradeoffs.push("Default top-10 retrieval without Ranking API lowers context precision; add rerank in Workbench.");
  }
  if (lid === "managed_vertex" && tradeoffs.length === 0) {
    tradeoffs.push("Prefer selectable Gemini over extractive segments; built-in VAIS summary is more conservative and abstains more often.");
  }
  if (lid === "level3_multimodal" && tradeoffs.length === 0) {
    tradeoffs.push("Adds one-time Document AI Layout Parser + Gemini Vision ingestion cost and higher query latency than warm ScaNN.");
  }
  if (lid === "level4_agentic" && tradeoffs.length === 0) {
    tradeoffs.push("Multi-turn ADK tool execution adds token cost and higher end-to-end latency than single-pass pipelines.");
  }
  if (lid === "level2_hybrid" && tradeoffs.length === 0) {
    tradeoffs.push("Text-only chunking lacks Document AI table reconstruction and multi-hop calculator tools.");
  }
  if (lid === "vertex_vector_v2" && tradeoffs.length === 0) {
    tradeoffs.push(`Carries $${VVS2_BASE_REPLICA_MONTHLY_USD.toFixed(2)}/mo minReplicaCount=1 standing compute unless torn down.`);
  }
  if (lid === "alloydb_scann" && tradeoffs.length === 0) {
    tradeoffs.push(`Carries $${ALLOYDB_BASE_MONTHLY_USD.toFixed(2)}/mo Zonal primary compute (~2x for Regional HA).`);
  }

  return {
    fitScore: Math.max(10, Math.min(98, score)),
    rationale: rationale.slice(0, 3),
    tradeoffs: tradeoffs.slice(0, 2),
  };
}

function fallbackPriority(snap: RecordedCohortSnapshot): number {
  const rank = snap.synthesis_model === "gemini-4-argon" ? 2 : snap.synthesis_model === "gemini-3.8-flash" ? 1 : 0;
  return rank * 10_000 + snap.runs;
}

function finiteOrNull(val: unknown): number | null {
  return typeof val === "number" && Number.isFinite(val) ? val : null;
}

/** Extract exact `(engine|synthesis_model)` and best-available fallback cohorts from `LeaderboardResponse`. */
export function extractRecordedCohorts(leaderboard: LeaderboardResponse | null | undefined): {
  readonly exactCohorts: ReadonlyMap<string, RecordedCohortSnapshot>;
  readonly fallbackCohorts: ReadonlyMap<string, RecordedCohortSnapshot>;
} {
  const exact = new Map<string, RecordedCohortSnapshot>();
  const fallback = new Map<string, RecordedCohortSnapshot>();

  if (!leaderboard || !Array.isArray(leaderboard.groups)) {
    return { exactCohorts: exact, fallbackCohorts: fallback };
  }

  for (const group of leaderboard.groups) {
    if (!group || group.pair_known === false) continue;
    const synth = typeof group.synthesis_model === "string" ? group.synthesis_model.trim() : "";
    const judge = typeof group.judge_model === "string" ? group.judge_model.trim() : "";
    if (!synth || !judge || !Array.isArray(group.rows)) continue;

    for (const row of group.rows) {
      const engine = typeof row?.engine === "string" ? row.engine.trim() : "";
      const runs = typeof row?.runs === "number" ? row.runs : 0;
      if (!engine || runs <= 0) continue;

      const snap: RecordedCohortSnapshot = {
        synthesis_model: synth,
        judge_model: judge,
        runs,
        questions_covered: typeof row.questions_covered === "number" ? row.questions_covered : 0,
        avg_qa_relevance: finiteOrNull(row.avg_qa_relevance),
        avg_groundedness: finiteOrNull(row.avg_groundedness),
        avg_context_precision: finiteOrNull(row.avg_context_precision),
        avg_battle_score: finiteOrNull(row.avg_battle_score),
        avg_latency_ms: finiteOrNull(row.avg_latency_ms),
        avg_cost_usd: finiteOrNull(row.avg_cost_usd),
      };

      const key = `${engine}|${synth}`;
      const prev = exact.get(key);
      if (
        !prev ||
        (judge === "claude-opus-5-5" && prev.judge_model !== "claude-opus-5-5") ||
        snap.runs > prev.runs
      ) {
        exact.set(key, snap);
      }

      const prevFb = fallback.get(engine);
      if (!prevFb || fallbackPriority(snap) > fallbackPriority(prevFb)) {
        fallback.set(engine, snap);
      }
    }
  }

  return { exactCohorts: exact, fallbackCohorts: fallback };
}

export interface EstimateContext {
  readonly levels?: readonly LevelDescriptor[];
  readonly leaderboard?: LeaderboardResponse | null;
}

/** Pure deterministic monthly TCO + workload-fit estimator across all 8 architectures (`L1..L8`). */
export function estimateEnterpriseTco(workload: TcoWorkload, context: EstimateContext = {}): EnterpriseTcoEstimate {
  const validated = parseTcoWorkload(workload as unknown as Record<string, unknown>);
  const synthRate = MODEL_RATES[validated.synthesis_model];
  const rankRate = MODEL_RATES["discovery-engine-rank"];
  const vaisRate = MODEL_RATES["vertex-ai-search-enterprise"];
  const visionRate = MODEL_RATES["gemini-4-argon"];

  const crossover = computeCrossover(validated);
  const pdfGib = (validated.corpus_pages * PDF_BYTES_PER_PAGE) / BYTES_PER_GIB;
  const gcsMonthly = pdfGib * GCS_STANDARD_USD_PER_GIB_MONTH;
  const diagRatio = diagramPageRatio(validated.doc_complexity);

  const labelByLevel = new Map<string, string>();
  for (const lvl of context.levels ?? []) {
    if (lvl?.level_id && lvl?.label) {
      labelByLevel.set(lvl.level_id, lvl.label);
    }
  }
  const { exactCohorts, fallbackCohorts } = extractRecordedCohorts(context.leaderboard);

  const prelim = LEVEL_META.map((meta) => {
    const lid = meta.level_id;
    const label = labelByLevel.get(lid) ?? meta.default_label;

    let units = 0;
    let standingUsd = 0;
    let standingNote = "$0.00/mo serverless idle compute (pay strictly per query + active storage).";

    if (lid === "alloydb_scann") {
      units = Math.max(1, Math.ceil(validated.monthly_queries / ALLOYDB_QUERIES_PER_UNIT_MONTH));
      standingUsd = round2(units * ALLOYDB_BASE_MONTHLY_USD);
      standingNote =
        `${units}x 2-vCPU / 16 GiB Zonal AlloyDB primary ($${ALLOYDB_BASE_MONTHLY_USD.toFixed(2)}/mo per unit; ` +
        `Regional HA ~2x; $0/mo idle when torn down via scripts/ephemeral_alloydb.sh).`;
    } else if (lid === "vertex_vector_v2") {
      units = Math.max(1, Math.ceil(validated.monthly_queries / VVS2_QUERIES_PER_REPLICA_MONTH));
      standingUsd = round2(units * VVS2_BASE_REPLICA_MONTHLY_USD);
      standingNote =
        `${units}x PERFORMANCE_OPTIMIZED replica (minReplicaCount=${units}, $${VVS2_BASE_REPLICA_MONTHLY_USD.toFixed(2)}/mo ` +
        `per replica; $0/mo idle when torn down via scripts/ephemeral_vvs2.sh).`;
    } else if (lid === "rag_engine") {
      standingNote =
        `$0.00/mo on RagManagedDb Serverless ($0/query retrieveContexts fee); optional Spanner Basic tier ` +
        `adds ~$${RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD.toFixed(2)}/mo (100 PU) for dedicated enterprise SLAs.`;
    }

    let storageUsd: number;
    if (lid === "level1_naive") {
      const chunkGib = (validated.corpus_pages * 250 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + chunkGib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH;
    } else if (lid === "level2_hybrid") {
      const chunkGib = (validated.corpus_pages * 28 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + chunkGib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH;
    } else if (lid === "level3_multimodal" || lid === "level4_agentic") {
      const chunkGib = (validated.corpus_pages * 62 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + chunkGib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH;
    } else if (lid === "managed_vertex") {
      const billableVaisGib = Math.max(0, pdfGib - VAIS_FREE_STORAGE_GIB);
      storageUsd = gcsMonthly + billableVaisGib * VAIS_STORAGE_USD_PER_GIB_MONTH;
    } else if (lid === "rag_engine") {
      const ragGib = (validated.corpus_pages * 32 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + ragGib * RAG_ENGINE_STORAGE_USD_PER_GIB_MONTH;
    } else if (lid === "alloydb_scann") {
      const hybridGib = (validated.corpus_pages * 28 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + Math.max(ALLOYDB_MIN_STORAGE_GIB, hybridGib * 1.5) * ALLOYDB_STORAGE_USD_PER_GIB_MONTH;
    } else {
      const hybridGib = (validated.corpus_pages * 28 * 1024) / BYTES_PER_GIB;
      storageUsd = gcsMonthly + Math.max(VVS2_MIN_STORAGE_GIB, hybridGib * 1.3) * VVS2_STORAGE_USD_PER_GIB_MONTH;
    }
    storageUsd = round2(storageUsd);

    const embeddingUsd = round2(
      meta.embedding_model
        ? ((validated.monthly_queries * meta.query_embed_tokens) / 1_000_000) * MODEL_RATES[meta.embedding_model].inPer1M
        : 0
    );

    let retrievalScanUsd = 0;
    if (
      lid === "level1_naive" ||
      lid === "level2_hybrid" ||
      lid === "level3_multimodal" ||
      lid === "level4_agentic"
    ) {
      const scanBytesPerQ = bqScanBytesPerQuery(lid, validated.corpus_pages);
      retrievalScanUsd = ((validated.monthly_queries * scanBytesPerQ) / BYTES_PER_TIB) * BQ_SCAN_USD_PER_TIB;
    } else if (lid === "managed_vertex") {
      retrievalScanUsd = (validated.monthly_queries / 1_000) * vaisRate.per1kReq;
    }
    retrievalScanUsd = round2(retrievalScanUsd);

    const rerankUsd = round2(((validated.monthly_queries * meta.rank_counts_per_query) / 1_000) * rankRate.per1kReq);
    const synthesisUsd = round2(
      ((validated.monthly_queries * meta.prompt_tokens_per_query) / 1_000_000) * synthRate.inPer1M +
        ((validated.monthly_queries * meta.output_tokens_per_query) / 1_000_000) * synthRate.outPer1M
    );
    const queryVariableUsd = round2(embeddingUsd + retrievalScanUsd + rerankUsd + synthesisUsd);
    const totalMonthlyUsd = round2(standingUsd + storageUsd + queryVariableUsd);
    const costPer1kUsd = round2((totalMonthlyUsd / validated.monthly_queries) * 1_000);

    const ingestEmbedUsd =
      meta.embedding_model && meta.ingest_embed_tokens_per_page > 0
        ? ((validated.corpus_pages * meta.ingest_embed_tokens_per_page) / 1_000_000) *
          MODEL_RATES[meta.embedding_model].inPer1M
        : 0;

    let oneTimeIngestUsd: number;
    let ingestNote: string;
    if (lid === "level3_multimodal" || lid === "level4_agentic") {
      const docaiUsd = (validated.corpus_pages / 1_000) * DOCAI_LAYOUT_USD_PER_1K_PAGES;
      const diagramPages = validated.corpus_pages * diagRatio;
      const visionUsd =
        ((diagramPages * VISION_PROMPT_TOKENS_PER_DIAGRAM_PAGE) / 1_000_000) * visionRate.inPer1M +
        ((diagramPages * VISION_OUTPUT_TOKENS_PER_DIAGRAM_PAGE) / 1_000_000) * visionRate.outPer1M;
      oneTimeIngestUsd = round2(docaiUsd + visionUsd + ingestEmbedUsd);
      ingestNote =
        `Document AI Layout Parser ($10/1k pages = $${docaiUsd.toFixed(2)}) + Gemini Vision on ~${Math.round(
          diagRatio * 100
        )}% diagram pages ($${visionUsd.toFixed(2)}) + embeddings ($${ingestEmbedUsd.toFixed(2)}).`;
    } else if (lid === "managed_vertex") {
      oneTimeIngestUsd = 0;
      ingestNote = "$0.00 separate parse/embedding fee on standard digital PDF import into Vertex AI Search.";
    } else if (lid === "rag_engine") {
      oneTimeIngestUsd = round2(ingestEmbedUsd);
      ingestNote = "Managed RagCorpus PDF chunking ($0 parse fee) + text-embedding-005 corpus embeddings.";
    } else {
      oneTimeIngestUsd = round2(ingestEmbedUsd);
      ingestNote = "PyMuPDF text extraction ($0 parse fee) + gemini-embedding-2 3072-d vector indexing.";
    }

    const fit = scoreWorkloadFit(meta, validated, crossover);
    const exactCohort = exactCohorts.get(`${lid}|${validated.synthesis_model}`) ?? null;
    const candidateAlt = fallbackCohorts.get(lid) ?? null;
    const altCohort =
      exactCohort === null && candidateAlt !== null && candidateAlt.synthesis_model !== validated.synthesis_model
        ? candidateAlt
        : null;

    return {
      meta,
      label,
      units,
      standingUsd,
      standingNote,
      storageUsd,
      embeddingUsd,
      retrievalScanUsd,
      rerankUsd,
      synthesisUsd,
      queryVariableUsd,
      totalMonthlyUsd,
      costPer1kUsd,
      oneTimeIngestUsd,
      ingestNote,
      fit,
      exactCohort,
      altCohort,
    };
  });

  const ranked = [...prelim]
    .filter((item) => item.meta.level_id !== "level1_naive")
    .sort((a, b) => {
      if (b.fit.fitScore !== a.fit.fitScore) return b.fit.fitScore - a.fit.fitScore;
      if (a.totalMonthlyUsd !== b.totalMonthlyUsd) return a.totalMonthlyUsd - b.totalMonthlyUsd;
      return a.meta.order - b.meta.order;
    });

  const recommendedId = ranked[0].meta.level_id;
  const runnerUpId = ranked[1].meta.level_id;

  const architectures: ArchitectureTcoRow[] = prelim.map((item) => {
    const lid = item.meta.level_id;
    let fitTier: FitTier;
    if (lid === recommendedId) {
      fitTier = "recommended";
    } else if (lid === runnerUpId) {
      fitTier = "runner_up";
    } else if (lid === "level1_naive") {
      fitTier = "baseline";
    } else if (item.fit.fitScore < 45) {
      fitTier = "not_recommended";
    } else {
      fitTier = "viable";
    }
    return {
      level_id: lid,
      order: item.meta.order,
      label: item.label,
      short_label: item.meta.short_label,
      gcp_services: item.meta.gcp_services,
      idle_mode: item.meta.idle_mode,
      capacity_units: item.units,
      standing_monthly_usd: item.standingUsd,
      standing_note: item.standingNote,
      storage_monthly_usd: item.storageUsd,
      embedding_monthly_usd: item.embeddingUsd,
      retrieval_scan_monthly_usd: item.retrievalScanUsd,
      rerank_monthly_usd: item.rerankUsd,
      synthesis_monthly_usd: item.synthesisUsd,
      query_variable_monthly_usd: item.queryVariableUsd,
      total_monthly_usd: item.totalMonthlyUsd,
      cost_per_1k_queries_usd: item.costPer1kUsd,
      one_time_ingestion_usd: item.oneTimeIngestUsd,
      ingestion_note: item.ingestNote,
      fit_score: item.fit.fitScore,
      fit_tier: fitTier,
      rationale: item.fit.rationale,
      tradeoffs: item.fit.tradeoffs,
      recorded_cohort: item.exactCohort,
      alternate_cohort: item.altCohort,
    };
  });

  return {
    workload: validated,
    recommended_level_id: recommendedId,
    runner_up_level_id: runnerUpId,
    crossover,
    architectures,
  };
}

/** Sort TCO rows deterministically by fit score, monthly TCO, recorded QA relevance, or canonical level order. */
export function sortArchitectures(
  rows: readonly ArchitectureTcoRow[],
  sortBy: TcoSortKey
): ArchitectureTcoRow[] {
  const copy = [...rows];
  if (sortBy === "monthly_tco") {
    return copy.sort((a, b) => a.total_monthly_usd - b.total_monthly_usd || b.fit_score - a.fit_score);
  }
  if (sortBy === "recorded_qa") {
    return copy.sort((a, b) => {
      const qaA = a.recorded_cohort?.avg_qa_relevance ?? a.alternate_cohort?.avg_qa_relevance ?? -1;
      const qaB = b.recorded_cohort?.avg_qa_relevance ?? b.alternate_cohort?.avg_qa_relevance ?? -1;
      return qaB - qaA || b.fit_score - a.fit_score;
    });
  }
  if (sortBy === "fit_score") {
    return copy.sort((a, b) => b.fit_score - a.fit_score || a.total_monthly_usd - b.total_monthly_usd);
  }
  return copy.sort((a, b) => a.order - b.order);
}

/**
 * Compute the `unchecked` level list for `COMPARE_SESSION` so clicking "Compare Top Candidates"
 * pre-selects only the desired enabled candidate levels on `/compare`.
 */
export function uncheckedForCompareCandidates(
  candidateIds: readonly string[],
  levels: readonly LevelDescriptor[]
): string[] {
  const enabledSet = new Set(levels.filter((l) => l.enabled).map((l) => l.level_id));
  const validCandidates =
    enabledSet.size > 0 ? candidateIds.filter((id) => enabledSet.has(id as LevelId)) : [...candidateIds];
  const wanted = new Set(validCandidates.length > 0 ? validCandidates : candidateIds);
  return LEVEL_IDS.filter((id) => !wanted.has(id));
}

export function formatUsdMonthly(usd: number): string {
  if (!Number.isFinite(usd)) return "—";
  if (usd >= 1_000) {
    return `$${usd.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
  }
  return `$${usd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatCompactInt(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1_000_000) {
    const m = value / 1_000_000;
    return `${Number.isInteger(m) ? m.toFixed(0) : m.toFixed(1)}M`;
  }
  if (value >= 1_000) {
    const k = value / 1_000;
    return `${Number.isInteger(k) ? k.toFixed(0) : k.toFixed(1)}k`;
  }
  return String(Math.round(value));
}

export function formatLatencySeconds(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "—";
  return `${(ms / 1_000).toFixed(1)}s`;
}
