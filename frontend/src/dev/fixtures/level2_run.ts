/**
 * DEV-ONLY Level 2 fixture. Imported exclusively behind `if (import.meta.env.DEV)` so it tree-shakes out
 * of `npm run build`. Every number here is a placeholder that follows the Level 2 stage payload contract
 * (query_prep expansions, hybrid retrieve candidates with vector/lexical ranks + RRF, Discovery Engine
 * rerank before/after); every frame is flagged `synthetic_dev_fixture: true` and the Shell shows the
 * "DEV FIXTURE — not real data" banner while it plays. `?mock_fail=rerank` swaps the rerank completion for
 * `stage_failed` + `engine_error` to exercise the fail-loud path.
 */

import type { ArchitectureDescriptor, BattleEvent, JsonObject } from "../../types/arena";
import type { TimedFixtureEvent } from "./level1_run";

const RUN_ID = "devfixture-level2-0001";
const ENGINE = "level2_hybrid";
const T0 = Date.parse("2026-10-21T20:00:00.000Z");
const F = true as const;
const RRF_K = 60;
const CANDIDATE_K = 50;
const TOP_N = 5;

function ts(offsetMs: number): string {
  return new Date(T0 + offsetMs).toISOString();
}

const QUERY = "What is the base hourly rate for CLIN-0004-AB under the SOW?";
const PREPARED = "What is the base hourly rate for CLIN (Contract Line Item Number)-0004-AB under the SOW (Statement of Work)?";

const SQL = [
  "WITH vec AS (",
  "  SELECT base.chunk_id, ROW_NUMBER() OVER (ORDER BY distance) AS vector_rank, distance AS vector_distance",
  "  FROM VECTOR_SEARCH((SELECT * FROM `@project.rag_arena.document_chunks` WHERE tier_profile = @tier),",
  "    'embedding', (SELECT @query_embedding AS embedding), top_k => @candidate_k, distance_type => 'COSINE')",
  "), lex AS (",
  "  SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY SCORE(content, @lexical_query) DESC) AS lexical_rank",
  "  FROM `@project.rag_arena.document_chunks`",
  "  WHERE tier_profile = @tier AND SEARCH(content, @lexical_query) LIMIT @candidate_k",
  ")",
  "SELECT c.chunk_id, c.doc_name, c.page_number, c.section_id, vec.vector_rank, lex.lexical_rank, vec.vector_distance,",
  "  COALESCE(1 / (@rrf_k + vec.vector_rank), 0) + COALESCE(1 / (@rrf_k + lex.lexical_rank), 0) AS rrf_score",
  "FROM vec FULL OUTER JOIN lex USING (chunk_id) JOIN `@project.rag_arena.document_chunks` c USING (chunk_id)",
  "ORDER BY rrf_score DESC LIMIT @candidate_k",
].join("\n");

const DOCS = [
  ["04_Pricing_Schedule_CLINs.pdf", "CLIN-0004-AB"],
  ["02_Statement_of_Work.pdf", "3.2"],
  ["07_Amendment_0001.pdf", "A1.4"],
  ["05_SLA_Matrix.pdf", "SLA-2"],
  ["01_RFP_Overview.pdf", "1.1"],
] as const;

interface FixtureCandidate {
  readonly chunk_id: string;
  readonly doc_name: string;
  readonly page_number: number;
  readonly section_id: string;
  readonly vector_rank: number | null;
  readonly lexical_rank: number | null;
  readonly vector_distance: number | null;
  readonly rrf_score: number;
}

/** 50 synthetic candidates: every 4th in both lists, two of four vector-only, one of four lexical-only. */
function buildCandidates(): FixtureCandidate[] {
  let v = 0;
  let l = 0;
  const out: FixtureCandidate[] = [];
  for (let k = 0; k < CANDIDATE_K; k += 1) {
    const kind = k % 4;
    const vector_rank = kind === 3 ? null : (v += 1);
    const lexical_rank = kind === 0 || kind === 3 ? (l += 1) : null;
    const [doc_name, section_id] = DOCS[k % DOCS.length];
    const rrf = (vector_rank ? 1 / (RRF_K + vector_rank) : 0) + (lexical_rank ? 1 / (RRF_K + lexical_rank) : 0);
    out.push({
      chunk_id: `devfix-l2-${String(k + 1).padStart(3, "0")}`,
      doc_name,
      page_number: 1 + (k % 7),
      section_id,
      vector_rank,
      lexical_rank,
      vector_distance: vector_rank ? Number((0.18 + vector_rank * 0.006).toFixed(4)) : null,
      rrf_score: Number(rrf.toFixed(6)),
    });
  }
  return out.sort((a, b) => b.rrf_score - a.rrf_score);
}

