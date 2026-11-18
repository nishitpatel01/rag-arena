/**
 * TDD suite for the deterministic Model Pair Recommender, GlobalModelPopover,
 * ModelPicker recommender row, and PROVIDER_LABELS (`vertex_genai_eval`).
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server.browser";
import { DEV_MODELS_FIXTURE } from "../src/dev/fixtures/models";
import { ModelPicker } from "../src/features/levels/ModelPicker";
import { PROVIDER_LABELS, providerLabel } from "../src/features/levels/modelSelection";
import { MODELS_CHANGED_EVENT } from "../src/features/levels/useModelCatalog";
import { GlobalModelPopover, staticModelCatalogState } from "../src/features/models/GlobalModelPopover";
import {
  PROFILE_ANTHROPIC_WORKER,
  PROFILE_BALANCED_ENTERPRISE,
  PROFILE_COST_LATENCY,
  PROFILE_MULTIMODAL_AGENTIC,
  RECOMMENDATION_PROFILE_IDS,
  RECOMMENDED_PROFILES,
  VERTEX_GENAI_EVAL_MODEL_ID,
  isCrossFamilyPair,
  modelFamily,
  recommendModelPair,
  recommendedPairsForCatalog,
  shortModelLabel,
} from "../src/features/models/modelRecommender";
import type { ModelsResponse } from "../src/types/arena";

test("canonical recommender profiles enforce cross-family distinct pairs and match backend parity", () => {
  assert.deepEqual(
    RECOMMENDED_PROFILES.map((p) => p.profile_id),
    [...RECOMMENDATION_PROFILE_IDS]
  );
  const byId = Object.fromEntries(RECOMMENDED_PROFILES.map((p) => [p.profile_id, p]));
  assert.equal(byId[PROFILE_COST_LATENCY].synthesis_model, "gemini-4-hydrogen");
  assert.equal(byId[PROFILE_COST_LATENCY].judge_model, "claude-sonnet-4-6");
  assert.equal(byId[PROFILE_COST_LATENCY].alt_judge_model, VERTEX_GENAI_EVAL_MODEL_ID);

  assert.equal(byId[PROFILE_BALANCED_ENTERPRISE].synthesis_model, "gemini-4-carbon");
  assert.equal(byId[PROFILE_BALANCED_ENTERPRISE].judge_model, "claude-opus-5-5");

  assert.equal(byId[PROFILE_MULTIMODAL_AGENTIC].synthesis_model, "gemini-4-argon");
  assert.equal(byId[PROFILE_MULTIMODAL_AGENTIC].judge_model, "claude-opus-5-5");

  assert.equal(byId[PROFILE_ANTHROPIC_WORKER].synthesis_model, "claude-sonnet-4-6");
  assert.equal(byId[PROFILE_ANTHROPIC_WORKER].judge_model, "gemini-4-argon");
  assert.equal(byId[PROFILE_ANTHROPIC_WORKER].adk_compatible, false);

  for (const p of RECOMMENDED_PROFILES) {
    assert.notEqual(p.synthesis_model, p.judge_model);
    assert.equal(isCrossFamilyPair(p.synthesis_model, p.judge_model), true);
  }
  assert.equal(isCrossFamilyPair("gemini-4-hydrogen", "gemini-4-argon"), false);
  assert.equal(isCrossFamilyPair("gemini-4-hydrogen", VERTEX_GENAI_EVAL_MODEL_ID), true);
  assert.equal(PROVIDER_LABELS.vertex_genai_eval, "Vertex AI GenAI Eval");
  assert.equal(providerLabel("vertex_genai_eval"), "Vertex AI GenAI Eval");
  assert.equal(MODELS_CHANGED_EVENT, "rag-arena:models-changed");
});

test("recommendModelPair infers workload profiles, enforces Gemini-only ADK, and respects availability", () => {
  const highQps = recommendModelPair({ monthlyQueries: 1_000_000, latencySla: "sub_second_retrieval" });
  assert.equal(highQps.profile_id, PROFILE_COST_LATENCY);
  assert.equal(highQps.synthesis_model, "gemini-4-hydrogen");
  assert.equal(highQps.judge_model, "claude-sonnet-4-6");

  const bq = recommendModelPair({ dataGravity: "bigquery_warehouse", docComplexity: "plain_text" });
  assert.equal(bq.profile_id, PROFILE_BALANCED_ENTERPRISE);
  assert.equal(bq.synthesis_model, "gemini-4-carbon");

  // Level 8 ADK must override anthropic_worker to Gemini 4 Argon
  const adk = recommendModelPair({ levelId: "level4_agentic", profileId: PROFILE_ANTHROPIC_WORKER });
  assert.equal(modelFamily(adk.synthesis_model), "gemini");
  assert.equal(adk.synthesis_model, "gemini-4-argon");
  assert.equal(adk.judge_model, "claude-opus-5-5");
  assert.equal(adk.adk_compatible, true);
  assert.match(adk.rationale, /ADK/);

  // Unavailable preferred models fall back honestly to available alt_judge_model (vertex-genai-eval) or cross-family judge
  const degraded: ModelsResponse = {
    ...DEV_MODELS_FIXTURE,
    models: DEV_MODELS_FIXTURE.models.map((m) =>
      m.id === "gemini-4-hydrogen" || m.id === "claude-sonnet-4-6"
        ? { ...m, available: false, unavailable_reason: "simulated outage" }
        : m
    ),
  };
  const fallbackWithEval = recommendModelPair({ profileId: PROFILE_COST_LATENCY, catalog: degraded });
  assert.ok(fallbackWithEval.synthesis_model === "gemini-4-argon" || fallbackWithEval.synthesis_model === "gemini-4-carbon");
  assert.equal(fallbackWithEval.judge_model, VERTEX_GENAI_EVAL_MODEL_ID);
  assert.equal(fallbackWithEval.cross_family, true);

  const degradedNoEval: ModelsResponse = {
    ...degraded,
    models: degraded.models.map((m) =>
      m.id === VERTEX_GENAI_EVAL_MODEL_ID ? { ...m, available: false, unavailable_reason: "no recordings" } : m
    ),
  };
  const fallbackOpus = recommendModelPair({ profileId: PROFILE_COST_LATENCY, catalog: degradedNoEval });
  assert.equal(fallbackOpus.judge_model, "claude-opus-5-5");
  assert.equal(fallbackOpus.cross_family, true);

  assert.throws(() => recommendModelPair({ profileId: "nonexistent" }));
  assert.equal(recommendedPairsForCatalog(DEV_MODELS_FIXTURE).length, 4);
  assert.equal(shortModelLabel(DEV_MODELS_FIXTURE, "gemini-4-hydrogen"), "Gemini 4 Hydrogen");
});

test("GlobalModelPopover and ModelPicker render recommender presets and cross-family controls cleanly", () => {
  const state = staticModelCatalogState(DEV_MODELS_FIXTURE, {
    synthesis: "gemini-4-hydrogen",
    judge: "claude-sonnet-4-6",
  });

  const popoverHtml = renderToStaticMarkup(
    React.createElement(GlobalModelPopover, { modelState: state, defaultOpen: true })
  );
  assert.match(popoverHtml, /data-testid="global-model-trigger"/);
  assert.match(popoverHtml, /Gemini 4 Hydrogen · Claude Sonnet 4\.6/);
  assert.match(popoverHtml, /data-testid="global-model-popover"/);
  assert.match(popoverHtml, /data-testid="global-model-preset-cost_latency"/);
  assert.match(popoverHtml, /data-testid="global-model-preset-balanced_enterprise"/);
  assert.match(popoverHtml, /data-testid="global-model-preset-multimodal_agentic"/);
  assert.match(popoverHtml, /data-testid="global-model-preset-anthropic_worker"/);
  assert.match(popoverHtml, /Vertex AI GenAI Eval/);

  const pickerHtml = renderToStaticMarkup(
    React.createElement(ModelPicker, {
      catalog: DEV_MODELS_FIXTURE,
      loading: false,
      error: null,
      selection: { synthesis: "gemini-4-hydrogen", judge: "claude-sonnet-4-6" },
      onChange: () => {},
      disabled: false,
      validationError: null,
      replay: null,
      levelId: "level4_agentic",
    })
  );
  assert.match(pickerHtml, /data-testid="model-picker-recommend"/);
  assert.match(pickerHtml, /data-testid="model-picker-apply-recommend"/);
  assert.match(pickerHtml, /Tables, Diagrams &amp; ADK Math/);
  assert.match(pickerHtml, /Gemini 4 Argon → Claude Opus 5\.5/);
});
