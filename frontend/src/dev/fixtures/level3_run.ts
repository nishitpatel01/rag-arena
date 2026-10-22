/**
 * DEV-ONLY Level 3 fixture. Imported exclusively behind `if (import.meta.env.DEV)` so it tree-shakes out
 * of `npm run build`. Every value is a placeholder following the PROVISIONAL Level 3 payload contract
 * (query_prep element_intent; retrieve/rerank rows with element_type / layout_source / bbox_source / amendment fields;
 * synthesize authority_headers + element_mix; citations with element_type + layout_source); every frame is
 * flagged `synthetic_dev_fixture: true` and the Shell shows the "DEV FIXTURE — not real data" banner.
 * Document names / pages point at the real local corpus so the viewer can render pages when a backend runs.
 */

import type { ArchitectureDescriptor, BattleEvent, JsonObject } from "../../types/arena";
import type { TimedFixtureEvent } from "./level1_run";

const RUN_ID = "devfixture-level3-0001";
const ENGINE = "level3_multimodal";
const T0 = Date.parse("2026-10-22T14:00:00.000Z");
const F = true as const;
const RRF_K = 60;
const CANDIDATE_K = 50;
const TOP_N = 5;
const SOL = "solicitation_01";

const ts = (offsetMs: number): string => new Date(T0 + offsetMs).toISOString();

const QUERY =
  "According to the Multi-Region VPC topology diagram in Solicitation 01 (03_Technical_Exhibit_Architecture.pdf) and Amendment 0001, what are the Primary and Failover GCP regions, the Cloud HA VPN BGP ASN, the private connectivity mechanism to BigQuery/Vertex AI, and the amended RTO/RPO targets?";
const PREPARED = QUERY.replace("RTO/RPO", "RTO (Recovery Time Objective)/RPO (Recovery Point Objective)");

type Kind = "narrative_text" | "table" | "diagram";
type Src = "documentai_layout" | "gemini_vision" | "pymupdf";

/** [doc, section, element_type, layout_source, amendment_number, amends_section, page]. */
const DOCS: readonly (readonly [string, string, Kind, Src, number, string | null, number])[] = [
  ["03_Technical_Exhibit_Architecture.pdf", "ARCH-TOPOLOGY-01", "diagram", "gemini_vision", 0, null, 1],
  ["06_Amendment_0001_Scope_Update.pdf", "AMD-0001", "narrative_text", "documentai_layout", 1, "SEC-4.2.1", 1],
  ["05_SLA_and_QASP_Requirements.pdf", "SLA-2", "table", "documentai_layout", 0, null, 2],
  ["01_RFP_Base_Solicitation.pdf", "SEC-4.2.1", "narrative_text", "documentai_layout", 0, null, 1],
  ["07_Amendment_0002_Vendor_QA.pdf", "AMD-0002", "table", "documentai_layout", 2, "SEC-5.1", 1],
  ["02_Statement_of_Work_SOW.pdf", "3.2", "narrative_text", "pymupdf", 0, null, 3],
];

/** 50 synthetic candidates: element metadata cycles through DOCS; ranks follow the Level 2 fixture pattern. */
function buildCandidates(): JsonObject[] {
  let v = 0;
  let l = 0;
  const out: (JsonObject & { rrf_score: number })[] = [];
  for (let k = 0; k < CANDIDATE_K; k += 1) {
    const kind = k % 4;
    const vector_rank = kind === 3 ? null : (v += 1);
    const lexical_rank = kind === 0 || kind === 3 ? (l += 1) : null;
    const [doc_name, section_id, element_type, layout_source, amendment_number, amends_section, page] = DOCS[k % DOCS.length];
    const rrf = (vector_rank ? 1 / (RRF_K + vector_rank) : 0) + (lexical_rank ? 1 / (RRF_K + lexical_rank) : 0);
    out.push({
      chunk_id: `devfix-l3-${String(k + 1).padStart(3, "0")}`,
      doc_name,
      page_number: page + Math.floor(k / DOCS.length) % 2,
      section_id,
      element_type,
      layout_source,
      amendment_number,
      amends_section,
      bbox_source: k % 7 === 6 ? "page" : "pdf_text_layer",
      vector_rank,
      lexical_rank,
      vector_distance: vector_rank ? Number((0.16 + vector_rank * 0.006).toFixed(4)) : null,
      rrf_score: Number(rrf.toFixed(6)),
    });
  }
  return out.sort((a, b) => b.rrf_score - a.rrf_score);
}

