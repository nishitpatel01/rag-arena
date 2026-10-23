/**
 * Pure model-picker rules (no React, no storage access): which catalog entries can be chosen for
 * each role, whether a (synthesis, judge) pair is runnable, localStorage validation, and which pairs
 * have replay recordings. Every fact (availability, prices, recordings) comes from GET /api/v1/models.
 */

import type { ModelCatalogEntry, ModelRole, ModelsResponse, RecordedPair } from "../../types/arena";

export const MODEL_SELECTION_STORAGE_KEY = "rag-arena.models";
export const MUST_DIFFER_REASON = "must differ from synthesis model";
export const MUST_DIFFER_FROM_JUDGE_REASON = "must differ from judge model";
const MAX_STORED_CHARS = 1_000;

export interface ModelSelection {
  readonly synthesis: string;
  readonly judge: string;
}

export interface OptionState {
  readonly entry: ModelCatalogEntry;
  readonly disabled: boolean;
  /** Why the option cannot be chosen (server `unavailable_reason` or the judge-differs rule). */
  readonly reason: string | null;
  readonly unavailable: boolean;
  /** Disabled because the model is valid only on another level (reason says which). */
  readonly restricted?: boolean;
}

export const PROVIDER_LABELS: Readonly<Record<string, string>> = {
  vertex_gemini: "Vertex AI Gemini",
  anthropic_vertex: "Anthropic on Vertex AI",
  vertex_ai_search: "Vertex AI Search (managed)",
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

/** Synthesis-only pseudo model: the managed Vertex AI Search summary (server returns 400 off `managed_vertex`). */
export const MANAGED_SUMMARY_MODEL = "vertex-ai-search-summary";
export const MANAGED_LEVEL = "managed_vertex";

/** Level ids a catalog entry is valid on, or null when it is valid everywhere. */
export function allowedLevels(entry: ModelCatalogEntry): readonly string[] | null {
  if (Array.isArray(entry.allowed_levels) && entry.allowed_levels.length > 0) return entry.allowed_levels;
  if (Array.isArray(entry.levels) && entry.levels.length > 0) return entry.levels;
  if (entry.id === MANAGED_SUMMARY_MODEL || entry.provider === "vertex_ai_search") return [MANAGED_LEVEL];
  return null;
}

/** Why `entry` cannot be used on `levelId` (null when allowed, or when no level is given). */
export function levelRestriction(entry: ModelCatalogEntry, levelId: string | null | undefined): string | null {
  if (!levelId) return null;
  const allowed = allowedLevels(entry);
  if (allowed === null || allowed.includes(levelId)) return null;
  return allowed.includes(MANAGED_LEVEL) && allowed.length === 1
    ? "managed Vertex AI Search summary — valid only on Level 5 (Managed Vertex AI Search)"
    : `valid only on ${allowed.join(", ")}`;
}

/** True for a model that only one level can use (e.g. the managed summary pseudo model). */
export function isLevelRestricted(entry: ModelCatalogEntry | null): boolean {
  return entry !== null && allowedLevels(entry) !== null;
}

export function findModel(catalog: ModelsResponse | null, id: string | null | undefined): ModelCatalogEntry | null {
  if (!catalog || !id) return null;
  return catalog.models.find((m) => m.id === id) ?? null;
}

/** Catalog label for an id, or the raw id when the catalog does not know it (never invents a name). */
export function modelLabel(catalog: ModelsResponse | null, id: string | null | undefined): string | null {
  if (!id) return null;
  return findModel(catalog, id)?.label ?? id;
}

function unavailableReason(entry: ModelCatalogEntry): string {
  return entry.unavailable_reason?.trim() || "unavailable (no reason given by the server)";
}

/**
 * Options for one select. Models lacking the role are omitted; unavailable models stay listed but
 * disabled; the model chosen in the *other* select is disabled (judge ≠ synthesis, both ways).
 */
export function optionStates(catalog: ModelsResponse, role: ModelRole, otherSelected: string | null, levelId: string | null = null): OptionState[] {
  return catalog.models
    .filter((m) => m.roles.includes(role))
    .map((entry) => {
      const restricted = levelRestriction(entry, levelId);
      if (restricted) return { entry, disabled: true, reason: restricted, unavailable: false, restricted: true };
      if (!entry.available) return { entry, disabled: true, reason: unavailableReason(entry), unavailable: true };
      if (otherSelected !== null && entry.id === otherSelected) {
        const reason = role === "judge" ? MUST_DIFFER_REASON : MUST_DIFFER_FROM_JUDGE_REASON;
        return { entry, disabled: true, reason, unavailable: false };
      }
      return { entry, disabled: false, reason: null, unavailable: false };
    });
}

/** Null when the pair is runnable; otherwise a human-readable reason (shown next to the Run button). */
export function validateSelection(catalog: ModelsResponse, sel: ModelSelection, levelId: string | null = null): string | null {
  const synth = findModel(catalog, sel.synthesis);
  const judge = findModel(catalog, sel.judge);
  if (!synth) return `Synthesis model "${sel.synthesis}" is not in the catalog.`;
  if (!judge) return `Judge model "${sel.judge}" is not in the catalog.`;
  if (!synth.roles.includes("synthesis")) return `${synth.label} cannot be used for synthesis.`;
  if (!judge.roles.includes("judge")) return `${judge.label} cannot be used as the judge.`;
  const synthRestricted = levelRestriction(synth, levelId);
  if (synthRestricted) return `${synth.label} cannot run on this level: ${synthRestricted}.`;
  const judgeRestricted = levelRestriction(judge, levelId);
  if (judgeRestricted) return `${judge.label} cannot judge on this level: ${judgeRestricted}.`;
  if (!synth.available) return `${synth.label} is unavailable: ${unavailableReason(synth)}`;
  if (!judge.available) return `${judge.label} is unavailable: ${unavailableReason(judge)}`;
  if (synth.id === judge.id) return `Judge model ${MUST_DIFFER_REASON}.`;
  return null;
}

export function defaultSelection(catalog: ModelsResponse): ModelSelection {
  return { synthesis: catalog.defaults.synthesis_model, judge: catalog.defaults.judge_model };
}

/** Parse the stored JSON; anything malformed, oversized or not runnable against the catalog → defaults. */
export function loadSelection(catalog: ModelsResponse, raw: string | null, levelId: string | null = null): ModelSelection {
  const fallback = defaultSelection(catalog);
  if (!raw || raw.length > MAX_STORED_CHARS) return fallback;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return fallback;
  }
  if (typeof parsed !== "object" || parsed === null) return fallback;
  const { synthesis, judge } = parsed as { synthesis?: unknown; judge?: unknown };
  if (typeof synthesis !== "string" || typeof judge !== "string") return fallback;
  const candidate: ModelSelection = { synthesis, judge };
  return validateSelection(catalog, candidate, levelId) === null ? candidate : fallback;
}

