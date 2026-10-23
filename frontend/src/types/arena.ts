/**
 * Shared frontend contracts for The RAG Arena v3.0.
 * Mirrors spec.md §3 (stages), §6.3 (SSE), §7.2.1 (architecture descriptors).
 * Nothing here carries timings or scores; those arrive only via SSE events.
 */

// ---------------------------------------------------------------------------
// Levels & stages
// ---------------------------------------------------------------------------

export const LEVEL_IDS = [
  "level1_naive",
  "level2_hybrid",
  "level3_multimodal",
  "level4_agentic",
  "managed_vertex",
] as const;
export type LevelId = (typeof LEVEL_IDS)[number];

export const QUERY_STAGE_IDS = [
  "query_prep",
  "embed_query",
  "retrieve",
  "rerank",
  "agent_turn",
  "synthesize",
  "evaluate",
] as const;
export type QueryStageId = (typeof QUERY_STAGE_IDS)[number];

export const INGEST_STAGE_IDS = [
  "discover_docs",
  "parse",
  "chunk",
  "embed",
  "load_bigquery",
  "build_indexes",
  "verify_counts",
] as const;
export type IngestStageId = (typeof INGEST_STAGE_IDS)[number];

/** Any canonical stage id; unknown ids from the backend are tolerated as plain strings. */
export type StageId = QueryStageId | IngestStageId | (string & {});

export type TierProfile = "naive" | "hybrid" | "multimodal";

/** Free-form JSON object (request/payload bodies are backend-defined). */
export type JsonObject = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Architecture descriptor (spec §7.2.1) — design metadata only
// ---------------------------------------------------------------------------

export type ArchNodeKind = "actor" | "model" | "store" | "api" | "compute" | "index" | "agent" | "process";

export interface ArchNode {
  readonly id: string;
  readonly label: string;
  readonly service: string;
  readonly kind: ArchNodeKind;
  readonly col: number;
  readonly row: number;
  readonly stage_ids: readonly string[];
  readonly region?: string;
  readonly code_ref?: string;
  /** Level 4: ADK tools this node represents; it lights up while one of their tool calls is open. */
  readonly tool_names?: readonly string[];
}

export interface ArchEdge {
  readonly from: string;
  readonly to: string;
  readonly label?: string;
}

export interface ArchitectureDescriptor {
  readonly level_id: LevelId;
  readonly nodes: readonly ArchNode[];
  readonly edges: readonly ArchEdge[];
  readonly ingest_nodes: readonly ArchNode[];
  readonly ingest_edges: readonly ArchEdge[];
}

export interface LevelDescriptor {
  readonly level_id: LevelId;
  readonly label: string;
  readonly order: number;
  readonly enabled: boolean;
  readonly stages: readonly string[];
  readonly architecture: ArchitectureDescriptor;
  /** Client-side only (DEV `?mock=1`): unlocked to play a fixture; the backend has it disabled. */
  readonly dev_fixture?: boolean;
}

export interface LevelsResponse {
  readonly levels: readonly LevelDescriptor[];
}

// ---------------------------------------------------------------------------
// Health / benchmarks / ingest status (REST)
// ---------------------------------------------------------------------------

export type DemoMode = "live" | "record" | "replay";

export interface HealthResponse {
  readonly status: string;
  readonly demo_mode: DemoMode | string;
  readonly project_id?: string | null;
  readonly gcs_bucket?: string | null;
  readonly enabled_levels?: readonly string[];
  readonly gemini_fast_model?: string;
  readonly gemini_pro_model?: string;
}

export type FailureModeCategory =
  | "exact_clause_and_clin"
  | "table_matrix_extraction"
  | "diagram_topology"
  | "multi_hop_amendment_and_math";

export interface BenchmarkQuestion {
  readonly question_id: string;
  readonly question: string;
  readonly failure_mode_category: FailureModeCategory | string;
  readonly target_solicitation: string;
  readonly expected_tier_winner: string;
  readonly ground_truth_answer?: string;
}

export interface BenchmarksResponse {
  readonly benchmark_version?: string;
  readonly domain?: string;
  readonly failure_mode_categories?: readonly {
    readonly id: string;
    readonly label: string;
    readonly description?: string;
  }[];
  readonly questions: readonly BenchmarkQuestion[];
}

