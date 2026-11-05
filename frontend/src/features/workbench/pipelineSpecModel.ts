/**
 * Pure Composable PipelineSpec model (Release 1.3.0) — no React, no I/O, no mutable globals.
 * Mirrors `backend/app/core/pipeline_spec.py` 1:1 so Workbench and Ablation `/compare` enforce
 * identical invariants, stage order, model restrictions, and 0-ms live `ArchitectureDescriptor` updates.
 */

import {
  LEVEL_IDS,
  type ArchEdge,
  type ArchNode,
  type ArchitectureDescriptor,
  type CanonicalAgentTool,
  type ContextOrder,
  type IndexProfile,
  type LevelDescriptor,
  type LevelId,
  type ModelCatalogEntry,
  type ModelRole,
  type ModelsResponse,
  type PipelinePresetEntry,
  type PipelineRecipeWire,
  type PipelineSpec,
  type QuerySlots,
  type QueryTransform,
  type ReasoningMode,
  type RerankerKind,
  type RetrievalMode,
} from "../../types/arena";
import {
  MANAGED_SUMMARY_MODEL,
  MUST_DIFFER_FROM_JUDGE_REASON,
  MUST_DIFFER_REASON,
  RAG_ENGINE_GROUNDED_MODEL,
  findModel,
  formatModelPrice,
  optionStates,
  replayGate,
  safeHttpUrl,
  type ModelSelection,
  type OptionState,
  type PickerBlockedModel,
  type PickerDetails,
  type PickerPriceRow,
  type ReplayGate,
} from "../levels/modelSelection";

export const WORKBENCH_SLOT_ID = "workbench";
export const WORKBENCH_SESSION = "workbench";

export const INDEX_PROFILES: readonly IndexProfile[] = [
  "naive",
  "hybrid",
  "multimodal",
  "vertex_ai_search",
  "rag_engine",
  "alloydb_scann",
  "vertex_vector_v2",
] as const;

export const BQ_INDEX_PROFILES: readonly IndexProfile[] = ["naive", "hybrid", "multimodal"] as const;
export const MANAGED_INDEX_PROFILES: readonly IndexProfile[] = ["vertex_ai_search", "rag_engine"] as const;
export const SCANN_INDEX_PROFILES: readonly IndexProfile[] = ["alloydb_scann", "vertex_vector_v2"] as const;

export const BACKING_LEVEL_BY_PROFILE: Readonly<Record<IndexProfile, LevelId>> = {
  naive: "level1_naive",
  hybrid: "level2_hybrid",
  multimodal: "level3_multimodal",
  vertex_ai_search: "managed_vertex",
  rag_engine: "rag_engine",
  alloydb_scann: "alloydb_scann",
  vertex_vector_v2: "vertex_vector_v2",
};

export const PROFILE_LABELS: Readonly<Record<IndexProfile, string>> = {
  naive: "BigQuery · Naive chunks",
  hybrid: "BigQuery · Lineage chunks",
  multimodal: "BigQuery · DocAI + Vision multimodal",
  vertex_ai_search: "Vertex AI Search · Extractive segments",
  rag_engine: "Vertex AI RAG Engine · RagCorpus",
  alloydb_scann: "AlloyDB PostgreSQL · ScaNN + GIN FTS",
  vertex_vector_v2: "Vertex AI Vector Search 2.0 · ScaNN + Sparse RRF",
};

export const MAX_CANDIDATE_K_BY_PROFILE: Readonly<Record<IndexProfile, number>> = {
  naive: 100,
  hybrid: 100,
  multimodal: 100,
  rag_engine: 50,
  vertex_ai_search: 20,
  alloydb_scann: 100,
  vertex_vector_v2: 100,
};

export const QUERY_TRANSFORMS: readonly QueryTransform[] = ["none", "acronym", "acronym_plus_intent"] as const;
export const RETRIEVAL_MODES: readonly RetrievalMode[] = [
  "vector_cosine",
  "lexical_bm25",
  "hybrid_rrf",
  "managed_retrieval",
] as const;
export const BQ_RETRIEVAL_MODES: readonly RetrievalMode[] = ["vector_cosine", "lexical_bm25", "hybrid_rrf"] as const;
export const RERANKERS: readonly RerankerKind[] = ["none", "vertex_ranking_api"] as const;
export const CONTEXT_ORDERS: readonly ContextOrder[] = ["score_order", "authority_amendment_first"] as const;
export const REASONING_MODES: readonly ReasoningMode[] = ["single_pass", "adk_agent"] as const;
export const CANONICAL_AGENT_TOOLS: readonly CanonicalAgentTool[] = [
  "search_procurement_corpus",
  "check_amendments",
  "calculate_contract_pricing",
  "verify_citations",
] as const;
export const TOP_K_RANGE: readonly [number, number] = [1, 20] as const;

export const QUERY_TRANSFORM_LABELS: Readonly<Record<QueryTransform, string>> = {
  none: "None (raw question)",
  acronym: "Procurement acronym expansion",
  acronym_plus_intent: "Acronym + table/diagram intent routing",
};