const CANDIDATES = buildCandidates();
const cand = (rrfRank: number): JsonObject => CANDIDATES[rrfRank - 1];

/** Rerank picks (RRF rank → rerank rank). Rerank rows intentionally omit element fields (joined from retrieve). */
const RERANK_PICKS: readonly { readonly rrfRank: number; readonly score: number }[] = [
  { rrfRank: 1, score: 0.962 },
  { rrfRank: 2, score: 0.913 },
  { rrfRank: 4, score: 0.744 },
  { rrfRank: 3, score: 0.701 },
  { rrfRank: 11, score: 0.522 },
];

const RERANK_RESULTS: JsonObject[] = RERANK_PICKS.map((p, i) => {
  const c = cand(p.rrfRank);
  return { chunk_id: c.chunk_id, doc_name: c.doc_name, page_number: c.page_number, rrf_rank: p.rrfRank, rrf_score: c.rrf_score, rerank_rank: i + 1, rerank_score: p.score };
});

const SERVICE_RANK = "Discovery Engine Ranking API";
const RANK_MODEL = "semantic-ranker-default@latest";
const SQL = [
  "-- [DEV FIXTURE] metadata pre-filter + hybrid RRF over the multimodal tier",
  "WITH vec AS (SELECT base.chunk_id, ROW_NUMBER() OVER (ORDER BY distance) AS vector_rank, distance AS vector_distance",
  "  FROM VECTOR_SEARCH((SELECT * FROM `@project.rag_arena.document_chunks` WHERE tier_profile = @tier AND element_type IN UNNEST(@element_types)),",
  "    'embedding_multimodal', (SELECT @query_embedding AS embedding), top_k => @candidate_k, distance_type => 'COSINE')),",
  "lex AS (SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY SCORE(content, @lexical_query) DESC) AS lexical_rank",
  "  FROM `@project.rag_arena.document_chunks` WHERE tier_profile = @tier AND SEARCH(content, @lexical_query) LIMIT @candidate_k)",
  "SELECT … ORDER BY rrf_score DESC LIMIT @candidate_k",
].join("\n");

const TRANSCRIPTION = [
  "[DEV FIXTURE transcription] Diagram ARCH-TOPOLOGY-01 — Multi-Region VPC topology.",
  "Components: Primary region us-central1 (GKE, Cloud SQL primary); Failover region us-east4 (warm standby);",
  "Cloud HA VPN + Cloud Router (BGP ASN 64512) ↔ on-premises router (ASN 65001); Cloud Armor WAF at ingress;",
  "Private Service Connect endpoints → BigQuery, Vertex AI.",
  "Flows: on-prem → HA VPN → shared VPC → PSC; us-central1 ⇄ us-east4 replication.",
].join("\n");

type Ev = BattleEvent & { readonly synthetic_dev_fixture: true };
const ev = (e: BattleEvent): Ev => ({ ...e, synthetic_dev_fixture: F }) as Ev;
const base = { run_id: RUN_ID, engine: ENGINE } as const;

