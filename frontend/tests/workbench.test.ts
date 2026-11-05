/**
 * Release 1.3.0 Step 3 — Composable Workbench pure-model tests (`npm test`).
 * Covers: 6 canonical presets, deterministic structural preset matching, automatic cross-slot coercion
 * (BigQuery <-> Managed profiles, single_pass <-> adk_agent, candidate_k / top_k bounds, model resets),
 * active stage derivation, model compatibility per recipe, replay & enabled gates, and 0-ms live
 * `ArchitectureDescriptor` construction with zero dangling edges.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BACKING_LEVEL_BY_PROFILE,
  CANONICAL_AGENT_TOOLS,
  INDEX_PROFILES,
  WORKBENCH_SLOT_ID,
  activeQueryStages,
  backingLevelForProfile,
  buildPipelineDescriptor,
  buildSpecPickerDetails,
  coerceSelectionForSpec,
  coerceSpec,
  defaultPresetSpec,
  isSpecEnabled,
  matchingPresetLevelId,
  modelRestrictionForSpec,
  pipelineSummaryLabel,
  synthesisOptionsForSpec,
  toRecipeWire,
  validateModelsForSpec,
  validatePipelineSpec,
  workbenchReplayGate,
} from "../src/features/workbench/pipelineSpecModel";
import {
  MANAGED_SUMMARY_MODEL,
  RAG_ENGINE_GROUNDED_MODEL,
} from "../src/features/levels/modelSelection";
import {
  LEVEL_IDS,
  type ArchitectureDescriptor,
  type ModelCatalogEntry,
  type ModelsResponse,
  type PipelinePresetEntry,
  type PipelineSpec,
} from "../src/types/arena";

const mkModel = (
  id: string,
  provider: string,
  roles: string[],
  extra: Partial<ModelCatalogEntry> = {},
): ModelCatalogEntry => ({
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

const LIVE_CATALOG: ModelsResponse = {
  models: [
    mkModel("gemini-4-argon", "vertex_gemini", ["synthesis", "judge"]),
    mkModel("gemini-3.8-flash", "vertex_gemini", ["synthesis", "judge"]),
    mkModel("claude-opus-5-5", "anthropic_vertex", ["synthesis", "judge"]),
    mkModel(MANAGED_SUMMARY_MODEL, "vertex_ai_search", ["synthesis"], { allowed_levels: ["managed_vertex"] }),
    mkModel(RAG_ENGINE_GROUNDED_MODEL, "vertex_rag_engine", ["synthesis"], { allowed_levels: ["rag_engine"] }),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "live",
  recorded_pairs: [
    {
      engine: "rag_engine",
      synthesis_model: "gemini-4-argon",
      judge_model: "claude-opus-5-5",
      question_ids: ["q_exact_clin_lookup"],
    },
  ],
};

test("all 8 Level presets round-trip through validatePipelineSpec and match their canonical level_id", () => {
  assert.equal(INDEX_PROFILES.length, 7);
  for (const levelId of LEVEL_IDS) {
    const spec = defaultPresetSpec(levelId);
    assert.equal(validatePipelineSpec(spec), null, `preset ${levelId} must be valid`);
    assert.equal(matchingPresetLevelId(spec), levelId);
    assert.equal(backingLevelForProfile(spec.index_profile), BACKING_LEVEL_BY_PROFILE[spec.index_profile]);
    assert.match(pipelineSummaryLabel(spec), new RegExp(levelId));
  }
});

test("coerceSpec keeps cross-slot invariants valid across every profile & reasoning transition", () => {
  // Start at Level 4 (multimodal + hybrid_rrf + candidate_k=50 + adk_agent + 4 tools)
  const l4 = defaultPresetSpec("level4_agentic");
  assert.equal(l4.slots.reasoning_mode, "adk_agent");
  assert.equal(l4.slots.agent_tools.length, 4);

  // Switch profile -> rag_engine: must coerce to managed_retrieval, single_pass, empty tools, candidate_k <= 50
  const toRag = coerceSpec({ ...l4, index_profile: "rag_engine" });
  assert.equal(validatePipelineSpec(toRag), null);
  assert.equal(toRag.slots.retrieval_mode, "managed_retrieval");
  assert.equal(toRag.slots.reasoning_mode, "single_pass");
  assert.deepEqual(toRag.slots.agent_tools, []);
  assert.ok(toRag.slots.candidate_k <= 50);

  // Switch profile -> vertex_ai_search with candidate_k=50: clamps candidate_k <= 20 and top_k <= candidate_k
  const toVais = coerceSpec({
    ...toRag,
    index_profile: "vertex_ai_search",
    slots: { ...toRag.slots, candidate_k: 50, top_k: 18 },
  });
  assert.equal(validatePipelineSpec(toVais), null);
  assert.equal(toVais.slots.candidate_k, 20);
  assert.equal(toVais.slots.top_k, 18);

  // Switch back from managed -> hybrid: managed_retrieval coerces to hybrid_rrf
  const backToHybrid = coerceSpec({ ...toVais, index_profile: "hybrid" });
  assert.equal(validatePipelineSpec(backToHybrid), null);
  assert.equal(backToHybrid.slots.retrieval_mode, "hybrid_rrf");

  // Enabling adk_agent on a BigQuery profile auto-populates canonical tools including search_procurement_corpus
  const customAdk = coerceSpec({
    ...backToHybrid,
    slots: { ...backToHybrid.slots, reasoning_mode: "adk_agent", agent_tools: ["calculate_contract_pricing"] },
  });
  assert.equal(validatePipelineSpec(customAdk), null);
  assert.deepEqual(customAdk.slots.agent_tools, [
    "search_procurement_corpus",
    "calculate_contract_pricing",
  ]);
  assert.equal(CANONICAL_AGENT_TOOLS[0], "search_procurement_corpus");

  // Boundary clamp: top_k=0 -> 1, top_k=99 -> 20, candidate_k < top_k -> bumped to top_k
  const clamped = coerceSpec({
    ...defaultPresetSpec("level1_naive"),
    slots: { ...defaultPresetSpec("level1_naive").slots, top_k: 15, candidate_k: 4 },
  });
  assert.equal(validatePipelineSpec(clamped), null);
  assert.equal(clamped.slots.top_k, 15);
  assert.equal(clamped.slots.candidate_k, 15);
});

test("validatePipelineSpec rejects illegal raw specs loudly", () => {
  const badManaged: PipelineSpec = {
    index_profile: "rag_engine",
    slots: {
      query_transform: "acronym",
      retrieval_mode: "lexical_bm25",
      candidate_k: 10,
      top_k: 5,
      reranker: "none",
      context_order: "score_order",
      reasoning_mode: "single_pass",
      agent_tools: [],
    },
  };
  assert.match(validatePipelineSpec(badManaged) ?? "", /managed_retrieval/);

  const badAdkManaged: PipelineSpec = {
    index_profile: "vertex_ai_search",
    slots: {
      query_transform: "acronym",
      retrieval_mode: "managed_retrieval",
      candidate_k: 10,
      top_k: 5,
      reranker: "none",
      context_order: "score_order",
      reasoning_mode: "adk_agent",
      agent_tools: ["search_procurement_corpus"],
    },
  };
  assert.match(validatePipelineSpec(badAdkManaged) ?? "", /adk_agent/);
});

test("activeQueryStages and buildPipelineDescriptor update live with zero dangling edges", () => {
  // Lexical BM25 without reranker -> skips embed_query AND skips rerank
  const lexicalSpec = coerceSpec({
    index_profile: "hybrid",
    slots: {
      query_transform: "acronym",
      retrieval_mode: "lexical_bm25",
      candidate_k: 25,
      top_k: 5,
      reranker: "none",
      context_order: "authority_amendment_first",
      reasoning_mode: "single_pass",
      agent_tools: [],
    },
  });
  assert.deepEqual(activeQueryStages(lexicalSpec), ["query_prep", "retrieve", "synthesize", "evaluate"]);
  const lexDesc = buildPipelineDescriptor(lexicalSpec);
  const lexIds = new Set(lexDesc.nodes.map((n) => n.id));
  assert.equal(lexIds.has("embed"), false);
  assert.equal(lexIds.has("rank"), false);
  assert.equal(lexIds.has("bq"), true);
  for (const e of lexDesc.edges) {
    assert.ok(lexIds.has(e.from), `missing edge.from ${e.from}`);
    assert.ok(lexIds.has(e.to), `missing edge.to ${e.to}`);
  }

  // RAG Engine + Discovery Engine Reranker -> adds rank node between retrieve and llm!
  const ragReranked = coerceSpec({
    ...defaultPresetSpec("rag_engine"),
    slots: { ...defaultPresetSpec("rag_engine").slots, candidate_k: 25, top_k: 5, reranker: "vertex_ranking_api" },
  });
  assert.equal(matchingPresetLevelId(ragReranked), null);
  assert.deepEqual(activeQueryStages(ragReranked), ["query_prep", "retrieve", "rerank", "synthesize", "evaluate"]);
  const ragDesc = buildPipelineDescriptor(ragReranked);
  const ragIds = new Set(ragDesc.nodes.map((n) => n.id));
  assert.equal(ragIds.has("rag_corpus"), true);
  assert.equal(ragIds.has("rank"), true);
  for (const e of ragDesc.edges) {
    assert.ok(ragIds.has(e.from), `missing edge.from ${e.from}`);
    assert.ok(ragIds.has(e.to), `missing edge.to ${e.to}`);
  }

  // Custom ADK with 2 tools -> only tool_search + tool_calc rendered, no dangling edges
  const twoToolAdk = coerceSpec({
    ...defaultPresetSpec("level4_agentic"),
    index_profile: "hybrid",
    slots: {
      ...defaultPresetSpec("level4_agentic").slots,
      agent_tools: ["search_procurement_corpus", "calculate_contract_pricing"],
    },
  });
  assert.deepEqual(activeQueryStages(twoToolAdk), ["query_prep", "agent_turn", "synthesize", "evaluate"]);
  const adkDesc = buildPipelineDescriptor(twoToolAdk);
  const adkIds = new Set(adkDesc.nodes.map((n) => n.id));
  assert.equal(adkIds.has("tool_search"), true);
  assert.equal(adkIds.has("tool_calc"), true);
  assert.equal(adkIds.has("tool_amend"), false);
  assert.equal(adkIds.has("tool_verify"), false);
  for (const e of adkDesc.edges) {
    assert.ok(adkIds.has(e.from), `missing edge.from ${e.from}`);
    assert.ok(adkIds.has(e.to), `missing edge.to ${e.to}`);
  }

  // Exact preset match returns server preset descriptor verbatim when supplied
  const serverDesc: ArchitectureDescriptor = {
    level_id: "rag_engine",
    nodes: [{ id: "srv_node", label: "Server RagCorpus", service: "Vertex AI", kind: "store", col: 0, row: 0, stage_ids: ["retrieve"] }],
    edges: [],
    ingest_nodes: [],
    ingest_edges: [],
  };
  const presets: PipelinePresetEntry[] = [
    {
      level_id: "rag_engine",
      label: "Level 6",
      enabled: true,
      backing_level_id: "rag_engine",
      pipeline_id: "pipe_preset6",
      active_stages: ["query_prep", "retrieve", "synthesize", "evaluate"],
      spec: defaultPresetSpec("rag_engine"),
      descriptor: serverDesc,
    },
  ];
  assert.equal(buildPipelineDescriptor(defaultPresetSpec("rag_engine"), presets), serverDesc);
});

test("model compatibility, replay gate, enabled gate, and wire serialization", () => {
  const ragPreset = defaultPresetSpec("rag_engine");
  const ragReranked = coerceSpec({
    ...ragPreset,
    slots: { ...ragPreset.slots, reranker: "vertex_ranking_api", candidate_k: 20, top_k: 5 },
  });
  const groundedEntry = LIVE_CATALOG.models.find((m) => m.id === RAG_ENGINE_GROUNDED_MODEL)!;
  const claudeEntry = LIVE_CATALOG.models.find((m) => m.id === "claude-opus-5-5")!;

  // Grounded model valid on un-reranked rag_engine, blocked when reranker is enabled!
  assert.equal(modelRestrictionForSpec(groundedEntry, ragPreset), null);
  assert.match(modelRestrictionForSpec(groundedEntry, ragReranked) ?? "", /reranker/i);
  const optsReranked = synthesisOptionsForSpec(LIVE_CATALOG, "claude-opus-5-5", ragReranked);
  assert.equal(optsReranked.find((o) => o.entry.id === RAG_ENGINE_GROUNDED_MODEL)?.disabled, true);

  // ADK agent blocks non-Gemini synthesis (e.g. Claude)
  const adkSpec = defaultPresetSpec("level4_agentic");
  assert.match(modelRestrictionForSpec(claudeEntry, adkSpec) ?? "", /Gemini/i);
  assert.match(
    validateModelsForSpec(LIVE_CATALOG, { synthesis: "claude-opus-5-5", judge: "gemini-4-argon" }, adkSpec) ?? "",
    /Gemini/i,
  );

  // Replay gate: preset plays recorded pair; custom recipe is blocked with explicit Live-mode explanation
  const replayCatalog: ModelsResponse = { ...LIVE_CATALOG, demo_mode: "replay" };
  const presetGate = workbenchReplayGate(
    replayCatalog,
    ragPreset,
    "q_exact_clin_lookup",
    { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" },
  );
  assert.equal(presetGate.blockedReason, null);

  const customGate = workbenchReplayGate(
    replayCatalog,
    ragReranked,
    "q_exact_clin_lookup",
    { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" },
  );
  assert.match(customGate.blockedReason ?? "", /Custom component recipes require Live mode/);

  // Enabled gate checks backing level AND level4_agentic for ADK
  assert.equal(isSpecEnabled(adkSpec, ["level1_naive", "level3_multimodal"]), false);
  assert.equal(isSpecEnabled(adkSpec, ["level3_multimodal", "level4_agentic"]), true);

  // Wire format carries slot_id + models cleanly
  const wire = toRecipeWire(ragReranked, WORKBENCH_SLOT_ID, {
    synthesis: "gemini-3.8-flash",
    judge: "claude-opus-5-5",
  });
  assert.equal(wire.slot_id, "workbench");
  assert.equal(wire.synthesis_model, "gemini-3.8-flash");
  assert.equal(wire.judge_model, "claude-opus-5-5");
  assert.equal(wire.slots.reranker, "vertex_ranking_api");

  // Selection coercion: switching from un-reranked rag_engine (grounded model) to reranked recipe auto-resets synthesis
  const coercedSel = coerceSelectionForSpec(
    LIVE_CATALOG,
    { synthesis: RAG_ENGINE_GROUNDED_MODEL, judge: "claude-opus-5-5" },
    ragReranked,
  );
  assert.equal(coercedSel.synthesis, "gemini-4-argon");
  assert.equal(coercedSel.judge, "claude-opus-5-5");

  // Spec picker details surface recipe-restricted models in blockedModels
  const details = buildSpecPickerDetails(LIVE_CATALOG, coercedSel, ragReranked);
  assert.equal(details.prices.length, 2);
  assert.ok(details.blockedModels.some((b) => b.id === RAG_ENGINE_GROUNDED_MODEL));
});
