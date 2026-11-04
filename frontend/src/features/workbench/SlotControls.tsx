import React from "react";
import { RotateCcw } from "lucide-react";
import { SHORT_LEVEL_LABEL } from "../../app/Shell";
import { Pill } from "../../components/ui/Pill";
import {
  LEVEL_IDS,
  type CanonicalAgentTool,
  type ContextOrder,
  type IndexProfile,
  type LevelId,
  type PipelinePresetEntry,
  type PipelineSpec,
  type QueryTransform,
  type ReasoningMode,
  type RerankerKind,
  type RetrievalMode,
} from "../../types/arena";
import {
  AGENT_TOOL_LABELS,
  BQ_RETRIEVAL_MODES,
  CANONICAL_AGENT_TOOLS,
  CONTEXT_ORDERS,
  CONTEXT_ORDER_LABELS,
  INDEX_PROFILES,
  MAX_CANDIDATE_K_BY_PROFILE,
  PROFILE_LABELS,
  QUERY_TRANSFORMS,
  QUERY_TRANSFORM_LABELS,
  REASONING_MODES,
  REASONING_MODE_LABELS,
  RERANKERS,
  RERANKER_LABELS,
  RETRIEVAL_MODES,
  RETRIEVAL_MODE_LABELS,
  TOP_K_RANGE,
  activeQueryStages,
  backingLevelForProfile,
  coerceSpec,
  defaultPresetSpec,
  isBqProfile,
  matchingPresetLevelId,
} from "./pipelineSpecModel";

export interface SlotControlsProps {
  readonly idPrefix: string;
  readonly spec: PipelineSpec;
  readonly presets?: readonly PipelinePresetEntry[];
  readonly enabledLevels?: readonly string[];
  readonly disabled?: boolean;
  readonly showLabelInput?: boolean;
  readonly onChange: (next: PipelineSpec) => void;
}

const selectClass =
  "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:cursor-not-allowed disabled:opacity-60";

interface PresetBarProps {
  readonly activePreset: LevelId | null;
  readonly backingLevel: LevelId;
  readonly presets?: readonly PipelinePresetEntry[];
  readonly enabledLevels?: readonly string[];
  readonly disabled: boolean;
  readonly onPickPreset: (levelId: LevelId) => void;
}

const PresetBar: React.FC<PresetBarProps> = ({
  activePreset,
  backingLevel,
  presets,
  enabledLevels,
  disabled,
  onPickPreset,
}) => (
  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2.5">
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Level presets">
      <span className="mr-1 text-xs font-medium text-ink-3">Presets:</span>
      {LEVEL_IDS.map((id) => {
        const active = activePreset === id;
        const locked = enabledLevels ? !enabledLevels.includes(id) : false;
        return (
          <button
            key={id}
            type="button"
            disabled={disabled || locked}
            onClick={() => onPickPreset(id)}
            data-testid={`preset-pill-${id}`}
            aria-pressed={active}
            title={locked ? `${SHORT_LEVEL_LABEL[id] ?? id} is locked` : `Load ${SHORT_LEVEL_LABEL[id] ?? id} preset`}
            className={`rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              active
                ? "border-moss bg-moss text-surface-0"
                : "border-line bg-surface-2 text-ink-2 hover:border-ink-3 hover:text-ink-1"
            }`}
          >
            {SHORT_LEVEL_LABEL[id] ?? id}
          </button>
        );
      })}
    </div>
    <div className="flex items-center gap-2">
      {activePreset ? (
        <Pill tone="ok" mono data-testid="recipe-preset-badge">
          preset · {SHORT_LEVEL_LABEL[activePreset] ?? activePreset}
        </Pill>
      ) : (
        <>
          <Pill tone="info" mono data-testid="recipe-custom-badge">
            custom recipe
          </Pill>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onPickPreset(backingLevel)}
            className="inline-flex items-center gap-1 text-xs text-moss underline disabled:opacity-50"
          >
            <RotateCcw className="h-3 w-3" aria-hidden />
            Reset to {SHORT_LEVEL_LABEL[backingLevel] ?? backingLevel}
          </button>
        </>
      )}
      {presets && presets.length > 0 && <span className="sr-only">{presets.length} server presets loaded</span>}
    </div>
  </div>
);

interface AgentToolChecklistProps {
  readonly idPrefix: string;
  readonly selected: readonly CanonicalAgentTool[];
  readonly disabled: boolean;
  readonly onToggle: (tool: CanonicalAgentTool, checked: boolean) => void;
}

