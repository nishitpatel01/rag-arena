/**
 * Pure Controlled Ablation `/compare` model (Release 1.3.0 Step 4) — no React, no I/O.
 * Manages `2..4` `AblationSlot` recipes (`slot_1`..`slot_4`), per-slot synthesis model overrides
 * with one shared unbiased judge, scientific varying-axes detection, run-gate validation, and
 * wire serialization for `POST /api/v1/battle/stream` (`pipelines` array).
 */

import type {
  IngestStatusResponse,
  LevelId,
  ModelsResponse,
  PipelinePresetEntry,
  PipelineRecipeWire,
  PipelineSpec,
} from "../../types/arena";
import { runGateFor } from "../ingest/ingestStatus";
import { modelLabel, type ModelSelection } from "../levels/modelSelection";
import { uploadRunGateFor, type UploadWorkspace } from "../uploads/uploadModel";
import {
  activeQueryStages,
  backingLevelForProfile,
  coerceSpec,
  defaultPresetSpec,
  isSpecEnabled,
  pipelineSummaryLabel,
  toRecipeWire,
  validateModelsForSpec,
  validatePipelineSpec,
  workbenchReplayGate,
} from "../workbench/pipelineSpecModel";
import type { LevelChoice } from "./compareModel";

export const ABLATION_SESSION = "compare:ablation";
export const MIN_ABLATION_SLOTS = 2;
export const MAX_ABLATION_SLOTS = 4;

export interface AblationSlot {
  readonly slotId: string;
  readonly spec: PipelineSpec;
  /** `null` = inherit shared synthesis model; non-null = per-slot synthesis override. */
  readonly synthesisModelOverride: string | null;
}

export interface AblationTemplate {
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly buildSlots: (presets?: readonly PipelinePresetEntry[]) => readonly AblationSlot[];
}

export interface AblationChoice extends LevelChoice {
  readonly stages: readonly string[];
  readonly backingLevelId: LevelId;
}

export interface AblationChoicesInput {
  readonly slots: readonly AblationSlot[];
  readonly enabledLevels: readonly string[];
  readonly catalog: ModelsResponse | null;
  readonly sharedSelection: ModelSelection | null;
  readonly questionId: string;
  readonly presets?: readonly PipelinePresetEntry[];
  readonly isUploadActive: boolean;
  readonly activeWorkspace: UploadWorkspace | null;
  readonly ingestStatus: IngestStatusResponse | null;
  readonly ingestError: string | null;
  readonly isMock: boolean;
}

const SLOT_IDS: readonly string[] = ["slot_1", "slot_2", "slot_3", "slot_4"] as const;

function nextFreeSlotId(existing: readonly AblationSlot[]): string {
  const used = new Set(existing.map((s) => s.slotId));
  return SLOT_IDS.find((id) => !used.has(id)) ?? `slot_${existing.length + 1}`;
}

/** Default 2-slot controlled ablation: isolates Semantic Reranker + Authority Ordering on Hybrid RRF. */
export function defaultAblationSlots(presets?: readonly PipelinePresetEntry[]): readonly AblationSlot[] {
  const baseHybrid = defaultPresetSpec("level2_hybrid", presets);
  return [
    {
      slotId: "slot_1",
      synthesisModelOverride: null,
      spec: coerceSpec({
        ...baseHybrid,
        label: "Baseline · Hybrid RRF (No Rerank)",
        slots: { ...baseHybrid.slots, reranker: "none", context_order: "score_order" },
      }),
    },
    {
      slotId: "slot_2",
      synthesisModelOverride: null,
      spec: coerceSpec({
        ...baseHybrid,
        label: "Treatment · Hybrid RRF + Rerank + Authority",
        slots: { ...baseHybrid.slots, reranker: "vertex_ranking_api", context_order: "authority_amendment_first" },
      }),
    },
  ];
}