export interface IngestRunSummary {
  readonly run_id: string;
  readonly level: string;
  readonly tier_profile: string;
  readonly started_at?: string | null;
  readonly finished_at: string | null;
  readonly status: string;
  readonly docs?: number;
  readonly pages?: number;
  readonly chunks: number;
  readonly embedded?: number;
  readonly loaded?: number;
  readonly embedding_model?: string;
  readonly layout_source?: string;
  readonly git_sha?: string;
}

export interface IngestStatusResponse {
  readonly counts_by_tier: Record<string, number>;
  readonly index_states: Record<string, string>;
  readonly last_runs: readonly IngestRunSummary[];
  /** false when the backend cannot answer (e.g. replay mode without BigQuery); `reason` explains why. */
  readonly available?: boolean;
  readonly reason?: string | null;
  readonly demo_mode?: string;
  readonly last_runs_source?: string;
  /** Fully-qualified BigQuery table the counts were read from (server-provided; never hardcoded). */
  readonly table?: string;
  /** Level 5 (provisional): Vertex AI Search data store status; absent on pre-Level-5 backends. */
  readonly managed_vertex?: ManagedVertexIngestStatus | null;
}

/** `GET /api/v1/ingest/status` → `managed_vertex` (provisional; every field optional, shown verbatim). */
export interface ManagedVertexIngestStatus {
  readonly document_count?: number | null;
  readonly data_store_state?: string | null;
  readonly engine_state?: string | null;
  readonly indexed?: number | null;
  readonly search_tier?: string | null;
  /** Resource name of the data store / engine (masked in safe mode). */
  readonly data_store?: string | null;
  readonly engine?: string | null;
  readonly available?: boolean;
  readonly reason?: string | null;
}

export interface IngestJobCreated {
  readonly job_id: string;
}

// ---------------------------------------------------------------------------
// Model catalog (GET /api/v1/models) — prices and availability come only from the API
// ---------------------------------------------------------------------------

export type ModelRole = "synthesis" | "judge";
export type ModelProvider = "vertex_gemini" | "anthropic_vertex" | "vertex_ai_search" | (string & {});

export interface ModelCatalogEntry {
  readonly id: string;
  readonly label: string;
  readonly provider: ModelProvider;
  readonly location: string;
  readonly roles: readonly (ModelRole | (string & {}))[];
  readonly input_usd_per_1m: number | null;
  readonly output_usd_per_1m: number | null;
  readonly price_source: string | null;
  readonly price_as_of: string | null;
  readonly available: boolean;
  readonly unavailable_reason: string | null;
  readonly verified_at: string | null;
  /** Level ids this model is valid on (empty/absent = every level); backend key `allowed_levels`, `levels` tolerated. */
  readonly allowed_levels?: readonly string[] | null;
  readonly levels?: readonly string[] | null;
  /** Request-priced managed models (Vertex AI Search summary): USD per 1,000 requests. */
  readonly usd_per_1k_requests?: number | null;
}

export interface RecordedPair {
  readonly engine: string;
  readonly synthesis_model: string;
  readonly judge_model: string;
  readonly question_ids: readonly string[];
}

export interface ModelsResponse {
  readonly models: readonly ModelCatalogEntry[];
  readonly defaults: { readonly synthesis_model: string; readonly judge_model: string };
  readonly demo_mode: DemoMode | string;
  readonly recorded_pairs: readonly RecordedPair[];
}

// ---------------------------------------------------------------------------
// Leaderboard (GET /api/v1/leaderboard) — tolerant of the pre-picker shape (rows without models)
// ---------------------------------------------------------------------------

export interface LeaderboardProvenance {
  readonly question_id?: string | null;
  readonly recorded_at?: string | null;
  readonly git_sha?: string | null;
  readonly model_ids?: unknown;
  readonly synthetic_dev_fixture?: boolean;
}

export interface LeaderboardRow {
  readonly engine: string;
  readonly synthesis_model?: string | null;
  readonly judge_model?: string | null;
  readonly runs?: number;
  readonly provenance?: readonly LeaderboardProvenance[];
  readonly [metric: string]: unknown;
}

export interface LeaderboardResponse {
  readonly source?: string;
  readonly engines?: readonly LeaderboardRow[];
  readonly rows?: readonly LeaderboardRow[];
}

// ---------------------------------------------------------------------------
// Documents & citations (kept from v2; bbox normalized 0–1)
// ---------------------------------------------------------------------------