const AgentToolChecklist: React.FC<AgentToolChecklistProps> = ({ idPrefix, selected, disabled, onToggle }) => (
  <fieldset className="rounded-md border border-line bg-surface-1 p-2.5" data-testid={`${idPrefix}-agent-tools`}>
    <legend className="px-1 text-xs font-medium text-ink-2">
      ADK Agent Tools <span className="font-normal text-ink-3">(Google ADK LlmAgent loop)</span>
    </legend>
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {CANONICAL_AGENT_TOOLS.map((tool) => {
        const locked = tool === "search_procurement_corpus";
        const checked = locked || selected.includes(tool);
        const inputId = `${idPrefix}-tool-${tool}`;
        return (
          <label
            key={tool}
            htmlFor={inputId}
            className={`flex items-start gap-2 rounded border border-line bg-surface-0 px-2 py-1.5 text-xs ${
              disabled || locked ? "opacity-80" : "cursor-pointer hover:border-ink-3"
            }`}
          >
            <input
              id={inputId}
              type="checkbox"
              checked={checked}
              disabled={disabled || locked}
              onChange={(e) => onToggle(tool, e.target.checked)}
              className="mt-0.5 rounded border-line"
            />
            <span className="font-mono text-[11px] leading-snug text-ink-1">{AGENT_TOOL_LABELS[tool]}</span>
          </label>
        );
      })}
    </div>
  </fieldset>
);

/**
 * Interactive Composable RAG slot builder: 1-click preset pills + validated component selects
 * for `IndexProfile` and all 8 `QuerySlots`. Every edit passes through `coerceSpec` so cross-slot
 * invariants stay valid and `ArchitectureDiagram` updates instantaneously.
 */