const CANDIDATES = buildCandidates();

/** Rerank picks (RRF rank → rerank rank): includes a lift from #9 and #14 into the top 5. */
const RERANK_PICKS: readonly { readonly rrfRank: number; readonly score: number }[] = [
  { rrfRank: 3, score: 0.951 },
  { rrfRank: 1, score: 0.884 },
  { rrfRank: 9, score: 0.713 },
  { rrfRank: 2, score: 0.642 },
  { rrfRank: 14, score: 0.518 },
];

const RERANK_RESULTS: JsonObject[] = RERANK_PICKS.map((p, i) => {
  const c = CANDIDATES[p.rrfRank - 1];
  return {
    chunk_id: c.chunk_id,
    doc_name: c.doc_name,
    page_number: c.page_number,
    rrf_rank: p.rrfRank,
    rrf_score: c.rrf_score,
    rerank_rank: i + 1,
    rerank_score: p.score,
  };
});

const SERVICE_RANK = "Discovery Engine Ranking API";
const RANK_MODEL = "semantic-ranker-default@latest";

type Ev = BattleEvent & { readonly synthetic_dev_fixture: true };
const ev = (e: BattleEvent): Ev => ({ ...e, synthetic_dev_fixture: F }) as Ev;

function head(): TimedFixtureEvent[] {
  return [
    { t_rel: 0, event: ev({ event: "run_started", run_id: RUN_ID, engines: [ENGINE], demo_mode: "dev_fixture", question_id: "q_exact_clin_lookup", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", ts: ts(0) }) },
    { t_rel: 150, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "query_prep", label: "Query preparation (acronym expansion)", service: "engine (local)", request: { original_query: QUERY }, ts: ts(150) }) },
    { t_rel: 300, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "query_prep", duration_ms: 2, payload: { original_query: QUERY, prepared_query: PREPARED, expansions: [{ term: "CLIN", expansion: "Contract Line Item Number" }, { term: "SOW", expansion: "Statement of Work" }] }, ts: ts(450) }) },
    { t_rel: 80, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "embed_query", label: "Embed query", service: "Vertex AI · gemini-embedding-2", request: { model: "gemini-embedding-2", task_type: "RETRIEVAL_QUERY", text_chars: PREPARED.length }, ts: ts(530) }) },
    { t_rel: 900, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "embed_query", duration_ms: 880, payload: { model: "gemini-embedding-2", dimensions: 3072, token_count: 27 }, ts: ts(1430) }) },
    { t_rel: 60, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "retrieve", label: "Hybrid retrieval (VECTOR_SEARCH + SEARCH → RRF)", service: "BigQuery VECTOR_SEARCH + SEARCH()", request: { sql: SQL, params: { tier: "hybrid", candidate_k: CANDIDATE_K, rrf_k: RRF_K, lexical_query: "CLIN-0004-AB base hourly rate" }, candidate_k: CANDIDATE_K }, ts: ts(1490) }) },
    { t_rel: 1500, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "retrieve", duration_ms: 1460, payload: { rows_returned: CANDIDATES.length, candidate_k: CANDIDATE_K, bytes_processed: 131_334_144, slot_millis: 5210, cache_hit: false, job_id: "devfixture_job_l2_0001", rrf_k: RRF_K, candidates: CANDIDATES as unknown as JsonObject[] }, ts: ts(2950) }) },
    { t_rel: 50, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "rerank", label: "Cross-encoder rerank", service: SERVICE_RANK, request: { service: SERVICE_RANK, model: RANK_MODEL, top_n: TOP_N, records_sent: CANDIDATES.length }, ts: ts(3000) }) },
  ];
}