export interface BoundingBox {
  readonly page: number;
  readonly x0: number;
  readonly y0: number;
  readonly x1: number;
  readonly y1: number;
}

export interface DocumentChunk {
  readonly chunk_id: string;
  readonly tier_profile: string;
  readonly solicitation_id: string;
  readonly doc_name: string;
  readonly doc_type: string;
  readonly element_type: string;
  readonly section_id: string | null;
  readonly amendment_number: number;
  readonly effective_date: string | null;
  readonly amends_section: string | null;
  readonly page_number: number;
  readonly bbox: BoundingBox;
  readonly content: string;
  readonly score: number;
}

export interface Citation {
  readonly chunk_id: string;
  readonly doc_name: string;
  readonly solicitation_id: string;
  readonly page_number: number;
  readonly section_id: string | null;
  readonly element_type: string;
  readonly bbox: BoundingBox;
  readonly excerpt: string;
  /** Level 3: service that produced the element ("documentai_layout" | "gemini_vision" | "pymupdf"). */
  readonly layout_source?: string | null;
  /** Level 3: who located `bbox` — "documentai" | "pdf_text_layer" | "page" (whole page, not located). */
  readonly bbox_source?: string | null;
  /** Level 3 (provisional): Document AI / vision region bbox when sent separately from `bbox`. */
  readonly region_bbox?: BoundingBox | null;
}

// ---------------------------------------------------------------------------
// Battle SSE protocol (spec §6.3)
// ---------------------------------------------------------------------------

export interface EvalMetrics {
  readonly groundedness: number;
  readonly qa_relevance: number;
  readonly context_precision: number;
}

export interface TokenUsage {
  readonly prompt_tokens: number;
  readonly output_tokens: number;
  readonly embedding_tokens: number;
  /** Synthesis model id (catalog id) that produced the answer. */
  readonly model?: string | null;
}

export interface EngineFlags {
  readonly reranker_used: boolean;
  readonly adk_runner_used: boolean;
  readonly layout_source: string | null;
}

interface BaseEvent {
  readonly run_id: string;
  readonly ts: string;
}

export interface RunStartedEvent extends BaseEvent {
  readonly event: "run_started";
  readonly engines: readonly string[];
  readonly demo_mode: string;
  readonly question_id: string | null;
  /** Catalog ids of the models this run actually uses (absent on pre-picker backends/recordings). */
  readonly synthesis_model?: string | null;
  readonly judge_model?: string | null;
}

/** Replay-mode frames carry `replayed: true` and the original `recorded_ts`. */
export interface ReplayMarkers {
  readonly replayed?: boolean;
  readonly recorded_ts?: string;
}

export interface StageStartedEvent extends BaseEvent, ReplayMarkers {
  readonly event: "stage_started";
  readonly engine: string;
  readonly stage: StageId;
  readonly label: string;
  readonly service: string;
  readonly request: JsonObject;
}

export interface StageCompletedEvent extends BaseEvent, ReplayMarkers {
  readonly event: "stage_completed";
  readonly engine: string;
  readonly stage: StageId;
  readonly duration_ms: number;
  readonly payload: JsonObject;
}

export interface StageFailedEvent extends BaseEvent, ReplayMarkers {
  readonly event: "stage_failed";
  readonly engine: string;
  readonly stage: StageId;
  readonly duration_ms: number;
  readonly error_type: string;
  readonly error: string;
}

export interface StageSkippedEvent extends BaseEvent {
  readonly event: "stage_skipped";
  readonly engine: string;
  readonly stage: StageId;
  readonly reason: string;
}

/**
 * Level 4: one ADK event, forwarded the moment it happens. `kind` splits a model turn, a tool call and its result;
 * legacy frames without `kind` (spec §6.3) describe one finished tool call. All fields beyond the base are optional.
 */
export interface AgentTurnEvent extends BaseEvent, ReplayMarkers {
  readonly event: "agent_turn";
  readonly engine: string;
  readonly turn_index?: number;
  readonly kind?: "model" | "tool_call" | "tool_result";
  readonly tool_name?: string | null;
  readonly tool_args?: JsonObject | string | null;
  readonly tool_result_summary?: string | null;
  /** Structured tool result (e.g. calculator value, verify_citations claims) when the backend sends it. */
  readonly tool_result?: JsonObject | null;
  readonly duration_ms?: number | null;
  readonly tokens?: number | JsonObject | null;
  readonly call_id?: string | null;
  readonly repair?: boolean;
  readonly status?: string | null;
  readonly text_preview?: string | null;
}