export const SlotControls: React.FC<SlotControlsProps> = ({
  idPrefix,
  spec,
  presets,
  enabledLevels,
  disabled = false,
  showLabelInput = true,
  onChange,
}) => {
  const activePreset = matchingPresetLevelId(spec, presets);
  const backingLevel = backingLevelForProfile(spec.index_profile);
  const bq = isBqProfile(spec.index_profile);
  const maxCand = MAX_CANDIDATE_K_BY_PROFILE[spec.index_profile];
  const maxTopK = Math.min(TOP_K_RANGE[1], maxCand);
  const stages = activeQueryStages(spec);

  const applyPatch = (next: PipelineSpec): void => onChange(coerceSpec(next));
  const pickPreset = (levelId: LevelId): void => {
    const base = defaultPresetSpec(levelId, presets);
    onChange(
      coerceSpec({
        ...base,
        ...(spec.synthesis_model ? { synthesis_model: spec.synthesis_model } : {}),
        ...(spec.judge_model ? { judge_model: spec.judge_model } : {}),
      }),
    );
  };

  const toggleTool = (tool: CanonicalAgentTool, checked: boolean): void => {
    const next = checked
      ? [...spec.slots.agent_tools, tool]
      : spec.slots.agent_tools.filter((t) => t !== tool);
    applyPatch({ ...spec, slots: { ...spec.slots, agent_tools: next } });
  };

  return (
    <div className="flex flex-col gap-3" data-testid={`${idPrefix}-slot-controls`}>
      <PresetBar
        activePreset={activePreset}
        backingLevel={backingLevel}
        presets={presets}
        enabledLevels={enabledLevels}
        disabled={disabled}
        onPickPreset={pickPreset}
      />

      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-index-profile`} className="text-xs font-medium text-ink-2">
            1. Index Profile <span className="font-normal text-ink-3">(corpus store)</span>
          </label>
          <select
            id={`${idPrefix}-index-profile`}
            value={spec.index_profile}
            disabled={disabled}
            onChange={(e) => applyPatch({ ...spec, index_profile: e.target.value as IndexProfile })}
            className={selectClass}
          >
            {INDEX_PROFILES.map((p) => {
              const backId = backingLevelForProfile(p);
              const locked = enabledLevels ? !enabledLevels.includes(backId) : false;
              return (
                <option key={p} value={p} disabled={locked}>
                  {PROFILE_LABELS[p]}{locked ? " — locked" : ""}
                </option>
              );
            })}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-query-transform`} className="text-xs font-medium text-ink-2">
            2. Query Transform
          </label>
          <select
            id={`${idPrefix}-query-transform`}
            value={spec.slots.query_transform}
            disabled={disabled}
            onChange={(e) =>
              applyPatch({ ...spec, slots: { ...spec.slots, query_transform: e.target.value as QueryTransform } })
            }
            className={selectClass}
          >
            {QUERY_TRANSFORMS.map((qt) => (
              <option key={qt} value={qt}>{QUERY_TRANSFORM_LABELS[qt]}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-retrieval-mode`} className="text-xs font-medium text-ink-2">
            3. Retrieval Mode
          </label>
          <select
            id={`${idPrefix}-retrieval-mode`}
            value={spec.slots.retrieval_mode}
            disabled={disabled || !bq}
            onChange={(e) =>
              applyPatch({ ...spec, slots: { ...spec.slots, retrieval_mode: e.target.value as RetrievalMode } })
            }
            className={selectClass}
          >
            {RETRIEVAL_MODES.map((rm) => {
              const modeValid = bq ? BQ_RETRIEVAL_MODES.includes(rm) : rm === "managed_retrieval";
              return (
                <option key={rm} value={rm} disabled={!modeValid}>
                  {RETRIEVAL_MODE_LABELS[rm]}
                </option>
              );
            })}
          </select>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={`${idPrefix}-candidate-k`} className="text-xs font-medium text-ink-2">
              4a. Candidate K <span className="font-normal text-ink-3">(≤{maxCand})</span>
            </label>
            <input
              id={`${idPrefix}-candidate-k`}
              type="number"
              min={spec.slots.top_k}
              max={maxCand}
              value={spec.slots.candidate_k}
              disabled={disabled}
              onChange={(e) =>
                applyPatch({ ...spec, slots: { ...spec.slots, candidate_k: Number(e.target.value) } })
              }
              className={`${selectClass} font-mono`}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={`${idPrefix}-top-k`} className="text-xs font-medium text-ink-2">
              4b. Final Top K <span className="font-normal text-ink-3">(≤{maxTopK})</span>
            </label>
            <input
              id={`${idPrefix}-top-k`}
              type="number"
              min={TOP_K_RANGE[0]}
              max={maxTopK}
              value={spec.slots.top_k}
              disabled={disabled}
              onChange={(e) =>
                applyPatch({ ...spec, slots: { ...spec.slots, top_k: Number(e.target.value) } })
              }
              className={`${selectClass} font-mono`}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-reranker`} className="text-xs font-medium text-ink-2">
            5. Cross-Encoder Reranker
          </label>
          <select
            id={`${idPrefix}-reranker`}
            value={spec.slots.reranker}
            disabled={disabled}
            onChange={(e) =>
              applyPatch({ ...spec, slots: { ...spec.slots, reranker: e.target.value as RerankerKind } })
            }
            className={selectClass}
          >
            {RERANKERS.map((rk) => (
              <option key={rk} value={rk}>{RERANKER_LABELS[rk]}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-context-order`} className="text-xs font-medium text-ink-2">
            6. Context Packing Order
          </label>
          <select
            id={`${idPrefix}-context-order`}
            value={spec.slots.context_order}
            disabled={disabled}
            onChange={(e) =>
              applyPatch({ ...spec, slots: { ...spec.slots, context_order: e.target.value as ContextOrder } })
            }
            className={selectClass}
          >
            {CONTEXT_ORDERS.map((co) => (
              <option key={co} value={co}>{CONTEXT_ORDER_LABELS[co]}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${idPrefix}-reasoning-mode`} className="text-xs font-medium text-ink-2">
            7. Reasoning Loop
          </label>
          <select
            id={`${idPrefix}-reasoning-mode`}
            value={spec.slots.reasoning_mode}
            disabled={disabled || !bq}
            title={!bq ? "ADK agent tools require a BigQuery index_profile" : undefined}
            onChange={(e) =>
              applyPatch({ ...spec, slots: { ...spec.slots, reasoning_mode: e.target.value as ReasoningMode } })
            }
            className={selectClass}
          >
            {REASONING_MODES.map((rm) => (
              <option key={rm} value={rm} disabled={rm === "adk_agent" && !bq}>
                {REASONING_MODE_LABELS[rm]}{rm === "adk_agent" && !bq ? " — BigQuery profiles only" : ""}
              </option>
            ))}
          </select>
        </div>

        {showLabelInput && (
          <div className="flex flex-col gap-1">
            <label htmlFor={`${idPrefix}-recipe-label`} className="text-xs font-medium text-ink-2">
              8. Recipe Label <span className="font-normal text-ink-3">(optional)</span>
            </label>
            <input
              id={`${idPrefix}-recipe-label`}
              type="text"
              maxLength={80}
              value={spec.label ?? ""}
              disabled={disabled}
              placeholder="e.g. RAG Engine + Discovery Rerank"
              onChange={(e) => applyPatch({ ...spec, label: e.target.value })}
              className={selectClass}
            />
          </div>
        )}
      </div>

      {spec.slots.reasoning_mode === "adk_agent" && (
        <AgentToolChecklist
          idPrefix={idPrefix}
          selected={spec.slots.agent_tools}
          disabled={disabled}
          onToggle={toggleTool}
        />
      )}

      <div className="flex flex-wrap items-center gap-2 text-[11px] text-ink-3">
        <span>Active query stages:</span>
        <span className="font-mono text-ink-2" data-testid={`${idPrefix}-active-stages`}>
          {stages.join(" → ")}
        </span>
        <span>·</span>
        <span>
          Backing store:{" "}
          <span className="font-mono text-ink-2">{SHORT_LEVEL_LABEL[backingLevel] ?? backingLevel}</span>
        </span>
      </div>
    </div>
  );
};