export const RETRIEVAL_MODE_LABELS: Readonly<Record<RetrievalMode, string>> = {
  vector_cosine: "Dense Vector (BigQuery VECTOR_SEARCH)",
  lexical_bm25: "Lexical Keyword (BigQuery SEARCH)",
  hybrid_rrf: "Hybrid Vector + Lexical → RRF",
  managed_retrieval: "Managed Service Retriever",
};

export const RERANKER_LABELS: Readonly<Record<RerankerKind, string>> = {
  none: "None (retriever rank order)",
  vertex_ranking_api: "Discovery Engine Semantic Reranker",
};

export const CONTEXT_ORDER_LABELS: Readonly<Record<ContextOrder, string>> = {
  score_order: "Score order (highest rank first)",
  authority_amendment_first: "Authority-first (latest amendments above base)",
};

export const REASONING_MODE_LABELS: Readonly<Record<ReasoningMode, string>> = {
  single_pass: "Single-pass retrieve → synthesize",
  adk_agent: "Multi-step Google ADK agent loop",
};

export const AGENT_TOOL_LABELS: Readonly<Record<CanonicalAgentTool, string>> = {
  search_procurement_corpus: "search_procurement_corpus (required)",
  check_amendments: "check_amendments (SQL lineage lookup)",
  calculate_contract_pricing: "calculate_contract_pricing (AST math)",
  verify_citations: "verify_citations (claim grounding check)",
};

/** Returns the ingest level whose corpus/workspace data backs `profile`. */
export function backingLevelForProfile(profile: IndexProfile): LevelId {
  return BACKING_LEVEL_BY_PROFILE[profile];
}

/** Returns true when `profile` queries the BigQuery `document_chunks` table. */
export function isBqProfile(profile: IndexProfile): boolean {
  return BQ_INDEX_PROFILES.includes(profile);
}

/** Returns true when `profile` queries an ephemeral ScaNN hybrid store (Level 7 AlloyDB or Level 8 VVS2). */
export function isScannProfile(profile: IndexProfile): boolean {
  return SCANN_INDEX_PROFILES.includes(profile);
}

