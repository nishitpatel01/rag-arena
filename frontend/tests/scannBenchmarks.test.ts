/**
 * Release 1.4.0 — Ephemeral Operational & High-QPS ScaNN Benchmarks (`npm test`).
 * Covers:
 *   1. Levels 7 (`alloydb_scann`) & 8 (`vertex_vector_v2`) in `LEVEL_IDS`, `SCANN_INDEX_PROFILES`,
 *      layout source labels, and 8-level `/compare` choices (`MAX_COMPARE_LEVELS = 8`).
 *   2. Workbench `PipelineSpec` presets, cross-slot coercion (ADK -> ScaNN single_pass, retrieval modes
 *      `vector_cosine` | `lexical_bm25` | `hybrid_rrf`, `candidate_k <= 100`), and 0-ms live
 *      `ArchitectureDescriptor` construction (`alloydb` / `vvs2` store nodes, zero dangling edges).
 *   3. Ephemeral `/ingest/status` run gating (`replay` vs live idle `$0/mo` vs live configured),
 *      `tierCountFor` isolation from Level 5 VAIS document counts, `ingestPillText`, and upload-scope
 *      gating (`applyUploadScopeToChoices` + `uploadRunGateFor`).
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  SCANN_INDEX_PROFILES,
  activeQueryStages,
  backingLevelForProfile,
  buildPipelineDescriptor,
  coerceSpec,
  defaultPresetSpec,
  isScannProfile,
  matchingPresetLevelId,
  validatePipelineSpec,
} from "../src/features/workbench/pipelineSpecModel";
import {
  TIER_FOR_LEVEL,
  ephemeralScannFor,
  ingestPillText,
  isEphemeralScannLevel,
  isManagedLevel,
  runGateFor,
  tierCountFor,
} from "../src/features/ingest/ingestStatus";
import { MAX_COMPARE_LEVELS, levelChoices } from "../src/features/compare/compareModel";
import { applyUploadScopeToChoices, uploadRunGateFor } from "../src/features/uploads/uploadModel";
import { isLayoutSource, layoutSourceLabel } from "../src/lib/elements";
import { LEVEL_IDS, type IngestStatusResponse, type LevelDescriptor, type ModelsResponse } from "../src/types/arena";

test("LEVEL_IDS, SCANN_INDEX_PROFILES, layoutSource, and Compare max include Levels 7 and 8", () => {
  assert.deepEqual(LEVEL_IDS.slice(6), ["alloydb_scann", "vertex_vector_v2"]);
  assert.deepEqual(SCANN_INDEX_PROFILES, ["alloydb_scann", "vertex_vector_v2"]);
  assert.equal(MAX_COMPARE_LEVELS, 8);
  assert.equal(isScannProfile("alloydb_scann"), true);
  assert.equal(isScannProfile("vertex_vector_v2"), true);
  assert.equal(isScannProfile("hybrid"), false);
  assert.equal(isLayoutSource("alloydb_scann"), true);
  assert.equal(isLayoutSource("vertex_vector_v2"), true);
  assert.equal(layoutSourceLabel("alloydb_scann"), "AlloyDB + ScaNN");
  assert.equal(layoutSourceLabel("vertex_vector_v2"), "Vector Search 2.0");
});

test("Levels 7 & 8 Workbench presets validate, coerce ADK to single_pass, and render store nodes cleanly", () => {
  for (const levelId of ["alloydb_scann", "vertex_vector_v2"] as const) {
    const preset = defaultPresetSpec(levelId);
    assert.equal(validatePipelineSpec(preset), null);
    assert.equal(matchingPresetLevelId(preset), levelId);
    assert.equal(backingLevelForProfile(preset.index_profile), levelId);
    assert.equal(preset.slots.retrieval_mode, "hybrid_rrf");
    assert.equal(preset.slots.reranker, "vertex_ranking_api");
    assert.equal(preset.slots.candidate_k, 25);
    assert.equal(preset.slots.top_k, 5);
    const maxKSpec = coerceSpec({ ...preset, slots: { ...preset.slots, candidate_k: 150 } });
    assert.equal(maxKSpec.slots.candidate_k, 100);
    assert.deepEqual(activeQueryStages(preset), [
      "query_prep",
      "embed_query",
      "retrieve",
      "rerank",
      "synthesize",
      "evaluate",
    ]);

    // Disabling reranker on ScaNN profile coerces lexical_bm25 -> hybrid_rrf, keeps embed_query, drops rank node, zero dangling edges
    const noRerankSpec = coerceSpec({
      ...preset,
      slots: { ...preset.slots, retrieval_mode: "lexical_bm25", reranker: "none" },
    });
    assert.equal(validatePipelineSpec(noRerankSpec), null);
    assert.equal(noRerankSpec.slots.retrieval_mode, "hybrid_rrf");
    assert.deepEqual(activeQueryStages(noRerankSpec), ["query_prep", "embed_query", "retrieve", "synthesize", "evaluate"]);
    const desc = buildPipelineDescriptor(noRerankSpec);
    const ids = new Set(desc.nodes.map((n) => n.id));
    assert.equal(ids.has(levelId === "alloydb_scann" ? "alloydb" : "vvs2"), true);
    assert.equal(ids.has("embed"), true);
    assert.equal(ids.has("rank"), false);
    for (const edge of desc.edges) {
      assert.ok(ids.has(edge.from), `missing edge.from ${edge.from}`);
      assert.ok(ids.has(edge.to), `missing edge.to ${edge.to}`);
    }
  }

  // Coercing Level 4 ADK -> alloydb_scann resets reasoning_mode to single_pass and clears agent_tools
  const fromAdk = coerceSpec({
    ...defaultPresetSpec("level4_agentic"),
    index_profile: "alloydb_scann",
  });
  assert.equal(validatePipelineSpec(fromAdk), null);
  assert.equal(fromAdk.slots.reasoning_mode, "single_pass");
  assert.deepEqual(fromAdk.slots.agent_tools, []);

  // Raw illegal spec: adk_agent or non-hybrid_rrf retrieval_mode on ScaNN profile fails validation loudly
  assert.match(
    validatePipelineSpec({
      ...defaultPresetSpec("alloydb_scann"),
      slots: { ...defaultPresetSpec("alloydb_scann").slots, retrieval_mode: "managed_retrieval" },
    }) ?? "",
    /hybrid_rrf/i,
  );
  assert.match(
    validatePipelineSpec({
      ...defaultPresetSpec("vertex_vector_v2"),
      slots: {
        ...defaultPresetSpec("vertex_vector_v2").slots,
        reasoning_mode: "adk_agent",
        agent_tools: ["search_procurement_corpus"],
      },
    }) ?? "",
    /adk_agent/i,
  );
});

const mkStatus = (patch: Partial<IngestStatusResponse>): IngestStatusResponse => ({
  counts_by_tier: { naive: 5067, hybrid: 1384, multimodal: 1412 },
  index_states: {},
  last_runs: [],
  managed_vertex: { available: true, document_count: 45, data_store_state: "ACTIVE" },
  ...patch,
});

test("ingestStatus gates Levels 7–8 cleanly across replay, live idle ($0/mo), and live configured", () => {
  assert.equal(TIER_FOR_LEVEL.alloydb_scann, null);
  assert.equal(TIER_FOR_LEVEL.vertex_vector_v2, null);
  assert.equal(isEphemeralScannLevel("alloydb_scann"), true);
  assert.equal(isEphemeralScannLevel("vertex_vector_v2"), true);
  assert.equal(isEphemeralScannLevel("rag_engine"), false);
  assert.equal(isManagedLevel("alloydb_scann"), false);

  // Replay mode: unblocked even when BigQuery ingest status has available === false
  const replay = mkStatus({
    demo_mode: "replay",
    available: false,
    reason: "DEMO_MODE=replay: no GCP calls",
    ephemeral_scann: {
      alloydb_scann: { enabled: true, ephemeral: true, configured: true, script: "scripts/ephemeral_alloydb.sh" },
      vertex_vector_v2: { enabled: true, ephemeral: true, configured: true, script: "scripts/ephemeral_vvs2.sh" },
    },
  });
  // Must NOT leak Level 5 VAIS document_count (45) into tierCountFor(L7/L8)
  assert.equal(tierCountFor("alloydb_scann", replay), null);
  assert.equal(tierCountFor("vertex_vector_v2", replay), null);

  const gReplay = runGateFor("alloydb_scann", replay, null);
  assert.equal(gReplay.blocked, false);
  assert.equal(gReplay.tone, "ok");
  assert.equal(ingestPillText("alloydb_scann", gReplay, replay), "ephemeral benchmark · replay ($0/mo idle)");

  // Live mode idle ($0/mo): blocked with explicit CLI script guidance
  const liveIdle = mkStatus({
    demo_mode: "live",
    available: true,
    ephemeral_scann: {
      alloydb_scann: {
        enabled: true,
        ephemeral: true,
        configured: false,
        script: "scripts/ephemeral_alloydb.sh",
        reason: "ALLOYDB_DSN is unset ($0/mo idle) — run scripts/ephemeral_alloydb.sh --up (or --omni) for live runs",
      },
      vertex_vector_v2: {
        enabled: true,
        ephemeral: true,
        configured: false,
        script: "scripts/ephemeral_vvs2.sh",
        reason: "VVS2_COLLECTION is unset ($0/mo idle) — run scripts/ephemeral_vvs2.sh --up for live runs",
      },
    },
  });
  assert.equal(ephemeralScannFor("alloydb_scann", liveIdle)?.configured, false);
  const gIdle = runGateFor("alloydb_scann", liveIdle, null);
  assert.equal(gIdle.blocked, true);
  assert.equal(gIdle.tone, "warn");
  assert.match(gIdle.note ?? "", /ephemeral_alloydb\.sh/);
  assert.match(ingestPillText("alloydb_scann", gIdle, liveIdle), /ephemeral_alloydb\.sh/);

  // Live mode configured (ephemeral cluster/index active): unblocked
  const liveReady = mkStatus({
    demo_mode: "live",
    available: true,
    ephemeral_scann: {
      alloydb_scann: { enabled: true, ephemeral: true, configured: true, script: "scripts/ephemeral_alloydb.sh" },
      vertex_vector_v2: { enabled: true, ephemeral: true, configured: true, script: "scripts/ephemeral_vvs2.sh" },
    },
  });
  const gReady = runGateFor("vertex_vector_v2", liveReady, null);
  assert.equal(gReady.blocked, false);
  assert.equal(gReady.tone, "ok");
  assert.equal(ingestPillText("vertex_vector_v2", gReady, liveReady), "ephemeral benchmark · live configured");
});

test("upload workspaces explicitly block Ephemeral ScaNN Levels 7–8 with corpus guidance", () => {
  const ws = {
    id: "upl_abcdef012345",
    created_at: "2026-11-04T00:00:00Z",
    files: [{ id: "f_abcdef012345", name: "RFP.pdf", status: "ready" as const, pages: 4 }],
    levels: {
      level1_naive: { status: "completed" as const, chunks: 12 },
      alloydb_scann: { status: "completed" as const, chunks: 12 },
    },
  };
  const g7 = uploadRunGateFor("alloydb_scann", ws);
  assert.equal(g7.blocked, true);
  assert.match(g7.note ?? "", /Standing Levels 1–6/);

  const g8 = uploadRunGateFor("vertex_vector_v2", ws);
  assert.equal(g8.blocked, true);
  assert.match(g8.note ?? "", /Ephemeral ScaNN Levels 7–8/);

  const eightLevels: LevelDescriptor[] = LEVEL_IDS.map((id, i) => ({
    level_id: id,
    label: `Level ${i + 1}`,
    order: i + 1,
    enabled: true,
    stages: [],
    architecture: { level_id: id, nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] },
  }));
  const catalog: ModelsResponse = {
    models: [
      {
        id: "gemini-4-argon",
        label: "Gemini 4 Argon",
        provider: "vertex_gemini",
        location: "us-central1",
        roles: ["synthesis", "judge"],
        input_usd_per_1m: 1,
        output_usd_per_1m: 2,
        price_source: null,
        price_as_of: null,
        available: true,
        unavailable_reason: null,
        verified_at: null,
      },
      {
        id: "claude-opus-5-5",
        label: "Claude Opus 5.5",
        provider: "anthropic_vertex",
        location: "global",
        roles: ["synthesis", "judge"],
        input_usd_per_1m: 1,
        output_usd_per_1m: 2,
        price_source: null,
        price_as_of: null,
        available: true,
        unavailable_reason: null,
        verified_at: null,
      },
    ],
    defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
    demo_mode: "live",
    recorded_pairs: [],
  };
  const baseChoices = levelChoices(eightLevels, catalog, { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" }, "q1");
  const scoped = applyUploadScopeToChoices(baseChoices, ws);
  assert.equal(scoped.find((c) => c.levelId === "level1_naive")?.disabledReason, null);
  assert.match(scoped.find((c) => c.levelId === "alloydb_scann")?.disabledReason ?? "", /Standing Levels 1–6/);
  assert.match(scoped.find((c) => c.levelId === "vertex_vector_v2")?.disabledReason ?? "", /Standing Levels 1–6/);
});