export const LEVEL3_DEV_FIXTURE: readonly TimedFixtureEvent[] = [
  { t_rel: 0, event: ev({ event: "run_started", run_id: RUN_ID, engines: [ENGINE], demo_mode: "dev_fixture", question_id: "q_vpc_failover_diagram", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", ts: ts(0) }) },
  { t_rel: 150, event: ev({ ...base, event: "stage_started", stage: "query_prep", label: "Query preparation (expansion + element intent)", service: "engine (local)", request: { original_query: QUERY }, ts: ts(150) }) },
  {
    t_rel: 300,
    event: ev({
      ...base, event: "stage_completed", stage: "query_prep", duration_ms: 3,
      payload: { original_query: QUERY, prepared_query: PREPARED, expansions: [{ term: "RTO", expansion: "Recovery Time Objective" }, { term: "RPO", expansion: "Recovery Point Objective" }], element_intent: ["diagram", "table"], intent_matches: { diagram: ["diagram", "topology"], table: ["targets"] } },
      ts: ts(450),
    }),
  },
  { t_rel: 80, event: ev({ ...base, event: "stage_started", stage: "embed_query", label: "Embed query (multimodal space)", service: "Vertex AI · gemini-embedding-2", request: { model: "gemini-embedding-2", task_type: "RETRIEVAL_QUERY", text_chars: PREPARED.length }, ts: ts(530) }) },
  { t_rel: 800, event: ev({ ...base, event: "stage_completed", stage: "embed_query", duration_ms: 770, payload: { model: "gemini-embedding-2", dimensions: 3072, token_count: 61 }, ts: ts(1330) }) },
  {
    t_rel: 60,
    event: ev({
      ...base, event: "stage_started", stage: "retrieve", label: "Pre-filtered hybrid retrieval (multimodal tier)", service: "BigQuery VECTOR_SEARCH + SEARCH()",
      request: { sql: SQL, params: { tier: "multimodal", element_types: ["diagram", "table", "narrative_text"], candidate_k: CANDIDATE_K, rrf_k: RRF_K }, candidate_k: CANDIDATE_K },
      ts: ts(1390),
    }),
  },
  {
    t_rel: 1400,
    event: ev({
      ...base, event: "stage_completed", stage: "retrieve", duration_ms: 1370,
      payload: { rows_returned: CANDIDATES.length, candidate_k: CANDIDATE_K, bytes_processed: 98_566_144, slot_millis: 4410, cache_hit: false, job_id: "devfixture_job_l3_0001", rrf_k: RRF_K, vector_k: 50, lexical_k: 25, with_both_ranks: 13, embedding_column: "embedding_multimodal", element_types: { diagram: 9, narrative_text: 25, table: 16 }, layout_sources: { gemini_vision: 9, documentai_layout: 33, pymupdf: 8 }, amendment_candidates: 17, candidates: CANDIDATES },
      ts: ts(2760),
    }),
  },
  { t_rel: 50, event: ev({ ...base, event: "stage_started", stage: "rerank", label: "Cross-encoder rerank", service: SERVICE_RANK, request: { service: SERVICE_RANK, model: RANK_MODEL, top_n: CANDIDATE_K, records_sent: CANDIDATES.length }, ts: ts(2810) }) },
  { t_rel: 420, event: ev({ ...base, event: "stage_completed", stage: "rerank", duration_ms: 401, payload: { service_used: SERVICE_RANK, model: RANK_MODEL, candidates_in: CANDIDATES.length, candidates_out: TOP_N, results: RERANK_RESULTS, rank_api_ms: 377 }, ts: ts(3230) }) },
  { t_rel: 40, event: ev({ ...base, event: "stage_started", stage: "synthesize", label: "Answer synthesis (authority-ordered context)", service: "Vertex AI · Gemini 4 Argon (dev fixture)", request: { model: "gemini-4-argon", thinking_level: "high", context_chunks: TOP_N }, ts: ts(3270) }) },
  {
    t_rel: 2400,
    event: ev({
      ...base, event: "stage_completed", stage: "synthesize", duration_ms: 2380,
      payload: {
        model: "gemini-4-argon", thinking_level: "high", prompt_tokens: 3120, output_tokens: 164,
        authority_headers: [
          { chunk_id: cand(2).chunk_id as string, header: "[DEV FIXTURE · AMENDMENT 0001 · effective 2026-03-15 · SUPERSEDES SEC-4.2.1] [TEXT] 06_Amendment_0001_Scope_Update.pdf · p.1 · AMD-0001", amendment_number: 1, effective_date: "2026-03-15", supersedes: ["SEC-4.2.1"], superseded_by: [], element_type: "narrative_text", layout_source: "documentai_layout" },
          { chunk_id: cand(1).chunk_id as string, header: "[DEV FIXTURE · BASE · TECHNICAL_EXHIBIT] [DIAGRAM transcription] 03_Technical_Exhibit_Architecture.pdf · p.1 · ARCH-TOPOLOGY-01", amendment_number: 0, effective_date: "2026-01-15", supersedes: [], superseded_by: [], element_type: "diagram", layout_source: "gemini_vision" },
          { chunk_id: cand(4).chunk_id as string, header: "[DEV FIXTURE · BASE] [TEXT] 01_RFP_Base_Solicitation.pdf · p.1 · SEC-4.2.1", amendment_number: 0, effective_date: "2026-01-15", supersedes: [], superseded_by: ["AMD-0001"], element_type: "narrative_text", layout_source: "documentai_layout" },
        ],
        element_mix: { table: 1, diagram: 2, narrative_text: 2 },
        context_order: "latest amendment first, then rerank order",
      },
      ts: ts(5650),
    }),
  },
  {
    t_rel: 40,
    event: ev({
      ...base, event: "answer_complete",
      answer:
        "[DEV FIXTURE] Primary region **us-central1**, failover **us-east4**; Cloud HA VPN with BGP ASN 64512; Private Service Connect to BigQuery/Vertex AI. Amendment 0001 supersedes SEC-4.2.1: RTO 15 minutes, RPO 0 minutes.",
      citations: [
        { chunk_id: cand(1).chunk_id as string, doc_name: "03_Technical_Exhibit_Architecture.pdf", solicitation_id: SOL, page_number: 1, section_id: "ARCH-TOPOLOGY-01", element_type: "diagram", layout_source: "gemini_vision", bbox_source: "pdf_text_layer", bbox: { page: 1, x0: 0.08, y0: 0.22, x1: 0.92, y1: 0.71 }, excerpt: TRANSCRIPTION },
        { chunk_id: cand(2).chunk_id as string, doc_name: "06_Amendment_0001_Scope_Update.pdf", solicitation_id: SOL, page_number: 1, section_id: "AMD-0001", element_type: "narrative_text", layout_source: "documentai_layout", bbox_source: "pdf_text_layer", bbox: { page: 1, x0: 0.07, y0: 0.3, x1: 0.93, y1: 0.46 }, excerpt: "Clause SEC-4.2.1 is hereby superseded" },
        { chunk_id: cand(3).chunk_id as string, doc_name: "05_SLA_and_QASP_Requirements.pdf", solicitation_id: SOL, page_number: 2, section_id: "SLA-2", element_type: "table", layout_source: "documentai_layout", bbox_source: "page", bbox: { page: 2, x0: 0.07, y0: 0.18, x1: 0.93, y1: 0.52 }, excerpt: "| Metric | Target |\n| RTO | 4 hours |" },
      ],
      latency_ms: 5700, cost_usd: 0.0212,
      usage: { prompt_tokens: 3120, output_tokens: 164, embedding_tokens: 61, model: "gemini-4-argon" },
      flags: { reranker_used: true, adk_runner_used: false, layout_source: "documentai_layout+gemini_vision" },
      ts: ts(5690),
    }),
  },
  { t_rel: 40, event: ev({ ...base, event: "stage_started", stage: "evaluate", label: "LLM-as-a-judge", service: "Vertex AI · Claude Opus 5.5 (dev fixture)", request: { model: "claude-opus-5-5" }, ts: ts(5730) }) },
  { t_rel: 1100, event: ev({ ...base, event: "stage_completed", stage: "evaluate", duration_ms: 1080, payload: { model: "claude-opus-5-5", input_tokens: 2210, output_tokens: 150 }, ts: ts(6830) }) },
  { t_rel: 20, event: ev({ ...base, event: "eval_complete", metrics: { groundedness: 1, qa_relevance: 0.95, context_precision: 0.85 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 1080, rationale: "[DEV FIXTURE] synthetic rationale", ts: ts(6850) }) },
  { t_rel: 20, event: ev({ event: "battle_complete", run_id: RUN_ID, engines_completed: [ENGINE], engines_failed: [], ts: ts(6870) }) },
];

/** Mirrors the expected Level 3 descriptor shape (design metadata only). */
export const LEVEL3_DEV_ARCHITECTURE: ArchitectureDescriptor = {
  level_id: "level3_multimodal",
  nodes: [
    { id: "user", label: "User question", service: "Browser", kind: "actor", col: 0, row: 1, stage_ids: ["query_prep"] },
    { id: "embed", label: "Query embedding", service: "Vertex AI · gemini-embedding-2", kind: "model", col: 1, row: 0, stage_ids: ["embed_query"] },
    { id: "bq", label: "Pre-filter + hybrid RRF", service: "BigQuery VECTOR_SEARCH + SEARCH()", kind: "store", col: 2, row: 1, stage_ids: ["retrieve"] },
    { id: "rank", label: "Cross-encoder rerank", service: SERVICE_RANK, kind: "api", col: 3, row: 1, stage_ids: ["rerank"] },
    { id: "llm", label: "Answer synthesis", service: "Vertex AI · gemini-4-argon", kind: "model", col: 4, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI · claude-opus-5-5", kind: "model", col: 5, row: 1, stage_ids: ["evaluate"] },
  ],
  edges: [
    { from: "user", to: "embed", label: "text" },
    { from: "embed", to: "bq", label: "3072-d vector" },
    { from: "user", to: "bq", label: "element intent" },
    { from: "bq", to: "rank", label: "RRF candidates" },
    { from: "rank", to: "llm", label: "reranked" },
    { from: "llm", to: "judge", label: "answer" },
  ],
  ingest_nodes: [],
  ingest_edges: [],
};
