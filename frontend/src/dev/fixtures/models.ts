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
    { id: "gemini-4-argon", label: "Gemini 4 Argon (dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null },
    { id: "gemini-3.8-flash", label: "Gemini 3.8 Flash (dev fixture)", provider: "vertex_gemini", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null },
    { id: "claude-opus-5-5", label: "Claude Opus 5.5 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: true, unavailable_reason: null, verified_at: null },
    { id: "claude-fable-5-1", label: "Claude Fable 5.1 (dev fixture)", provider: "anthropic_vertex", location: "global", roles: ["synthesis", "judge"], ...NO_PRICE, available: false, unavailable_reason: "DEV FIXTURE: simulated unavailability (not a real probe result)", verified_at: null },
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "replay",
  recorded_pairs: [
    { engine: "level1_naive", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", question_ids: ["q_exact_clin_lookup"] },
    { engine: "level1_naive", synthesis_model: "gemini-3.8-flash", judge_model: "claude-opus-5-5", question_ids: ["q_exact_clin_lookup"] },
  ],
};