export function serializeSelection(sel: ModelSelection): string {
  return JSON.stringify({ synthesis: sel.synthesis, judge: sel.judge });
}

/** Recorded pairs that can replay `questionId` for `engine` (empty when the question id is empty). */
export function recordedPairsFor(catalog: ModelsResponse, engine: string, questionId: string): RecordedPair[] {
  const q = questionId.trim();
  if (!q) return [];
  return (catalog.recorded_pairs ?? []).filter((p) => p.engine === engine && p.question_ids.includes(q));
}

export interface ReplayGate {
  /** True when the backend runs in replay mode (only recorded pairs can play). */
  readonly replay: boolean;
  readonly pairs: readonly RecordedPair[];
  /** Reason the Run button is disabled, or null. */
  readonly blockedReason: string | null;
}

export function replayGate(catalog: ModelsResponse, engine: string, questionId: string, sel: ModelSelection): ReplayGate {
  if (catalog.demo_mode !== "replay") return { replay: false, pairs: [], blockedReason: null };
  const pairs = recordedPairsFor(catalog, engine, questionId);
  if (!questionId.trim()) {
    return { replay: true, pairs, blockedReason: "Replay mode plays recordings only: choose a golden question (question_id)." };
  }
  const hasPair = pairs.some((p) => p.synthesis_model === sel.synthesis && p.judge_model === sel.judge);
  if (hasPair) return { replay: true, pairs, blockedReason: null };
  const synth = modelLabel(catalog, sel.synthesis);
  const judge = modelLabel(catalog, sel.judge);
  return {
    replay: true,
    pairs,
    blockedReason: `No recording of "${questionId.trim()}" with synthesis ${synth} · judge ${judge}.`,
  };
}

/** "$X / $Y per 1M in/out · as of <date>" from server prices; null-safe (shows "price unknown"). */
export function formatModelPrice(entry: ModelCatalogEntry): string {
  const usd = (v: number | null): string => (v === null || !Number.isFinite(v) ? "?" : `$${Number(v.toFixed(4))}`);
  // Request-priced managed models (Vertex AI Search summary): price per 1,000 requests, not per token.
  if (typeof entry.usd_per_1k_requests === "number" && Number.isFinite(entry.usd_per_1k_requests) && entry.usd_per_1k_requests > 0) {
    const asOfReq = entry.price_as_of ? ` · as of ${entry.price_as_of}` : "";
    return `${usd(entry.usd_per_1k_requests)} per 1,000 requests${asOfReq}`;
  }
  if (entry.input_usd_per_1m === null && entry.output_usd_per_1m === null) return "price unknown";
  const asOf = entry.price_as_of ? ` · as of ${entry.price_as_of}` : "";
  return `${usd(entry.input_usd_per_1m)} / ${usd(entry.output_usd_per_1m)} per 1M in/out${asOf}`;
}

/** Only http(s) price links are rendered as anchors. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}
