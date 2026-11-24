/**
 * Pure deterministic Model Pair Recommender (client mirror of `backend/app/core/model_recommender.py`).
 *
 * Recommends optimal cross-family `(synthesis_model, judge_model)` pairs across enterprise profiles:
 * - `judge_model !== synthesis_model` always.
 * - Cross-family judging by default (Gemini worker <-> Claude judge, or `vertex-genai-eval` rubric judge).
 * - Gemini-only worker when `levelId === "level4_agentic"` or `spec.slots.reasoning_mode === "adk_agent"`.
 * - Honest availability resolution against `ModelsResponse` (never recommends an unavailable model).
 */

import type { ModelRole, ModelsResponse, PipelineSpec, RecommendedModelPair } from "../../types/arena";

export const PROFILE_COST_LATENCY = "cost_latency" as const;
export const PROFILE_BALANCED_ENTERPRISE = "balanced_enterprise" as const;
export const PROFILE_MULTIMODAL_AGENTIC = "multimodal_agentic" as const;
export const PROFILE_ANTHROPIC_WORKER = "anthropic_worker" as const;

export const RECOMMENDATION_PROFILE_IDS = [
  PROFILE_COST_LATENCY,
  PROFILE_BALANCED_ENTERPRISE,
  PROFILE_MULTIMODAL_AGENTIC,
  PROFILE_ANTHROPIC_WORKER,
] as const;

export type RecommendationProfileId = (typeof RECOMMENDATION_PROFILE_IDS)[number];

export const VERTEX_GENAI_EVAL_MODEL_ID = "vertex-genai-eval";

const GEMINI_SYNTH_FALLBACKS: readonly string[] = [
  "gemini-4-neon",
  "gemini-4-argon",
  "gemini-4-carbon",
  "gemini-4-hydrogen",
  "gemini-3.8-flash",
];
const ANTHROPIC_SYNTH_FALLBACKS: readonly string[] = ["claude-sonnet-4-6", "claude-opus-5-5"];
const ANTHROPIC_JUDGE_FALLBACKS: readonly string[] = ["claude-opus-5-5", "claude-sonnet-4-6"];
const GEMINI_JUDGE_FALLBACKS: readonly string[] = [
  "gemini-4-neon",
  "gemini-4-argon",
  "gemini-4-carbon",
  "gemini-4-hydrogen",
  "gemini-3.8-flash",
];

export const RECOMMENDED_PROFILES: readonly RecommendedModelPair[] = [
  {
    profile_id: PROFILE_COST_LATENCY,
    profile_label: "High-QPS / Low-Cost",
    synthesis_model: "gemini-4-hydrogen",
    judge_model: "claude-sonnet-4-6",
    alt_judge_model: VERTEX_GENAI_EVAL_MODEL_ID,
    rationale:
      "Gemini 4 Hydrogen ($1.50 / $7.50 per 1M) delivers fast, low-cost grounded synthesis for high-QPS and interactive workloads.",
    judge_rationale:
      "Claude Sonnet 4.6 provides independent cross-family judging without Opus overhead (or switch to Vertex AI GenAI Eval for rubric scoring).",
    cross_family: true,
    adk_compatible: true,
  },
  {
    profile_id: PROFILE_BALANCED_ENTERPRISE,
    profile_label: "Enterprise SQL / Contracts",
    synthesis_model: "gemini-4-carbon",
    judge_model: "claude-opus-5-5",
    alt_judge_model: "claude-sonnet-4-6",
    rationale:
      "Gemini 4 Carbon ($2.00 / $10.00 per 1M) balances strong reasoning and clause accuracy across SQL-governed and enterprise contract corpora.",
    judge_rationale:
      "Claude Opus 5.5 grades groundedness and QA relevance from an independent Anthropic model family.",
    cross_family: true,
    adk_compatible: true,
  },
  {
    profile_id: PROFILE_MULTIMODAL_AGENTIC,
    profile_label: "Tables, Diagrams & ADK Math",
    synthesis_model: "gemini-4-argon",
    judge_model: "claude-opus-5-5",
    alt_judge_model: "claude-sonnet-4-6",
    rationale:
      "Gemini 4 Argon ($2.00 / $10.00 per 1M) is the flagship multimodal and Google ADK agentic worker for tables, diagrams, and multi-hop arithmetic.",
    judge_rationale:
      "Claude Opus 5.5 delivers flagship cross-family evaluation and matches the committed golden benchmark cohort.",
    cross_family: true,
    adk_compatible: true,
  },
  {
    profile_id: PROFILE_ANTHROPIC_WORKER,
    profile_label: "Claude Bake-Off (Levels 1–7)",
    synthesis_model: "claude-sonnet-4-6",
    judge_model: "gemini-4-argon",
    alt_judge_model: "gemini-4-carbon",
    rationale:
      "Claude Sonnet 4.6 ($3.00 / $15.00 per 1M) benchmarks Anthropic synthesis on single-pass Levels 1–7 (or upgrade to Claude Opus 5.5).",
    judge_rationale:
      "Gemini 4 Argon provides independent cross-family Google evaluation so Claude never grades its own family.",
    cross_family: true,
    adk_compatible: false,
  },
];