export interface AnswerCompleteEvent extends BaseEvent {
  readonly event: "answer_complete";
  readonly engine: string;
  readonly answer: string;
  readonly citations: readonly Citation[];
  readonly latency_ms: number;
  readonly cost_usd: number;
  readonly usage: TokenUsage;
  readonly flags: EngineFlags;
  /** Chunks the engine retrieved (full `content`; citation excerpts are capped at 240 chars). */
  readonly retrieved_chunks?: readonly JsonObject[];
}

/** Minimal view of a retrieved chunk kept on the timeline (full text for the diagram transcription box). */
export interface RetrievedChunkText {
  readonly chunk_id: string;
  readonly content: string;
}

export interface EvalCompleteEvent extends BaseEvent {
  readonly event: "eval_complete";
  readonly engine: string;
  readonly metrics: EvalMetrics;
  readonly judge_model: string;
  readonly reference_answer_used: boolean;
  readonly duration_ms: number;
  readonly judge_provider?: string;
  readonly judge_location?: string;
  readonly input_tokens?: number;
  readonly output_tokens?: number;
  readonly judge_cost_usd?: number;
  readonly run_cost_usd?: number;
  readonly rationale?: string;
}

export interface EngineErrorEvent extends BaseEvent {
  readonly event: "engine_error";
  readonly engine: string;
  readonly error_type: string;
  readonly error: string;
  readonly failed_stage: string | null;
}

export interface StreamWarningEvent {
  readonly event: "stream_warning";
  readonly run_id: string | null;
  readonly message: string;
  readonly ts: string;
}

export interface BattleCompleteEvent extends BaseEvent {
  readonly event: "battle_complete";
  readonly engines_completed: readonly string[];
  readonly engines_failed: readonly string[];
}

export type BattleEvent =
  | RunStartedEvent
  | StageStartedEvent
  | StageCompletedEvent
  | StageFailedEvent
  | StageSkippedEvent
  | AgentTurnEvent
  | AnswerCompleteEvent
  | EvalCompleteEvent
  | EngineErrorEvent
  | StreamWarningEvent
  | BattleCompleteEvent;

export type BattleEventType = BattleEvent["event"];

// ---------------------------------------------------------------------------
// Ingestion SSE protocol (spec §6.3) — `job_id` instead of `run_id`, `engine` = level
// ---------------------------------------------------------------------------

interface BaseJobEvent {
  readonly job_id: string;
  readonly ts: string;
}

export interface JobStartedEvent extends BaseJobEvent {
  readonly event: "job_started";
  readonly level: string;
  readonly tier_profile: string;
}

export interface JobStageStartedEvent extends BaseJobEvent {
  readonly event: "stage_started";
  readonly engine: string;
  readonly stage: StageId;
  readonly label: string;
  readonly service: string;
  readonly request: JsonObject;
}

export interface JobStageCompletedEvent extends BaseJobEvent {
  readonly event: "stage_completed";
  readonly engine: string;
  readonly stage: StageId;
  readonly duration_ms: number;
  readonly payload: JsonObject;
}

export interface JobStageFailedEvent extends BaseJobEvent {
  readonly event: "stage_failed";
  readonly engine: string;
  readonly stage: StageId;
  readonly duration_ms: number;
  readonly error_type: string;
  readonly error: string;
}

export interface DocProgressEvent extends BaseJobEvent {
  readonly event: "doc_progress";
  readonly doc_name: string;
  readonly pages: number;
  readonly chunks: number;
  /** Level 3 (multimodal tier): Document AI tables / diagram pages found in this document. */
  readonly tables?: number;
  readonly diagrams?: number;
  /** Level 3 parse sub-step ("documentai" | "gemini_vision") and its outcome. */
  readonly step?: string;
  readonly status?: string;
}

export interface EmbedProgressEvent extends BaseJobEvent {
  readonly event: "embed_progress";
  readonly done: number;
  readonly total: number;
  readonly batch_ms: number;
}

export interface JobCompleteEvent extends BaseJobEvent {
  readonly event: "job_complete";
  readonly counts_by_tier: Record<string, number>;
  readonly index_states: Record<string, string>;
}