function successTail(): TimedFixtureEvent[] {
  return [
    { t_rel: 450, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "rerank", duration_ms: 431, payload: { service_used: SERVICE_RANK, model: RANK_MODEL, candidates_in: CANDIDATES.length, candidates_out: TOP_N, results: RERANK_RESULTS, rank_api_ms: 402 }, ts: ts(3450) }) },
    { t_rel: 40, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "synthesize", label: "Answer synthesis", service: "Vertex AI · Gemini 4 Argon (dev fixture)", request: { model: "gemini-4-argon", thinking_level: "high", context_chunks: TOP_N }, ts: ts(3490) }) },
    { t_rel: 2600, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "synthesize", duration_ms: 2570, payload: { model: "gemini-4-argon", thinking_level: "high", prompt_tokens: 2104, output_tokens: 88 }, ts: ts(6090) }) },
    {
      t_rel: 40,
      event: ev({
        event: "answer_complete", run_id: RUN_ID, engine: ENGINE,
        answer: "[DEV FIXTURE] CLIN-0004-AB (Cloud Security Architect Labor) is priced at $210.00/hr.",
        citations: [
          { chunk_id: CANDIDATES[2].chunk_id, doc_name: "04_Pricing_Schedule_CLINs.pdf", solicitation_id: "solicitation_01", page_number: 2, section_id: "CLIN-0004-AB", element_type: "table", bbox: { page: 2, x0: 0.08, y0: 0.31, x1: 0.92, y1: 0.47 }, excerpt: "CLIN-0004-AB | Cloud Security Architect Labor | $210.00/hr" },
        ],
        latency_ms: 6130, cost_usd: 0.0141,
        usage: { prompt_tokens: 2104, output_tokens: 88, embedding_tokens: 27, model: "gemini-4-argon" },
        flags: { reranker_used: true, adk_runner_used: false, layout_source: "pymupdf" },
        ts: ts(6130),
      }),
    },
    { t_rel: 40, event: ev({ event: "stage_started", run_id: RUN_ID, engine: ENGINE, stage: "evaluate", label: "LLM-as-a-judge", service: "Vertex AI · Claude Opus 5.5 (dev fixture)", request: { model: "claude-opus-5-5" }, ts: ts(6170) }) },
    { t_rel: 1200, event: ev({ event: "stage_completed", run_id: RUN_ID, engine: ENGINE, stage: "evaluate", duration_ms: 1180, payload: { model: "claude-opus-5-5", input_tokens: 1650, output_tokens: 140 }, ts: ts(7370) }) },
    { t_rel: 20, event: ev({ event: "eval_complete", run_id: RUN_ID, engine: ENGINE, metrics: { groundedness: 1, qa_relevance: 0.9, context_precision: 0.8 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 1180, rationale: "[DEV FIXTURE] synthetic rationale", ts: ts(7390) }) },
    { t_rel: 20, event: ev({ event: "battle_complete", run_id: RUN_ID, engines_completed: [ENGINE], engines_failed: [], ts: ts(7410) }) },
  ];
}

function failureTail(): TimedFixtureEvent[] {
  const message = "[DEV FIXTURE] 403 Permission 'discoveryengine.rankingConfigs.rank' denied on resource (synthetic error text)";
  return [
    { t_rel: 300, event: ev({ event: "stage_failed", run_id: RUN_ID, engine: ENGINE, stage: "rerank", duration_ms: 288, error_type: "PermissionDenied", error: message, ts: ts(3300) }) },
    { t_rel: 10, event: ev({ event: "engine_error", run_id: RUN_ID, engine: ENGINE, error_type: "PermissionDenied", error: message, failed_stage: "rerank", ts: ts(3310) }) },
    { t_rel: 10, event: ev({ event: "battle_complete", run_id: RUN_ID, engines_completed: [], engines_failed: [ENGINE], ts: ts(3320) }) },
  ];
}

export function level2DevFixture(failRerank: boolean): readonly TimedFixtureEvent[] {
  return [...head(), ...(failRerank ? failureTail() : successTail())];
}

/** Mirrors the backend's Level 2 descriptor shape (design metadata only). */
export const LEVEL2_DEV_ARCHITECTURE: ArchitectureDescriptor = {
  level_id: "level2_hybrid",
  nodes: [
    { id: "user", label: "User question", service: "Browser", kind: "actor", col: 0, row: 1, stage_ids: ["query_prep"] },
    { id: "embed", label: "Query embedding", service: "Vertex AI · gemini-embedding-2", kind: "model", col: 1, row: 0, stage_ids: ["embed_query"] },
    { id: "bq", label: "Hybrid RRF", service: "BigQuery VECTOR_SEARCH + SEARCH()", kind: "store", col: 2, row: 1, stage_ids: ["retrieve"] },
    { id: "rank", label: "Cross-encoder rerank", service: SERVICE_RANK, kind: "api", col: 3, row: 1, stage_ids: ["rerank"] },
    { id: "llm", label: "Answer synthesis", service: "Vertex AI · gemini-4-argon", kind: "model", col: 4, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI · claude-opus-5-5", kind: "model", col: 5, row: 1, stage_ids: ["evaluate"] },
  ],
  edges: [
    { from: "user", to: "embed", label: "text" },
    { from: "embed", to: "bq", label: "3072-d vector" },
    { from: "user", to: "bq", label: "lexical query" },
    { from: "bq", to: "rank", label: "RRF candidates" },
    { from: "rank", to: "llm", label: "reranked" },
    { from: "llm", to: "judge", label: "answer" },
  ],
  ingest_nodes: [],
  ingest_edges: [],
};
