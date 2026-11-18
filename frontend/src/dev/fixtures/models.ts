/**
 * DEV-ONLY model catalog fixture for `?mock=1`. Imported exclusively behind `if (import.meta.env.DEV)`
 * so it tree-shakes out of `npm run build`. Prices are deliberately null ("price unknown"): this file
 * must never be a source of real prices — those come only from GET /api/v1/models. The Shell shows the
 * "DEV FIXTURE — not real data" banner whenever `?mock=1` is set.
 */

import type { ModelsResponse } from "../../types/arena";

const NO_PRICE = { input_usd_per_1m: null, output_usd_per_1m: null, price_source: null, price_as_of: null } as const;

export const DEV_MODELS_FIXTURE: ModelsResponse = {
  models: [
    { id: "gemini-4-hydrogen", label: "Gemini 4 Hydrogen (dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
    { id: "gemini-4-carbon", label: "Gemini 4 Carbon (dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
    { id: "gemini-4-argon", label: "Gemini 4 Argon (dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
    { id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
    { id: "claude-opus-5-5", label: "Claude Opus 5.5 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
    { id: "gemini-3.8-flash", label: "Gemini 3.8 Flash (Legacy · Recorded, dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: true },
    { id: "claude-fable-5-2", label: "Claude Fable 5.2 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: false, unavailable_reason: "DEV FIXTURE: simulated data-sharing gate (not a real probe result)", verified_at: null, legacy: false },
    { id: "claude-fable-5-1", label: "Claude Fable 5.1 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: false, unavailable_reason: "DEV FIXTURE: simulated quota unavailability (not a real probe result)", verified_at: null, legacy: false },
    { id: "vertex-ai-search-summary", label: "Vertex AI Search summary (dev fixture)", provider: "vertex_ai_search", location: "global", roles: ["synthesis"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, allowed_levels: ["managed_vertex"], legacy: false },
    { id: "vertex-rag-engine-grounded", label: "Vertex AI RAG Engine Grounded (dev fixture)", provider: "vertex_rag_engine", location: "us-central1", roles: ["synthesis"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, allowed_levels: ["rag_engine"], legacy: false },
    { id: "vertex-genai-eval", label: "Vertex AI GenAI Eval Service (dev fixture)", provider: "vertex_genai_eval", location: "us-central1", roles: ["judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null, legacy: false },
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "replay",
  recorded_pairs: [
    { engine: "level1_naive", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", question_ids: ["q_exact_clin_lookup"] },
    { engine: "level1_naive", synthesis_model: "gemini-3.8-flash", judge_model: "claude-opus-5-5", question_ids: ["q_exact_clin_lookup"] },
  ],
  recommended_pairs: [
    {
      profile_id: "cost_latency",
      profile_label: "High-QPS / Low-Cost",
      synthesis_model: "gemini-4-hydrogen",
      judge_model: "claude-sonnet-4-6",
      alt_judge_model: "vertex-genai-eval",
      rationale: "Fast, low-cost Gemini 4 worker for high-QPS workloads.",
      judge_rationale: "Independent cross-family Anthropic judge.",
      cross_family: true,
      adk_compatible: true,
    },
    {
      profile_id: "balanced_enterprise",
      profile_label: "Enterprise SQL / Contracts",
      synthesis_model: "gemini-4-carbon",
      judge_model: "claude-opus-5-5",
      alt_judge_model: "claude-sonnet-4-6",
      rationale: "Balanced Gemini 4 Carbon worker for SQL and contract RAG.",
      judge_rationale: "Flagship cross-family Anthropic judge.",
      cross_family: true,
      adk_compatible: true,
    },
    {
      profile_id: "multimodal_agentic",
      profile_label: "Tables, Diagrams & ADK Math",
      synthesis_model: "gemini-4-argon",
      judge_model: "claude-opus-5-5",
      alt_judge_model: "claude-sonnet-4-6",
      rationale: "Flagship multimodal and Google ADK agentic worker.",
      judge_rationale: "Flagship cross-family judge matching golden recordings.",
      cross_family: true,
      adk_compatible: true,
    },
    {
      profile_id: "anthropic_worker",
      profile_label: "Claude Bake-Off (Levels 1–7)",
      synthesis_model: "claude-sonnet-4-6",
      judge_model: "gemini-4-argon",
      alt_judge_model: "gemini-4-carbon",
      rationale: "Anthropic synthesis bake-off on single-pass Levels 1–7.",
      judge_rationale: "Independent Google Gemini cross-family judge.",
      cross_family: true,
      adk_compatible: false,
    },
  ],
};