/** Terminal failure of an ingestion job (backend `ingestion/jobs.py`). */
export interface JobFailedEvent extends BaseJobEvent {
  readonly event: "job_failed";
  readonly engine?: string;
  readonly error_type: string;
  readonly error: string;
  readonly failed_stage: string | null;
}

/**
 * Client-only synthetic event (never sent by the server): the SSE stream ended — reader done,
 * network error, or abort — before its terminal event. Fails every `active` stage loudly.
 */
export interface StreamInterruptedEvent {
  readonly event: "stream_interrupted";
  readonly run_id: string | null;
  readonly reason: string;
  readonly ts: string;
}

export type IngestEvent =
  | JobStartedEvent
  | JobFailedEvent
  | JobStageStartedEvent
  | JobStageCompletedEvent
  | JobStageFailedEvent
  | DocProgressEvent
  | EmbedProgressEvent
  | JobCompleteEvent
  | StreamWarningEvent;

// ---------------------------------------------------------------------------
// Derived timeline (client-side reduction of events; never timer-driven)
// ---------------------------------------------------------------------------

export type StageStatus = "idle" | "active" | "done" | "failed" | "skipped";

export interface StageState {
  readonly status: StageStatus;
  readonly label: string | null;
  readonly service: string | null;
  /** Epoch ms parsed from `stage_started.ts`; drives the live elapsed counter only. */
  readonly startedAt: number | null;
  /** Backend-measured duration; the only duration ever displayed once a stage ends. */
  readonly durationMs: number | null;
  readonly request: JsonObject | null;
  readonly payload: JsonObject | null;
  readonly error: { readonly type: string; readonly message: string } | null;
  readonly skipReason: string | null;
}

/** "tool" = legacy combined call + result in one frame. */
export type AgentTurnKind = "model" | "tool_call" | "tool_result" | "tool";

/** One normalised `agent_turn` frame (see `agentTimelineModel.normalizeAgentTurn`); null = not sent. */
export interface AgentTurnRecord {
  /** Arrival index — the only ordering the timeline uses. */
  readonly seq: number;
  readonly turnIndex: number | null;
  readonly kind: AgentTurnKind;
  readonly toolName: string | null;
  readonly toolArgs: JsonObject | null;
  /** Raw `tool_args` text when it was a string that is not a complete JSON object (e.g. truncated at 1200 chars). */
  readonly toolArgsText: string | null;
  readonly resultSummary: string | null;
  readonly result: JsonObject | null;
  readonly durationMs: number | null;
  readonly tokens: { readonly total: number | null; readonly prompt: number | null; readonly output: number | null; readonly thinking: number | null } | null;
  readonly callId: string | null;
  readonly repair: boolean;
  /** Model text preview (`text_preview`). */
  readonly text: string | null;
  /** tool_result `status` ("ok" | "error") when reported. */
  readonly status: string | null;
  readonly ts: string;
}

export interface EngineAnswer {
  readonly answer: string;
  readonly citations: readonly Citation[];
  /** From `answer_complete.retrieved_chunks` (chunk_id + content only); empty when the server sent none. */
  readonly retrievedChunks: readonly RetrievedChunkText[];
  readonly latencyMs: number;
  readonly costUsd: number;
  readonly usage: TokenUsage;
  readonly flags: EngineFlags;
}

export interface EngineEvaluation {
  readonly metrics: EvalMetrics;
  readonly judgeModel: string;
  readonly referenceAnswerUsed: boolean;
  readonly durationMs: number;
  readonly judgeProvider?: string | null;
  readonly judgeLocation?: string | null;
  readonly inputTokens?: number | null;
  readonly outputTokens?: number | null;
  readonly judgeCostUsd?: number | null;
  readonly runCostUsd?: number | null;
  readonly rationale?: string | null;
}

export interface EngineTimeline {
  readonly engine: string;
  readonly stages: Readonly<Record<string, StageState>>;
  /** Stage ids in the order they were first seen on the stream. */
  readonly stageOrder: readonly string[];
  readonly agentTurns: readonly AgentTurnRecord[];
  readonly answer: EngineAnswer | null;
  readonly evaluation: EngineEvaluation | null;
  readonly error: {
    readonly type: string;
    readonly message: string;
    readonly failedStage: string | null;
  } | null;
}