/** Returns the canonical default `PipelineSpec` for one of the 8 delivery levels. */
export function defaultPresetSpec(levelId: LevelId, presets?: readonly PipelinePresetEntry[]): PipelineSpec {
  const fromServer = presets?.find((p) => p.level_id === levelId);
  if (fromServer) {
    return {
      index_profile: fromServer.spec.index_profile,
      slots: { ...fromServer.spec.slots, agent_tools: [...fromServer.spec.slots.agent_tools] },
    };
  }
  switch (levelId) {
    case "level1_naive":
      return {
        index_profile: "naive",
        slots: {
          query_transform: "none",
          retrieval_mode: "vector_cosine",
          candidate_k: 5,
          top_k: 5,
          reranker: "none",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "level2_hybrid":
      return {
        index_profile: "hybrid",
        slots: {
          query_transform: "acronym",
          retrieval_mode: "hybrid_rrf",
          candidate_k: 50,
          top_k: 5,
          reranker: "vertex_ranking_api",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "level3_multimodal":
      return {
        index_profile: "multimodal",
        slots: {
          query_transform: "acronym_plus_intent",
          retrieval_mode: "hybrid_rrf",
          candidate_k: 50,
          top_k: 5,
          reranker: "vertex_ranking_api",
          context_order: "authority_amendment_first",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "level4_agentic":
      return {
        index_profile: "multimodal",
        slots: {
          query_transform: "acronym_plus_intent",
          retrieval_mode: "hybrid_rrf",
          candidate_k: 50,
          top_k: 5,
          reranker: "vertex_ranking_api",
          context_order: "authority_amendment_first",
          reasoning_mode: "adk_agent",
          agent_tools: [...CANONICAL_AGENT_TOOLS],
        },
      };
    case "managed_vertex":
      return {
        index_profile: "vertex_ai_search",
        slots: {
          query_transform: "acronym",
          retrieval_mode: "managed_retrieval",
          candidate_k: 10,
          top_k: 5,
          reranker: "none",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "rag_engine":
      return {
        index_profile: "rag_engine",
        slots: {
          query_transform: "acronym",
          retrieval_mode: "managed_retrieval",
          candidate_k: 10,
          top_k: 10,
          reranker: "none",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "alloydb_scann":
      return {
        index_profile: "alloydb_scann",
        slots: {
          query_transform: "acronym",
          retrieval_mode: "hybrid_rrf",
          candidate_k: 25,
          top_k: 5,
          reranker: "vertex_ranking_api",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
    case "vertex_vector_v2":
      return {
        index_profile: "vertex_vector_v2",
        slots: {
          query_transform: "acronym",
          retrieval_mode: "hybrid_rrf",
          candidate_k: 25,
          top_k: 5,
          reranker: "vertex_ranking_api",
          context_order: "score_order",
          reasoning_mode: "single_pass",
          agent_tools: [],
        },
      };
  }
}

function canonicalTools(tools: readonly string[], reasoningMode: ReasoningMode): CanonicalAgentTool[] {
  if (reasoningMode === "single_pass") return [];
  const wanted = new Set(tools);
  wanted.add("search_procurement_corpus");
  return CANONICAL_AGENT_TOOLS.filter((t) => wanted.has(t));
}

function slotsStructurallyEqual(a: QuerySlots, b: QuerySlots): boolean {
  if (
    a.query_transform !== b.query_transform ||
    a.retrieval_mode !== b.retrieval_mode ||
    a.candidate_k !== b.candidate_k ||
    a.top_k !== b.top_k ||
    a.reranker !== b.reranker ||
    a.context_order !== b.context_order ||
    a.reasoning_mode !== b.reasoning_mode
  ) {
    return false;
  }
  const ta = canonicalTools(a.agent_tools, a.reasoning_mode);
  const tb = canonicalTools(b.agent_tools, b.reasoning_mode);
  return ta.length === tb.length && ta.every((t, idx) => t === tb[idx]);
}

/** Returns the matching `LevelId` preset when structural slots match 1:1, else `null`. */
export function matchingPresetLevelId(spec: PipelineSpec, presets?: readonly PipelinePresetEntry[]): LevelId | null {
  for (const levelId of LEVEL_IDS) {
    const preset = defaultPresetSpec(levelId, presets);
    if (spec.index_profile === preset.index_profile && slotsStructurallyEqual(spec.slots, preset.slots)) {
      return levelId;
    }
  }
  return null;
}

/** Returns a concise human-readable recipe label for headers, rails, and scoreboards. */
export function pipelineSummaryLabel(spec: PipelineSpec, presets?: readonly PipelinePresetEntry[]): string {
  const trimmed = spec.label?.trim() ?? "";
  if (trimmed.length > 0) return trimmed;
  const presetId = matchingPresetLevelId(spec, presets);
  if (presetId !== null) return `Preset · ${presetId}`;
  const parts = [spec.index_profile, spec.slots.retrieval_mode, `k=${spec.slots.top_k}`];
  if (spec.slots.reranker !== "none") parts.push("rerank");
  if (spec.slots.reasoning_mode === "adk_agent") parts.push(`adk(${spec.slots.agent_tools.length}t)`);
  return parts.join(" · ");
}

function clampInt(raw: number, lo: number, hi: number): number {
  const n = Number.isFinite(raw) ? Math.round(raw) : lo;
  return Math.max(lo, Math.min(hi, n));
}

/** Coerces a candidate `PipelineSpec` into a guaranteed-valid domain configuration on any UI click. */
export function coerceSpec(raw: PipelineSpec): PipelineSpec {
  const profile: IndexProfile = INDEX_PROFILES.includes(raw.index_profile) ? raw.index_profile : "hybrid";
  const bq = isBqProfile(profile);
  const scann = isScannProfile(profile);
  const maxCand = MAX_CANDIDATE_K_BY_PROFILE[profile];
  const topK = clampInt(raw.slots.top_k, TOP_K_RANGE[0], Math.min(TOP_K_RANGE[1], maxCand));
  const candidateK = clampInt(raw.slots.candidate_k, topK, maxCand);
  const retrievalMode: RetrievalMode = bq
    ? BQ_RETRIEVAL_MODES.includes(raw.slots.retrieval_mode)
      ? raw.slots.retrieval_mode
      : profile === "naive"
        ? "vector_cosine"
        : "hybrid_rrf"
    : scann
      ? "hybrid_rrf"
      : "managed_retrieval";
  const reasoningMode: ReasoningMode = bq && raw.slots.reasoning_mode === "adk_agent" ? "adk_agent" : "single_pass";
  const reranker: RerankerKind = RERANKERS.includes(raw.slots.reranker) ? raw.slots.reranker : "none";
  const queryTransform: QueryTransform = QUERY_TRANSFORMS.includes(raw.slots.query_transform)
    ? raw.slots.query_transform
    : "none";
  const contextOrder: ContextOrder = CONTEXT_ORDERS.includes(raw.slots.context_order)
    ? raw.slots.context_order
    : "score_order";
  const agentTools =
    reasoningMode === "adk_agent"
      ? raw.slots.agent_tools.length === 0
        ? [...CANONICAL_AGENT_TOOLS]
        : canonicalTools(raw.slots.agent_tools, "adk_agent")
      : [];

  let synth = raw.synthesis_model ?? null;
  if (synth === MANAGED_SUMMARY_MODEL && (profile !== "vertex_ai_search" || reranker !== "none" || reasoningMode !== "single_pass")) {
    synth = null;
  }
  if (synth === RAG_ENGINE_GROUNDED_MODEL && (profile !== "rag_engine" || reranker !== "none" || reasoningMode !== "single_pass")) {
    synth = null;
  }

  return {
    index_profile: profile,
    slots: {
      query_transform: queryTransform,
      retrieval_mode: retrievalMode,
      candidate_k: candidateK,
      top_k: topK,
      reranker,
      context_order: contextOrder,
      reasoning_mode: reasoningMode,
      agent_tools: agentTools,
    },
    ...(synth ? { synthesis_model: synth } : {}),
    ...(raw.judge_model ? { judge_model: raw.judge_model } : {}),
    ...(raw.label?.trim() ? { label: raw.label.trim().slice(0, 80) } : {}),
  };
}

/** Validates a `PipelineSpec` without coercion; returns `null` when valid or an explicit error string. */
export function validatePipelineSpec(spec: PipelineSpec): string | null {
  if (!INDEX_PROFILES.includes(spec.index_profile)) {
    return `Unknown index_profile "${String(spec.index_profile)}".`;
  }
  const s = spec.slots;
  if (!QUERY_TRANSFORMS.includes(s.query_transform)) return `Invalid query_transform "${String(s.query_transform)}".`;
  if (!RETRIEVAL_MODES.includes(s.retrieval_mode)) return `Invalid retrieval_mode "${String(s.retrieval_mode)}".`;
  if (!Number.isInteger(s.top_k) || s.top_k < TOP_K_RANGE[0] || s.top_k > TOP_K_RANGE[1]) {
    return `top_k (${s.top_k}) must be an integer in [${TOP_K_RANGE[0]}, ${TOP_K_RANGE[1]}].`;
  }
  const maxCand = MAX_CANDIDATE_K_BY_PROFILE[spec.index_profile];
  if (!Number.isInteger(s.candidate_k) || s.candidate_k < s.top_k || s.candidate_k > maxCand) {
    return `candidate_k (${s.candidate_k}) must be an integer in [${s.top_k}, ${maxCand}] for ${spec.index_profile}.`;
  }
  if (!RERANKERS.includes(s.reranker)) return `Invalid reranker "${String(s.reranker)}".`;
  if (!CONTEXT_ORDERS.includes(s.context_order)) return `Invalid context_order "${String(s.context_order)}".`;
  if (!REASONING_MODES.includes(s.reasoning_mode)) return `Invalid reasoning_mode "${String(s.reasoning_mode)}".`;
  if (isBqProfile(spec.index_profile) && !BQ_RETRIEVAL_MODES.includes(s.retrieval_mode)) {
    return `BigQuery index_profile "${spec.index_profile}" requires retrieval_mode in ${BQ_RETRIEVAL_MODES.join(", ")}.`;
  }
  if (isScannProfile(spec.index_profile) && s.retrieval_mode !== "hybrid_rrf") {
    return `ScaNN index_profile "${spec.index_profile}" requires retrieval_mode "hybrid_rrf".`;
  }
  if (!isBqProfile(spec.index_profile) && !isScannProfile(spec.index_profile) && s.retrieval_mode !== "managed_retrieval") {
    return `Managed index_profile "${spec.index_profile}" requires retrieval_mode "managed_retrieval".`;
  }
  if (s.reasoning_mode === "adk_agent") {
    if (!isBqProfile(spec.index_profile)) {
      return `reasoning_mode "adk_agent" requires a BigQuery index_profile (${BQ_INDEX_PROFILES.join(", ")}).`;
    }
    if (!s.agent_tools.includes("search_procurement_corpus")) {
      return `reasoning_mode "adk_agent" requires "search_procurement_corpus" in agent_tools.`;
    }
  } else if (s.agent_tools.length > 0) {
    return `agent_tools must be empty when reasoning_mode is "single_pass".`;
  }
  return null;
}

/** Returns the ordered canonical query stage ids emitted by `spec`. */
export function activeQueryStages(spec: PipelineSpec): readonly string[] {
  if (spec.slots.reasoning_mode === "adk_agent") {
    return ["query_prep", "agent_turn", "synthesize", "evaluate"];
  }
  const stages: string[] = ["query_prep"];
  if (
    isScannProfile(spec.index_profile) ||
    (isBqProfile(spec.index_profile) && (spec.slots.retrieval_mode === "vector_cosine" || spec.slots.retrieval_mode === "hybrid_rrf"))
  ) {
    stages.push("embed_query");
  }
  stages.push("retrieve");
  if (spec.slots.reranker === "vertex_ranking_api") stages.push("rerank");
  stages.push("synthesize", "evaluate");
  return stages;
}

/** Returns true when `spec`'s backing corpus level (and `level4_agentic` for ADK) is enabled on the server. */
export function isSpecEnabled(spec: PipelineSpec, enabledLevels: readonly string[]): boolean {
  const backing = backingLevelForProfile(spec.index_profile);
  if (!enabledLevels.includes(backing)) return false;
  return spec.slots.reasoning_mode !== "adk_agent" || enabledLevels.includes("level4_agentic");
}

/** Returns why `entry` cannot synthesize for `spec`, or `null` when compatible. */
export function modelRestrictionForSpec(entry: ModelCatalogEntry, spec: PipelineSpec): string | null {
  if (entry.id === MANAGED_SUMMARY_MODEL || entry.provider === "vertex_ai_search") {
    if (spec.index_profile !== "vertex_ai_search") {
      return "managed Vertex AI Search summary requires index_profile=vertex_ai_search";
    }
    if (spec.slots.reranker !== "none" || spec.slots.reasoning_mode !== "single_pass") {
      return "managed Vertex AI Search summary requires reranker=none and single_pass reasoning";
    }
    return null;
  }
  if (entry.id === RAG_ENGINE_GROUNDED_MODEL || entry.provider === "vertex_rag_engine") {
    if (spec.index_profile !== "rag_engine") {
      return "VertexRagStore grounded synthesis requires index_profile=rag_engine";
    }
    if (spec.slots.reranker !== "none" || spec.slots.reasoning_mode !== "single_pass") {
      return "VertexRagStore grounded synthesis retrieves inside generateContent and requires reranker=none and single_pass";
    }
    return null;
  }
  if (spec.slots.reasoning_mode === "adk_agent" && entry.provider !== "vertex_gemini") {
    return "ADK agent reasoning requires a Vertex AI Gemini synthesis model";
  }
  return null;
}

/** Builds synthesis `<option>` states gated by `spec` invariants, catalog availability, and judge ≠ synthesis. */
export function synthesisOptionsForSpec(
  catalog: ModelsResponse,
  judgeId: string | null,
  spec: PipelineSpec,
): OptionState[] {
  return catalog.models
    .filter((m) => m.roles.includes("synthesis"))
    .map((entry) => {
      const restricted = modelRestrictionForSpec(entry, spec);
      if (restricted) return { entry, disabled: true, reason: restricted, unavailable: false, restricted: true };
      if (!entry.available) {
        return {
          entry,
          disabled: true,
          reason: entry.unavailable_reason?.trim() || "unavailable",
          unavailable: true,
        };
      }
      if (judgeId !== null && entry.id === judgeId) {
        return { entry, disabled: true, reason: MUST_DIFFER_FROM_JUDGE_REASON, unavailable: false };
      }
      return { entry, disabled: false, reason: null, unavailable: false };
    });
}

/** Validates `(synthesis, judge)` against `spec` invariants and catalog rules; returns `null` when runnable. */
export function validateModelsForSpec(
  catalog: ModelsResponse,
  sel: ModelSelection,
  spec: PipelineSpec,
): string | null {
  const synth = findModel(catalog, sel.synthesis);
  const judge = findModel(catalog, sel.judge);
  if (!synth) return `Synthesis model "${sel.synthesis}" is not in the catalog.`;
  if (!judge) return `Judge model "${sel.judge}" is not in the catalog.`;
  if (!synth.roles.includes("synthesis")) return `${synth.label} cannot be used for synthesis.`;
  if (!judge.roles.includes("judge")) return `${judge.label} cannot be used as the judge.`;
  const synthRestriction = modelRestrictionForSpec(synth, spec);
  if (synthRestriction) return `${synth.label} cannot run with this recipe: ${synthRestriction}.`;
  if (!synth.available) return `${synth.label} is unavailable: ${synth.unavailable_reason?.trim() || "unavailable"}.`;
  if (!judge.available) return `${judge.label} is unavailable: ${judge.unavailable_reason?.trim() || "unavailable"}.`;
  if (synth.id === judge.id) return `Judge model ${MUST_DIFFER_REASON}.`;
  return null;
}

/** Keeps `(synthesis, judge)` compatible when changing `spec` invalidates the current synthesis model. */
export function coerceSelectionForSpec(
  catalog: ModelsResponse,
  sel: ModelSelection,
  spec: PipelineSpec,
): ModelSelection {
  if (validateModelsForSpec(catalog, sel, spec) === null) return sel;
  const defCand: ModelSelection = { synthesis: catalog.defaults.synthesis_model, judge: sel.judge };
  if (validateModelsForSpec(catalog, defCand, spec) === null) return defCand;
  const firstValid = synthesisOptionsForSpec(catalog, sel.judge, spec).find((o) => !o.disabled);
  if (firstValid) return { synthesis: firstValid.entry.id, judge: sel.judge };
  return { synthesis: catalog.defaults.synthesis_model, judge: catalog.defaults.judge_model };
}

/** Builds pricing + unavailable/recipe-restricted disclosure details for `<ModelPicker spec={spec}>`. */
export function buildSpecPickerDetails(
  catalog: ModelsResponse,
  sel: ModelSelection,
  spec: PipelineSpec,
): PickerDetails {
  const rolePairs: readonly (readonly [ModelRole, string, string])[] = [
    ["synthesis", "Synthesis", sel.synthesis],
    ["judge", "Judge", sel.judge],
  ];
  const prices: PickerPriceRow[] = [];
  for (const [role, roleLabel, id] of rolePairs) {
    const entry = findModel(catalog, id);
    if (!entry) continue;
    prices.push({
      role,
      roleLabel,
      modelLabel: entry.label,
      priceText: formatModelPrice(entry),
      priceUrl: safeHttpUrl(entry.price_source),
    });
  }
  const seen = new Set<string>();
  const blockedModels: PickerBlockedModel[] = [];
  const combined = [
    ...synthesisOptionsForSpec(catalog, sel.judge, spec),
    ...optionStates(catalog, "judge", sel.synthesis, null),
  ];
  for (const opt of combined) {
    if ((!opt.unavailable && !opt.restricted) || !opt.reason || seen.has(opt.entry.id)) continue;
    seen.add(opt.entry.id);
    blockedModels.push({ id: opt.entry.id, label: opt.entry.label, reason: opt.reason, unavailable: opt.unavailable });
  }
  const unavailableCount = blockedModels.filter((b) => b.unavailable).length;
  const summaryLabel =
    unavailableCount > 0 ? `Pricing & availability · ${unavailableCount} unavailable` : "Pricing & availability";
  return { summaryLabel, prices, blockedModels };
}

/** Replay gate for Workbench / Ablation: exact presets play recorded pairs; custom recipes require Live mode. */
export function workbenchReplayGate(
  catalog: ModelsResponse,
  spec: PipelineSpec,
  questionId: string,
  sel: ModelSelection,
  presets?: readonly PipelinePresetEntry[],
): ReplayGate {
  if (catalog.demo_mode !== "replay") return { replay: false, pairs: [], blockedReason: null };
  const presetId = matchingPresetLevelId(spec, presets);
  if (presetId !== null) return replayGate(catalog, presetId, questionId, sel);
  return {
    replay: true,
    pairs: [],
    blockedReason: "Custom component recipes require Live mode — pick one of the 8 Level Presets above to play recorded runs.",
  };
}

/** Converts a validated `PipelineSpec` + `slotId` + optional `ModelSelection` into the `/battle/stream` wire item. */
export function toRecipeWire(
  spec: PipelineSpec,
  slotId: string,
  sel: ModelSelection | null = null,
): PipelineRecipeWire {
  const synth = sel?.synthesis ?? spec.synthesis_model ?? null;
  const judge = sel?.judge ?? spec.judge_model ?? null;
  return {
    slot_id: slotId,
    index_profile: spec.index_profile,
    slots: { ...spec.slots, agent_tools: [...spec.slots.agent_tools] },
    ...(synth ? { synthesis_model: synth } : {}),
    ...(judge ? { judge_model: judge } : {}),
    ...(spec.label?.trim() ? { label: spec.label.trim() } : {}),
  };
}

// ---------------------------------------------------------------------------
// 0-ms client-side live `ArchitectureDescriptor` builder
// ---------------------------------------------------------------------------

function userNode(transform: QueryTransform): ArchNode {
  const meta: Readonly<Record<QueryTransform, readonly [string, string]>> = {
    none: ["User question", "Browser"],
    acronym: ["User question + acronym expansion", "Engine (local)"],
    acronym_plus_intent: ["User question + element intent", "Engine (local)"],
  };
  const [label, service] = meta[transform];
  return {
    id: "user",
    label,
    service,
    kind: "actor",
    col: 0,
    row: 1,
    stage_ids: ["query_prep"],
    code_ref: "app.engines.composable:ComposableRAGEngine",
  };
}

function bqServiceLabel(spec: PipelineSpec): string {
  const mode = spec.slots.retrieval_mode;
  const k = spec.slots.candidate_k;
  if (mode === "vector_cosine") return `BigQuery VECTOR_SEARCH (top-${k})`;
  if (mode === "lexical_bm25") return `BigQuery SEARCH() + lexical rank (top-${k})`;
  return `BigQuery VECTOR_SEARCH + SEARCH() → RRF (top-${k})`;
}

function singlePassRetrievalGraph(spec: PipelineSpec): {
  readonly nodes: readonly ArchNode[];
  readonly edges: readonly ArchEdge[];
  readonly outId: string;
} {
  const nodes: ArchNode[] = [];
  const edges: ArchEdge[] = [];
  if (isBqProfile(spec.index_profile)) {
    const bq: ArchNode = {
      id: "bq",
      label: `document_chunks (tier ${spec.index_profile})`,
      service: bqServiceLabel(spec),
      kind: "store",
      col: 2,
      row: 1,
      stage_ids: ["retrieve"],
    };
    if (spec.slots.retrieval_mode === "vector_cosine" || spec.slots.retrieval_mode === "hybrid_rrf") {
      const embSvc =
        spec.index_profile === "multimodal"
          ? "Vertex AI · gemini-embedding-2 (multimodal)"
          : "Vertex AI · gemini-embedding-2";
      nodes.push({ id: "embed", label: "Query embedding", service: embSvc, kind: "model", col: 1, row: 0, stage_ids: ["embed_query"] });
      edges.push({ from: "user", to: "embed", label: "prepared query" }, { from: "embed", to: "bq", label: "3072-d vector" });
    }
    if (spec.slots.retrieval_mode === "lexical_bm25" || spec.slots.retrieval_mode === "hybrid_rrf") {
      edges.push({ from: "user", to: "bq", label: "lexical terms" });
    }
    nodes.push(bq);
    return { nodes, edges, outId: "bq" };
  }
  if (spec.index_profile === "alloydb_scann") {
    nodes.push(
      { id: "embed", label: "Query embedding", service: "Vertex AI · gemini-embedding-2", kind: "model", col: 1, row: 0, stage_ids: ["embed_query"] },
      { id: "alloydb", label: "AlloyDB ScaNN + GIN FTS", service: `AlloyDB for PostgreSQL · ScaNN RRF (top-${spec.slots.candidate_k})`, kind: "store", col: 2, row: 1, stage_ids: ["retrieve"] },
    );
    edges.push(
      { from: "user", to: "embed", label: "prepared query" },
      { from: "embed", to: "alloydb", label: "3072-d vector" },
      { from: "user", to: "alloydb", label: "tsquery terms" },
    );
    return { nodes, edges, outId: "alloydb" };
  }
  if (spec.index_profile === "vertex_vector_v2") {
    nodes.push(
      { id: "embed", label: "Query embedding", service: "Vertex AI · gemini-embedding-2", kind: "model", col: 1, row: 0, stage_ids: ["embed_query"] },
      { id: "vvs2", label: "Vector Search 2.0 Collection", service: `dataObjects:batchSearch · ScaNN + Sparse RRF (top-${spec.slots.candidate_k})`, kind: "store", col: 2, row: 1, stage_ids: ["retrieve"] },
    );
    edges.push(
      { from: "user", to: "embed", label: "prepared query" },
      { from: "embed", to: "vvs2", label: "3072-d vector" },
      { from: "user", to: "vvs2", label: "sparse tokens" },
    );
    return { nodes, edges, outId: "vvs2" };
  }
  if (spec.index_profile === "vertex_ai_search") {
    nodes.push(
      { id: "search", label: "Search app", service: "Vertex AI Search · :search", kind: "api", col: 1, row: 1, stage_ids: ["retrieve"] },
      { id: "ds", label: "Data store", service: "Vertex AI Search data store", kind: "store", col: 2, row: 0, stage_ids: ["retrieve"] },
    );
    edges.push(
      { from: "user", to: "search", label: "prepared query" },
      { from: "search", to: "ds", label: "query" },
      { from: "ds", to: "search", label: "extractive segments" },
    );
    return { nodes, edges, outId: "search" };
  }
  nodes.push(
    { id: "retrieve", label: "RetrieveContexts API", service: "Vertex AI RAG Engine · :retrieveContexts", kind: "api", col: 1, row: 1, stage_ids: ["retrieve"] },
    { id: "rag_corpus", label: "RagCorpus", service: "Vertex AI RAG Engine · RagManagedDb", kind: "store", col: 2, row: 0, stage_ids: ["retrieve"] },
  );
  edges.push(
    { from: "user", to: "retrieve", label: "prepared query" },
    { from: "retrieve", to: "rag_corpus", label: `KNN (top-${spec.slots.candidate_k})` },
    { from: "rag_corpus", to: "retrieve", label: "chunks + pageSpan" },
  );
  return { nodes, edges, outId: "retrieve" };
}

function agentGraph(spec: PipelineSpec): { readonly nodes: readonly ArchNode[]; readonly edges: readonly ArchEdge[] } {
  const turn = ["agent_turn"];
  const tools = new Set(spec.slots.agent_tools);
  const bqTools = ["search_procurement_corpus", ...(tools.has("check_amendments") ? ["check_amendments"] : [])];
  const nodes: ArchNode[] = [
    userNode(spec.slots.query_transform),
    { id: "agent", label: "ADK LlmAgent loop", service: "Google ADK InMemoryRunner · Vertex AI Gemini", kind: "agent", col: 1, row: 1, stage_ids: turn },
    { id: "tool_search", label: "search_procurement_corpus", service: `${spec.index_profile} · ${spec.slots.retrieval_mode}`, kind: "process", col: 2, row: 0, stage_ids: turn, tool_names: ["search_procurement_corpus"] },
    { id: "bq", label: `document_chunks (tier ${spec.index_profile})`, service: bqServiceLabel(spec), kind: "store", col: 3, row: 1, stage_ids: turn, tool_names: bqTools },
  ];
  const edges: ArchEdge[] = [
    { from: "user", to: "agent", label: "prepared query" },
    { from: "agent", to: "tool_search", label: "tool call" },
    { from: "tool_search", to: "agent", label: "chunks" },
  ];
  if (spec.slots.retrieval_mode === "vector_cosine" || spec.slots.retrieval_mode === "hybrid_rrf") {
    const embSvc = spec.index_profile === "multimodal" ? "Vertex AI · gemini-embedding-2 (multimodal)" : "Vertex AI · gemini-embedding-2";
    nodes.push({ id: "embed", label: "Query embedding", service: embSvc, kind: "model", col: 3, row: 0, stage_ids: turn, tool_names: ["search_procurement_corpus"] });
    edges.push({ from: "tool_search", to: "embed", label: "query" }, { from: "embed", to: "bq", label: "3072-d vector" });
  } else {
    edges.push({ from: "tool_search", to: "bq", label: "lexical terms" });
  }
  if (spec.slots.reranker === "vertex_ranking_api") {
    nodes.push({ id: "rank", label: "Semantic rerank", service: "Discovery Engine Ranking API", kind: "api", col: 4, row: 0, stage_ids: turn, region: "global", tool_names: ["search_procurement_corpus"] });
    edges.push({ from: "bq", to: "rank", label: `candidates (≤ ${spec.slots.candidate_k})` }, { from: "rank", to: "tool_search", label: `top-${spec.slots.top_k} chunks` });
  } else {
    edges.push({ from: "bq", to: "tool_search", label: `top-${spec.slots.top_k} chunks` });
  }
  if (tools.has("check_amendments")) {
    nodes.push({ id: "tool_amend", label: "check_amendments", service: "BigQuery amends_section lookup", kind: "process", col: 2, row: 1, stage_ids: turn, tool_names: ["check_amendments"] });
    edges.push({ from: "agent", to: "tool_amend", label: "tool call" }, { from: "tool_amend", to: "bq", label: "@section_id" }, { from: "tool_amend", to: "agent", label: "superseding text" });
  }
  if (tools.has("calculate_contract_pricing")) {
    nodes.push({ id: "tool_calc", label: "calculate_contract_pricing", service: "Local AST-validated arithmetic", kind: "process", col: 2, row: 2, stage_ids: turn, tool_names: ["calculate_contract_pricing"] });
    edges.push({ from: "agent", to: "tool_calc", label: "tool call" }, { from: "tool_calc", to: "agent", label: "value" });
  }
  if (tools.has("verify_citations")) {
    nodes.push({ id: "tool_verify", label: "verify_citations", service: "Local claim ↔ cited-chunk check", kind: "process", col: 2, row: 3, stage_ids: turn, tool_names: ["verify_citations"] });
    edges.push({ from: "agent", to: "tool_verify", label: "tool call" }, { from: "tool_verify", to: "agent", label: "unsupported claims" });
  }
  nodes.push(
    { id: "llm", label: "Final response (ADK)", service: "Google ADK · final model turn", kind: "model", col: 4, row: 2, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI Judge", kind: "model", col: 5, row: 2, stage_ids: ["evaluate"] },
  );
  edges.push({ from: "agent", to: "llm", label: "final turn" }, { from: "llm", to: "judge", label: "answer + tool chunks" });
  return { nodes, edges };
}

/** Builds an instant `ArchitectureDescriptor` for `spec` (exact preset descriptor when matching a preset). */
export function buildPipelineDescriptor(
  spec: PipelineSpec,
  presets?: readonly PipelinePresetEntry[],
  fallbackLevels?: readonly LevelDescriptor[],
): ArchitectureDescriptor {
  const presetId = matchingPresetLevelId(spec, presets);
  if (presetId !== null) {
    const serverPreset = presets?.find((p) => p.level_id === presetId)?.descriptor;
    if (serverPreset) return serverPreset;
    const fallback = fallbackLevels?.find((l) => l.level_id === presetId)?.architecture;
    if (fallback) return fallback;
  }
  const backingId = backingLevelForProfile(spec.index_profile);
  const backingArch =
    presets?.find((p) => p.level_id === backingId)?.descriptor ??
    fallbackLevels?.find((l) => l.level_id === backingId)?.architecture;

  if (spec.slots.reasoning_mode === "adk_agent") {
    const { nodes, edges } = agentGraph(spec);
    return {
      level_id: spec.pipeline_id ?? `custom_${spec.index_profile}_adk`,
      nodes,
      edges,
      ingest_nodes: backingArch?.ingest_nodes ?? [],
      ingest_edges: backingArch?.ingest_edges ?? [],
    };
  }

  const user = userNode(spec.slots.query_transform);
  const ret = singlePassRetrievalGraph(spec);
  const nodes: ArchNode[] = [user, ...ret.nodes];
  const edges: ArchEdge[] = [...ret.edges];
  let preLlm = ret.outId;
  if (spec.slots.reranker === "vertex_ranking_api") {
    nodes.push({ id: "rank", label: "Semantic rerank", service: "Discovery Engine Ranking API", kind: "api", col: 3, row: 1, stage_ids: ["rerank"], region: "global" });
    edges.push({ from: ret.outId, to: "rank", label: `candidates (≤ ${spec.slots.candidate_k})` });
    preLlm = "rank";
  }
  const llmLabel = spec.slots.context_order === "authority_amendment_first" ? "Authority-headed synthesis" : "Answer synthesis";
  const ctxEdge = spec.slots.context_order === "authority_amendment_first" ? `top-${spec.slots.top_k} · amendments first` : `top-${spec.slots.top_k} chunks`;
  nodes.push(
    { id: "llm", label: llmLabel, service: "Vertex AI Synthesis", kind: "model", col: 4, row: 1, stage_ids: ["synthesize"] },
    { id: "judge", label: "LLM-as-a-judge", service: "Vertex AI Judge", kind: "model", col: 5, row: 1, stage_ids: ["evaluate"] },
  );
  edges.push({ from: preLlm, to: "llm", label: ctxEdge }, { from: "llm", to: "judge", label: "answer + context" });
  return {
    level_id: spec.pipeline_id ?? `custom_${spec.index_profile}`,
    nodes,
    edges,
    ingest_nodes: backingArch?.ingest_nodes ?? [],
    ingest_edges: backingArch?.ingest_edges ?? [],
  };
}