export const ABLATION_TEMPLATES: readonly AblationTemplate[] = [
  {
    id: "rerank_effect",
    label: "Reranker + Authority Effect (2 slots)",
    description: "Same Hybrid RRF candidates (k=50 → 5): raw rank order vs Discovery Semantic Reranker + authority header.",
    buildSlots: defaultAblationSlots,
  },
  {
    id: "rag_engine_rerank",
    label: "RAG Engine: Raw vs Reranked (2 slots)",
    description: "Managed RagCorpus (k=20 → 5): raw RetrieveContexts KNN vs Discovery Engine Semantic Reranker.",
    buildSlots: (presets) => {
      const rag = defaultPresetSpec("rag_engine", presets);
      return [
        {
          slotId: "slot_1",
          synthesisModelOverride: null,
          spec: coerceSpec({ ...rag, label: "RAG Engine · Raw KNN (k=5)", slots: { ...rag.slots, candidate_k: 20, top_k: 5, reranker: "none" } }),
        },
        {
          slotId: "slot_2",
          synthesisModelOverride: null,
          spec: coerceSpec({ ...rag, label: "RAG Engine + Discovery Rerank (k=5)", slots: { ...rag.slots, candidate_k: 20, top_k: 5, reranker: "vertex_ranking_api", context_order: "authority_amendment_first" } }),
        },
      ];
    },
  },
  {
    id: "bq_retrieval_modes",
    label: "Vector vs Lexical vs Hybrid RRF (3 slots)",
    description: "Holds chunks, top_k=5, and synthesis constant while varying BigQuery retrieval_mode across all 3 arms.",
    buildSlots: (presets) => {
      const base = defaultPresetSpec("level2_hybrid", presets);
      return [
        {
          slotId: "slot_1",
          synthesisModelOverride: null,
          spec: coerceSpec({ ...base, label: "Dense Vector (VECTOR_SEARCH)", slots: { ...base.slots, retrieval_mode: "vector_cosine", reranker: "none" } }),
        },
        {
          slotId: "slot_2",
          synthesisModelOverride: null,
          spec: coerceSpec({ ...base, label: "Lexical Keyword (SEARCH)", slots: { ...base.slots, retrieval_mode: "lexical_bm25", reranker: "none" } }),
        },
        {
          slotId: "slot_3",
          synthesisModelOverride: null,
          spec: coerceSpec({ ...base, label: "Hybrid RRF (Vector + Lexical)", slots: { ...base.slots, retrieval_mode: "hybrid_rrf", reranker: "none" } }),
        },
      ];
    },
  },
  {
    id: "single_vs_adk",
    label: "Single-Pass vs ADK Agent Loop (2 slots)",
    description: "Same multimodal index + Hybrid RRF + Reranker: single-pass synthesis vs 4-tool Google ADK agent loop.",
    buildSlots: (presets) => [
      {
        slotId: "slot_1",
        synthesisModelOverride: null,
        spec: coerceSpec({ ...defaultPresetSpec("level3_multimodal", presets), label: "Single-Pass Multimodal" }),
      },
      {
        slotId: "slot_2",
        synthesisModelOverride: null,
        spec: coerceSpec({ ...defaultPresetSpec("level4_agentic", presets), label: "ADK Agentic Loop (4 tools)" }),
      },
    ],
  },
];

/** Clones `sourceSlotId` into a new slot (capped at `MAX_ABLATION_SLOTS = 4`). */
export function cloneAblationSlot(slots: readonly AblationSlot[], sourceSlotId: string): readonly AblationSlot[] {
  if (slots.length >= MAX_ABLATION_SLOTS) return slots;
  const src = slots.find((s) => s.slotId === sourceSlotId) ?? slots[slots.length - 1];
  if (!src) return slots;
  const nextId = nextFreeSlotId(slots);
  const copySpec = coerceSpec({ ...src.spec, label: undefined });
  return [...slots, { slotId: nextId, spec: copySpec, synthesisModelOverride: src.synthesisModelOverride }];
}

/** Removes `slotId` when `slots.length > MIN_ABLATION_SLOTS` (`2`). */
export function removeAblationSlot(slots: readonly AblationSlot[], slotId: string): readonly AblationSlot[] {
  if (slots.length <= MIN_ABLATION_SLOTS) return slots;
  return slots.filter((s) => s.slotId !== slotId);
}

/** Applies a coerced spec and/or synthesis override update to `slotId`. */
export function updateAblationSlot(
  slots: readonly AblationSlot[],
  slotId: string,
  patch: { readonly spec?: PipelineSpec; readonly synthesisModelOverride?: string | null },
): readonly AblationSlot[] {
  return slots.map((s) => {
    if (s.slotId !== slotId) return s;
    return {
      slotId: s.slotId,
      spec: patch.spec !== undefined ? coerceSpec(patch.spec) : s.spec,
      synthesisModelOverride:
        patch.synthesisModelOverride !== undefined ? patch.synthesisModelOverride : s.synthesisModelOverride,
    };
  });
}