const PROFILE_BY_ID: Readonly<Record<string, RecommendedModelPair>> = Object.fromEntries(
  RECOMMENDED_PROFILES.map((p) => [p.profile_id, p])
);

export type ModelFamily = "gemini" | "anthropic" | "vertex_eval" | "vertex_search" | "other";

export function modelFamily(modelId: string | null | undefined): ModelFamily {
  const mid = (modelId ?? "").trim().toLowerCase();
  if (mid === VERTEX_GENAI_EVAL_MODEL_ID) return "vertex_eval";
  if (mid.startsWith("gemini-") || mid === "vertex-rag-engine-grounded") return "gemini";
  if (mid.startsWith("claude-")) return "anthropic";
  if (mid.startsWith("vertex-ai-search")) return "vertex_search";
  return "other";
}

export function isCrossFamilyPair(synthesisModel: string, judgeModel: string): boolean {
  if (!synthesisModel || !judgeModel || synthesisModel === judgeModel) return false;
  if (judgeModel === VERTEX_GENAI_EVAL_MODEL_ID) return true;
  return modelFamily(synthesisModel) !== modelFamily(judgeModel);
}

const SHORT_MODEL_NAMES: Readonly<Record<string, string>> = {
  "gemini-4-hydrogen": "Gemini 4 Hydrogen",
  "gemini-4-carbon": "Gemini 4 Carbon",
  "gemini-4-argon": "Gemini 4 Argon",
  "claude-sonnet-4-6": "Claude Sonnet 4.6",
  "claude-opus-5-5": "Claude Opus 5.5",
  "gemini-3.8-flash": "Gemini 3.8 Flash",
  "claude-fable-5-2": "Claude Fable 5.2",
  "claude-fable-5-1": "Claude Fable 5.1",
  "vertex-ai-search-summary": "VAIS Summary",
  "vertex-rag-engine-grounded": "RAG Engine Grounded",
  "vertex-genai-eval": "Vertex GenAI Eval",
};

/** Compact display label for pills and summary bars (strips verbose parenthetical suffixes). */
export function shortModelLabel(catalog: ModelsResponse | null, modelId: string | null | undefined): string {
  if (!modelId) return "—";
  if (SHORT_MODEL_NAMES[modelId]) return SHORT_MODEL_NAMES[modelId];
  const entry = catalog?.models.find((m) => m.id === modelId);
  if (entry?.label) return entry.label.replace(/\s*\([^)]*\)\s*/g, "").trim();
  return modelId;
}

