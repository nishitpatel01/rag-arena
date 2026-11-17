/**
 * Slice #2 (v1.6.1) — Google Cloud Model Armor Guardrails in Composable Workbench (`npm test`).
 * Covers:
 *   1. Backward compatibility of all 8 Level presets (`guardrail: "none"` by default).
 *   2. `guardrail: "model_armor"` coercion, validation, wire serialization, and summary labels.
 *   3. Live `activeQueryStages` (`guardrail_in`, `guardrail_out`) and `buildPipelineDescriptor`
 *      (`model_armor` node + zero dangling edges across single-pass and ADK agent modes).
 *   4. Automatic synthesis model restriction/coercion when `guardrail === "model_armor"`.
 *   5. Honest `workbenchReplayGate` message + `buildPromptInjectionScenario` helper for replay vs live.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  GUARDRAIL_MODES,
  PROMPT_INJECTION_TEST_PROMPT,
  WORKBENCH_SLOT_ID,
  activeQueryStages,
  applyPromptInjectionTest,
  buildModelArmorBanner,
  buildPipelineDescriptor,
  coerceSelectionForSpec,
  coerceSpec,
  defaultPresetSpec,
  matchingPresetLevelId,
  modelRestrictionForSpec,
  pipelineSummaryLabel,
  toRecipeWire,
  validatePipelineSpec,
  workbenchReplayGate,
} from "../src/features/workbench/pipelineSpecModel";
import {
  MANAGED_SUMMARY_MODEL,
  RAG_ENGINE_GROUNDED_MODEL,
} from "../src/features/levels/modelSelection";
import {
  LEVEL_IDS,
  type ModelCatalogEntry,
  type ModelsResponse,
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
  input_usd_per_1m: 0.75,
  output_usd_per_1m: 3.75,
  price_source: null,
  price_as_of: null,
  available: true,
  unavailable_reason: null,
  verified_at: null,
  ...extra,
});

const REPLAY_CATALOG: ModelsResponse = {
  models: [
    mkModel("gemini-4-argon", "vertex_gemini", ["synthesis", "judge"]),
    mkModel("claude-opus-5-5", "anthropic_vertex", ["synthesis", "judge"]),
    mkModel(MANAGED_SUMMARY_MODEL, "vertex_ai_search", ["synthesis"]),
    mkModel(RAG_ENGINE_GROUNDED_MODEL, "vertex_rag_engine", ["synthesis"]),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "replay",
  recorded_pairs: [
    {
      engine: "level2_hybrid",
      synthesis_model: "gemini-4-argon",
      judge_model: "claude-opus-5-5",
      question_ids: ["q_exact_clin_lookup"],
    },
  ],
};

test("all 8 presets default to guardrail=none and preserve 1:1 preset matching", () => {
  assert.deepEqual(GUARDRAIL_MODES, ["none", "model_armor"]);
  for (const levelId of LEVEL_IDS) {
    const preset = defaultPresetSpec(levelId);
    assert.equal(preset.guardrail ?? "none", "none");
    assert.equal(matchingPresetLevelId(preset), levelId);
    assert.equal(validatePipelineSpec(preset), null);
  }
});

test("enabling guardrail=model_armor switches preset to custom recipe, adds stages, and renders Model Armor node", () => {
  const l2 = defaultPresetSpec("level2_hybrid");
  const armored = coerceSpec({ ...l2, guardrail: "model_armor" });
  assert.equal(armored.guardrail, "model_armor");
  assert.equal(matchingPresetLevelId(armored), null);
  assert.equal(validatePipelineSpec(armored), null);
  assert.match(pipelineSummaryLabel(armored), /model_armor/);

  assert.deepEqual(activeQueryStages(armored), [
    "query_prep",
    "embed_query",
    "retrieve",
    "rerank",
    "guardrail_in",
    "synthesize",
    "guardrail_out",
    "evaluate",
  ]);

  const desc = buildPipelineDescriptor(armored);
  const armorNode = desc.nodes.find((n) => n.id === "model_armor");
  assert.ok(armorNode, "expected model_armor node in single-pass diagram");
  assert.equal(armorNode.label, "Model Armor");
  assert.deepEqual(armorNode.stage_ids, ["guardrail_in", "guardrail_out"]);
  const ids = new Set(desc.nodes.map((n) => n.id));
  for (const e of desc.edges) {
    assert.ok(ids.has(e.from), `missing edge source ${e.from}`);
    assert.ok(ids.has(e.to), `missing edge target ${e.to}`);
  }

  // ADK agent + Model Armor also renders Model Armor node with zero dangling edges
  const l4Armored = coerceSpec({ ...defaultPresetSpec("level4_agentic"), guardrail: "model_armor" });
  assert.deepEqual(activeQueryStages(l4Armored), [
    "query_prep",
    "guardrail_in",
    "agent_turn",
    "synthesize",
    "guardrail_out",
    "evaluate",
  ]);
  const adkDesc = buildPipelineDescriptor(l4Armored);
  assert.ok(adkDesc.nodes.some((n) => n.id === "model_armor"));
  const adkIds = new Set(adkDesc.nodes.map((n) => n.id));
  for (const e of adkDesc.edges) {
    assert.ok(adkIds.has(e.from), `missing ADK edge source ${e.from}`);
    assert.ok(adkIds.has(e.to), `missing ADK edge target ${e.to}`);
  }

  // Wire format carries guardrail explicitly
  const wire = toRecipeWire(armored, WORKBENCH_SLOT_ID, {
    synthesis: "gemini-4-argon",
    judge: "claude-opus-5-5",
  });
  assert.equal(wire.guardrail, "model_armor");

  // Invalid raw guardrail is rejected by validatePipelineSpec and coerced to "none" by coerceSpec
  const rawInvalid = { ...l2, guardrail: "bogus" as unknown as PipelineSpec["guardrail"] };
  assert.match(validatePipelineSpec(rawInvalid) ?? "", /guardrail/i);
  assert.equal(coerceSpec(rawInvalid).guardrail ?? "none", "none");
});

test("modelRestrictionForSpec and coerceSelectionForSpec disallow inline managed synthesis with Model Armor", () => {
  const ragArmored = coerceSpec({ ...defaultPresetSpec("rag_engine"), guardrail: "model_armor" });
  const groundedEntry = REPLAY_CATALOG.models.find((m) => m.id === RAG_ENGINE_GROUNDED_MODEL)!;
  assert.match(modelRestrictionForSpec(groundedEntry, ragArmored) ?? "", /Model Armor|guardrail/i);

  const coerced = coerceSelectionForSpec(
    { ...REPLAY_CATALOG, demo_mode: "live" },
    { synthesis: RAG_ENGINE_GROUNDED_MODEL, judge: "claude-opus-5-5" },
    ragArmored,
  );
  assert.equal(coerced.synthesis, "gemini-4-argon");
});

test("Try Prompt-Injection Test helper and replay banner behave honestly in replay vs live mode", () => {
  const applied = applyPromptInjectionTest(defaultPresetSpec("level2_hybrid"));
  assert.equal(applied.spec.guardrail, "model_armor");
  assert.equal(applied.query, PROMPT_INJECTION_TEST_PROMPT);
  assert.equal(applied.questionId, "");
  assert.match(PROMPT_INJECTION_TEST_PROMPT, /SYSTEM OVERRIDE/i);

  const gate = workbenchReplayGate(
    REPLAY_CATALOG,
    applied.spec,
    "q_exact_clin_lookup",
    { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" },
  );
  assert.equal(gate.replay, true);
  assert.match(gate.blockedReason ?? "", /Model Armor/i);
  assert.match(gate.blockedReason ?? "", /Live mode/i);

  const replayBanner = buildModelArmorBanner("replay", applied.spec, true);
  assert.ok(replayBanner);
  assert.equal(replayBanner.tone, "warn");
  assert.match(replayBanner.body, /never simulates|offline/i);
  assert.match(replayBanner.body, /provision_model_armor\.py --apply/);

  const liveBanner = buildModelArmorBanner("live", applied.spec, true);
  assert.ok(liveBanner);
  assert.equal(liveBanner.tone, "info");
  assert.match(liveBanner.body, /sanitizeUserPrompt/);

  assert.equal(buildModelArmorBanner("replay", defaultPresetSpec("level2_hybrid"), false), null);
});
