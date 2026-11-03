import React from "react";
import { Copy, Play, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";
import { Button } from "../../components/ui/Button";
import { Pill } from "../../components/ui/Pill";
import { validateQueryInput } from "../../lib/validation";
import type { ModelsResponse, PipelinePresetEntry } from "../../types/arena";
import { ModelPicker } from "../levels/ModelPicker";
import { modelLabel, providerLabel, validateSelection, type ModelSelection } from "../levels/modelSelection";
import type { ModelCatalogState } from "../levels/useModelCatalog";
import { isUploadScope } from "../uploads/uploadModel";
import { pipelineSummaryLabel, synthesisOptionsForSpec } from "../workbench/pipelineSpecModel";
import { SlotControls } from "../workbench/SlotControls";
import {
  ABLATION_TEMPLATES,
  MAX_ABLATION_SLOTS,
  MIN_ABLATION_SLOTS,
  changedAxesSummary,
  cloneAblationSlot,
  removeAblationSlot,
  updateAblationSlot,
  type AblationChoice,
  type AblationSlot,
} from "./ablationModel";
import { CompareQuestionFields, questionIdError, type QuestionValue } from "./CompareQuestionFields";

export interface AblationSetupProps {
  readonly question: QuestionValue;
  readonly onQuestion: (q: QuestionValue) => void;
  readonly models: ModelCatalogState;
  readonly slots: readonly AblationSlot[];
  readonly onSlotsChange: (next: readonly AblationSlot[]) => void;
  readonly choices: readonly AblationChoice[];
  readonly presets: readonly PipelinePresetEntry[];
  readonly enabledLevels: readonly string[];
  readonly isStreaming: boolean;
  readonly onRun: (sel: ModelSelection | null) => void;
  readonly onAbort: () => void;
  readonly onReset: () => void;
  readonly canReset: boolean;
  readonly scopeControl?: React.ReactNode;
  readonly uploadScopeId?: string | null;
}

const selectClass =
  "w-full rounded-md border border-line bg-surface-0 px-2 py-1 text-xs text-ink-1 disabled:cursor-not-allowed disabled:opacity-60";

interface SlotSynthesisSelectProps {
  readonly id: string;
  readonly slot: AblationSlot;
  readonly catalog: ModelsResponse | null;
  readonly sharedSelection: ModelSelection | null;
  readonly disabled: boolean;
  readonly onOverride: (modelId: string | null) => void;
}

const SlotSynthesisSelect: React.FC<SlotSynthesisSelectProps> = ({
  id,
  slot,
  catalog,
  sharedSelection,
  disabled,
  onOverride,
}) => {
  if (!catalog || !sharedSelection) return null;
  const sharedSynthLabel = modelLabel(catalog, sharedSelection.synthesis) ?? sharedSelection.synthesis;
  const options = synthesisOptionsForSpec(catalog, sharedSelection.judge, slot.spec);
  return (
    <div className="flex flex-col gap-1 sm:max-w-sm">
      <label htmlFor={id} className="text-xs font-medium text-ink-2">
        Slot synthesis model <span className="font-normal text-ink-3">(judge stays shared for fairness)</span>
      </label>
      <select
        id={id}
        value={slot.synthesisModelOverride ?? ""}
        disabled={disabled}
        onChange={(e) => onOverride(e.target.value === "" ? null : e.target.value)}
        className={selectClass}
      >
        <option value="">Inherit shared synthesis ({sharedSynthLabel})</option>
        {options.map((o) => (
          <option key={o.entry.id} value={o.entry.id} disabled={o.disabled}>
            {o.entry.label} · {providerLabel(o.entry.provider)}
            {o.unavailable ? " — unavailable" : o.restricted ? " — incompatible with recipe" : o.disabled ? " — used as judge" : ""}
          </option>
        ))}
      </select>
    </div>
  );
};

/**
 * Controlled 2–4 Recipe Ablation builder for `/compare`:
 * holds the question + judge model fixed while letting you vary 1+ retrieval, reranker,
 * context-order, reasoning, or synthesis-model slots across 2–4 concurrent recipes.
 */
export const AblationSetup: React.FC<AblationSetupProps> = (p) => {
  const { mask } = useSafeMode();
  const { catalog, selection } = p.models;
  const uploadMode = isUploadScope(p.uploadScopeId);
  const variedAxes = changedAxesSummary(p.slots);
  const pairError = catalog && selection ? validateSelection(catalog, selection, null) : null;

  const slotBlockers = p.choices
    .filter((c) => c.disabledReason !== null)
    .map((c) => `${c.short}: ${c.disabledReason}`);
  const blockers = [
    validateQueryInput(p.question.query, p.slots.map((s) => s.slotId)),
    uploadMode ? null : questionIdError(p.question.questionId),
    pairError,
    ...slotBlockers,
  ].filter((x): x is string => x !== null);
  const disabled = p.isStreaming || blockers.length > 0;

  return (
    <form
      className="flex flex-col gap-4"
      data-testid="ablation-setup"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled) p.onRun(catalog && selection ? selection : null);
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line bg-surface-1 px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs font-medium text-ink-2">Starter experiments:</span>
          {ABLATION_TEMPLATES.map((tpl) => (
            <button
              key={tpl.id}
              type="button"
              disabled={p.isStreaming}
              title={tpl.description}
              onClick={() => p.onSlotsChange(tpl.buildSlots(p.presets))}
              data-testid={`ablation-template-${tpl.id}`}
              className="rounded-full border border-line bg-surface-0 px-2.5 py-0.5 text-xs font-medium text-ink-2 hover:border-ink-3 hover:text-ink-1 disabled:opacity-50"
            >
              {tpl.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {variedAxes.length > 0 ? (
            <Pill tone="ok" mono data-testid="ablation-varied-axes">
              varying: {variedAxes.join(", ")}
            </Pill>
          ) : (
            <Pill tone="warn" mono data-testid="ablation-identical-warn">
              0 axes varied (identical recipes)
            </Pill>
          )}
          <Button
            size="sm"
            variant="secondary"
            disabled={p.isStreaming || p.slots.length >= MAX_ABLATION_SLOTS}
            onClick={() => p.onSlotsChange(cloneAblationSlot(p.slots, p.slots[p.slots.length - 1]?.slotId ?? "slot_1"))}
            leadingIcon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            data-testid="ablation-add-slot"
          >
            Add recipe ({p.slots.length}/{MAX_ABLATION_SLOTS})
          </Button>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <CompareQuestionFields
          value={p.question}
          onChange={p.onQuestion}
          disabled={p.isStreaming}
          scopeControl={p.scopeControl}
          uploadScopeId={p.uploadScopeId}
        />
        <div className="flex flex-col gap-3">
          <ModelPicker
            catalog={catalog}
            loading={p.models.loading}
            error={p.models.error}
            selection={selection}
            onChange={p.models.setSelection}
            disabled={p.isStreaming}
            validationError={pairError}
            replay={null}
            levelId={null}
          />
          <p className="text-xs text-ink-3">
            All {p.slots.length} recipes run concurrently against the same question and are scored by the same Judge model.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="submit"
              variant="primary"
              disabled={disabled}
              leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}
              data-testid="run-ablation-button"
            >
              {p.isStreaming ? "Running…" : `Run ${p.slots.length} ablation recipes`}
            </Button>
            {p.isStreaming && (
              <Button variant="danger" onClick={p.onAbort} leadingIcon={<Square className="h-3.5 w-3.5" aria-hidden />}>
                Stop
              </Button>
            )}
            <Button
              variant="ghost"
              onClick={p.onReset}
              disabled={!p.canReset}
              data-testid="reset-ablation"
              leadingIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
            >
              {p.isStreaming ? "Stop & reset" : "Reset"}
            </Button>
          </div>
          {!p.isStreaming && blockers.length > 0 && p.question.query.length > 0 && (
            <ul className="text-xs text-clay" aria-label="Why Run is disabled">
              {blockers.map((b) => (
                <li key={b}>{mask(b)}</li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {p.slots.map((slot, idx) => {
          const choice = p.choices[idx];
          const summary = pipelineSummaryLabel(slot.spec, p.presets);
          return (
            <div
              key={slot.slotId}
              className="flex flex-col gap-3 rounded-lg border border-line bg-surface-0 p-3"
              data-testid={`ablation-card-${slot.slotId}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-surface-2 px-2 py-0.5 font-mono text-xs font-semibold text-ink-1">
                    Slot {idx + 1}
                  </span>
                  <span className="text-xs font-medium text-ink-1">{summary}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={p.isStreaming || p.slots.length >= MAX_ABLATION_SLOTS}
                    onClick={() => p.onSlotsChange(cloneAblationSlot(p.slots, slot.slotId))}
                    leadingIcon={<Copy className="h-3 w-3" aria-hidden />}
                  >
                    Clone
                  </Button>
                  {p.slots.length > MIN_ABLATION_SLOTS && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={p.isStreaming}
                      onClick={() => p.onSlotsChange(removeAblationSlot(p.slots, slot.slotId))}
                      leadingIcon={<Trash2 className="h-3 w-3" aria-hidden />}
                    >
                      Remove
                    </Button>
                  )}
                </div>
              </div>

              <SlotSynthesisSelect
                id={`${slot.slotId}-synth-override`}
                slot={slot}
                catalog={catalog}
                sharedSelection={selection}
                disabled={p.isStreaming}
                onOverride={(modelId) =>
                  p.onSlotsChange(updateAblationSlot(p.slots, slot.slotId, { synthesisModelOverride: modelId }))
                }
              />

              <SlotControls
                idPrefix={slot.slotId}
                spec={slot.spec}
                presets={p.presets}
                enabledLevels={p.enabledLevels}
                disabled={p.isStreaming}
                onChange={(nextSpec) => p.onSlotsChange(updateAblationSlot(p.slots, slot.slotId, { spec: nextSpec }))}
              />

              {choice?.disabledReason && (
                <p className="text-xs text-clay" role="status">
                  Slot blocked: {mask(choice.disabledReason)}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </form>
  );
};