function isAvailable(
  catalog: ModelsResponse | null | undefined,
  modelId: string,
  role: ModelRole,
  requireGemini = false
): boolean {
  if (!catalog) return !requireGemini || modelFamily(modelId) === "gemini";
  const spec = catalog.models.find((m) => m.id === modelId);
  if (!spec || !spec.available || !spec.roles.includes(role)) return false;
  if (requireGemini && spec.provider !== "vertex_gemini") return false;
  return true;
}

function pickSynthesis(
  preferred: string,
  catalog: ModelsResponse | null | undefined,
  requireGemini: boolean
): string {
  if (isAvailable(catalog, preferred, "synthesis", requireGemini)) return preferred;
  const fam = requireGemini ? "gemini" : modelFamily(preferred);
  const order =
    fam === "gemini" ? GEMINI_SYNTH_FALLBACKS : [...ANTHROPIC_SYNTH_FALLBACKS, ...GEMINI_SYNTH_FALLBACKS];
  for (const candidate of order) {
    if (isAvailable(catalog, candidate, "synthesis", requireGemini)) return candidate;
  }
  if (catalog) {
    for (const spec of catalog.models) {
      const restricted =
        (Array.isArray(spec.allowed_levels) && spec.allowed_levels.length > 0) ||
        (Array.isArray(spec.levels) && spec.levels.length > 0);
      if (spec.available && spec.roles.includes("synthesis") && !restricted) {
        if (!requireGemini || spec.provider === "vertex_gemini") return spec.id;
      }
    }
  }
  return requireGemini ? "gemini-4-argon" : preferred;
}

function pickJudge(
  preferred: string,
  alt: string | null,
  synthesisModel: string,
  catalog: ModelsResponse | null | undefined,
  preferRubricJudge: boolean
): string {
  const candidates: string[] = [];
  if (preferRubricJudge) candidates.push(VERTEX_GENAI_EVAL_MODEL_ID);
  candidates.push(preferred);
  if (alt) candidates.push(alt);
  const synthFam = modelFamily(synthesisModel);
  const crossFallbacks = synthFam === "gemini" ? ANTHROPIC_JUDGE_FALLBACKS : GEMINI_JUDGE_FALLBACKS;
  const sameFallbacks = synthFam === "gemini" ? GEMINI_JUDGE_FALLBACKS : ANTHROPIC_JUDGE_FALLBACKS;
  candidates.push(...crossFallbacks, VERTEX_GENAI_EVAL_MODEL_ID, ...sameFallbacks);

  const seen = new Set<string>();
  for (const candidate of candidates) {
    if (!candidate || seen.has(candidate) || candidate === synthesisModel) continue;
    seen.add(candidate);
    if (isAvailable(catalog, candidate, "judge")) return candidate;
  }
  if (catalog) {
    for (const spec of catalog.models) {
      if (spec.available && spec.roles.includes("judge") && spec.id !== synthesisModel) {
        return spec.id;
      }
    }
  }
  return preferred !== synthesisModel ? preferred : "claude-opus-5-5";
}

export interface RecommendPairOptions {
  readonly profileId?: RecommendationProfileId | string | null;
  readonly levelId?: string | null;
  readonly docComplexity?: string | null;
  readonly latencySla?: string | null;
  readonly dataGravity?: string | null;
  readonly monthlyQueries?: number | null;
  readonly reasoningMode?: string | null;
  readonly spec?: PipelineSpec | null;
  readonly preferAnthropicWorker?: boolean;
  readonly preferRubricJudge?: boolean;
  readonly catalog?: ModelsResponse | null;
}