/** Effective `(synthesis, judge)` pair for `slot` (judge is always shared across all slots). */
export function effectiveSlotSelection(slot: AblationSlot, shared: ModelSelection): ModelSelection {
  return { synthesis: slot.synthesisModelOverride ?? shared.synthesis, judge: shared.judge };
}

/** Lists which exact experimental axes vary across the current `2..4` slots. */
export function changedAxesSummary(slots: readonly AblationSlot[]): readonly string[] {
  if (slots.length < 2) return [];
  const varied: string[] = [];
  const distinct = (fn: (s: AblationSlot) => string): boolean => new Set(slots.map(fn)).size > 1;
  if (distinct((s) => s.spec.index_profile)) varied.push("index_profile");
  if (distinct((s) => s.spec.slots.query_transform)) varied.push("query_transform");
  if (distinct((s) => s.spec.slots.retrieval_mode)) varied.push("retrieval_mode");
  if (distinct((s) => String(s.spec.slots.candidate_k))) varied.push("candidate_k");
  if (distinct((s) => String(s.spec.slots.top_k))) varied.push("top_k");
  if (distinct((s) => s.spec.slots.reranker)) varied.push("reranker");
  if (distinct((s) => s.spec.slots.context_order)) varied.push("context_order");
  if (distinct((s) => s.spec.slots.reasoning_mode)) varied.push("reasoning_mode");
  if (distinct((s) => s.spec.slots.agent_tools.join(","))) varied.push("agent_tools");
  if (distinct((s) => s.synthesisModelOverride ?? "__shared__")) varied.push("synthesis_model");
  return varied;
}

function slotDisabledReason(slot: AblationSlot, input: AblationChoicesInput): string | null {
  const specErr = validatePipelineSpec(slot.spec);
  if (specErr) return specErr;
  const backing = backingLevelForProfile(slot.spec.index_profile);
  if (input.enabledLevels.length > 0 && !isSpecEnabled(slot.spec, input.enabledLevels)) {
    return `backing corpus (${backing}) is locked on the server`;
  }
  if (!input.isMock) {
    const gate = input.isUploadActive
      ? uploadRunGateFor(backing, input.activeWorkspace)
      : runGateFor(backing, input.ingestStatus, input.ingestError);
    if (gate.blocked) return gate.note ?? `${backing} corpus is not ingested`;
  }
  if (!input.catalog || !input.sharedSelection) return "model catalog not loaded";
  const eff = effectiveSlotSelection(slot, input.sharedSelection);
  const modelErr = validateModelsForSpec(input.catalog, eff, slot.spec);
  if (modelErr) return modelErr;
  if (!input.isUploadActive && !input.isMock) {
    const rep = workbenchReplayGate(input.catalog, slot.spec, input.questionId, eff, input.presets);
    if (rep.blockedReason) return rep.blockedReason;
  }
  return null;
}

/** Converts `slots` into `AblationChoice[]` compatible with `Scoreboard`, `CompareRails`, and `CompareAnswers`. */
export function ablationSlotChoices(input: AblationChoicesInput): readonly AblationChoice[] {
  return input.slots.slice(0, MAX_ABLATION_SLOTS).map((slot, idx) => {
    const baseLabel = pipelineSummaryLabel(slot.spec, input.presets);
    const overrideSuffix = slot.synthesisModelOverride
      ? ` · ${modelLabel(input.catalog, slot.synthesisModelOverride) ?? slot.synthesisModelOverride}`
      : "";
    return {
      levelId: slot.slotId,
      short: `Slot ${idx + 1}`,
      label: `${baseLabel}${overrideSuffix}`,
      order: idx + 1,
      stages: activeQueryStages(slot.spec),
      backingLevelId: backingLevelForProfile(slot.spec.index_profile),
      disabledReason: slotDisabledReason(slot, input),
    };
  });
}

/** Serializes `slots` into the `pipelines` array for `POST /api/v1/battle/stream`. */
export function buildAblationWire(
  slots: readonly AblationSlot[],
  sharedSelection: ModelSelection | null,
): readonly PipelineRecipeWire[] {
  return slots.slice(0, MAX_ABLATION_SLOTS).map((slot) => {
    const eff = sharedSelection ? effectiveSlotSelection(slot, sharedSelection) : null;
    return toRecipeWire(slot.spec, slot.slotId, eff);
  });
}