export function inferProfileId(opts: RecommendPairOptions = {}): RecommendationProfileId {
  const reasoning = opts.reasoningMode ?? opts.spec?.slots.reasoning_mode ?? null;
  const adkRequired = opts.levelId === "level4_agentic" || reasoning === "adk_agent";
  if (adkRequired || opts.docComplexity === "multi_hop_math" || opts.latencySla === "deep_agentic_reasoning") {
    return PROFILE_MULTIMODAL_AGENTIC;
  }
  if (opts.preferAnthropicWorker) return PROFILE_ANTHROPIC_WORKER;
  if (
    opts.latencySla === "sub_second_retrieval" ||
    (typeof opts.monthlyQueries === "number" && opts.monthlyQueries >= 250_000)
  ) {
    return PROFILE_COST_LATENCY;
  }
  if (
    opts.docComplexity === "tables_diagrams" ||
    opts.levelId === "level3_multimodal" ||
    opts.levelId === "bigquery_graphrag"
  ) {
    return PROFILE_MULTIMODAL_AGENTIC;
  }
  if (
    opts.dataGravity === "bigquery_warehouse" ||
    opts.levelId === "level2_hybrid" ||
    opts.levelId === "cloudsql_pgvector" ||
    opts.levelId === "alloydb_scann"
  ) {
    return PROFILE_BALANCED_ENTERPRISE;
  }
  if (
    opts.dataGravity === "managed_turnkey" ||
    opts.dataGravity === "postgres_oltp" ||
    opts.levelId === "managed_vertex" ||
    opts.levelId === "rag_engine" ||
    opts.levelId === "level1_naive" ||
    opts.levelId === "vertex_vector_v2"
  ) {
    return PROFILE_COST_LATENCY;
  }
  return PROFILE_BALANCED_ENTERPRISE;
}

export function recommendModelPair(opts: RecommendPairOptions = {}): RecommendedModelPair {
  const reasoning = opts.reasoningMode ?? opts.spec?.slots.reasoning_mode ?? null;
  const adkRequired = opts.levelId === "level4_agentic" || reasoning === "adk_agent";

  let chosenId: RecommendationProfileId;
  if (opts.profileId) {
    if (!(opts.profileId in PROFILE_BY_ID)) {
      throw new Error(
        `Unknown model profile '${opts.profileId}'; valid: [${RECOMMENDATION_PROFILE_IDS.join(", ")}]`
      );
    }
    chosenId =
      adkRequired && opts.profileId === PROFILE_ANTHROPIC_WORKER
        ? PROFILE_MULTIMODAL_AGENTIC
        : (opts.profileId as RecommendationProfileId);
  } else {
    chosenId = inferProfileId(opts);
  }

  const base = PROFILE_BY_ID[chosenId];
  const synth = pickSynthesis(base.synthesis_model, opts.catalog, adkRequired);
  const judge = pickJudge(base.judge_model, base.alt_judge_model, synth, opts.catalog, Boolean(opts.preferRubricJudge));
  let altJudge: string | null =
    base.alt_judge_model && base.alt_judge_model !== synth && base.alt_judge_model !== judge
      ? base.alt_judge_model
      : null;
  if (altJudge && opts.catalog && !isAvailable(opts.catalog, altJudge, "judge")) {
    altJudge = null;
  }

  let rationale = base.rationale;
  if (adkRequired && opts.profileId === PROFILE_ANTHROPIC_WORKER) {
    rationale =
      "Level 8 Google ADK requires a Vertex Gemini worker (Claude is judge-only on ADK); switched to Gemini 4 Argon.";
  }
  let judgeRationale = base.judge_rationale;
  if (judge === VERTEX_GENAI_EVAL_MODEL_ID && base.judge_model !== VERTEX_GENAI_EVAL_MODEL_ID) {
    judgeRationale =
      "Vertex AI GenAI Eval (:evaluateInstances) applies managed rubric scoring independently of the synthesis model.";
  }

  return {
    profile_id: base.profile_id,
    profile_label: base.profile_label,
    synthesis_model: synth,
    judge_model: judge,
    alt_judge_model: altJudge,
    rationale,
    judge_rationale: judgeRationale,
    cross_family: isCrossFamilyPair(synth, judge),
    adk_compatible: modelFamily(synth) === "gemini",
  };
}

export function recommendedPairsForCatalog(catalog?: ModelsResponse | null): RecommendedModelPair[] {
  return RECOMMENDATION_PROFILE_IDS.map((profileId) => recommendModelPair({ profileId, catalog }));
}
