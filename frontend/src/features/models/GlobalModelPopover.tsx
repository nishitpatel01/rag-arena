import React, { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Cpu, Sparkles } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";
import { Pill } from "../../components/ui/Pill";
import type { ModelsResponse } from "../../types/arena";
import {
  findModel,
  formatModelPrice,
  optionStates,
  providerLabel,
  restrictedOptionSuffix,
  type ModelSelection,
} from "../levels/modelSelection";
import { useModelCatalog, type ModelCatalogState } from "../levels/useModelCatalog";
import {
  isCrossFamilyPair,
  recommendedPairsForCatalog,
  shortModelLabel,
} from "./modelRecommender";

export interface GlobalModelPopoverProps {
  /** Optional injected catalog state (tests / SSR); defaults to `useModelCatalog(null)`. */
  readonly modelState?: ModelCatalogState;
  /** Optional initial open state (tests). */
  readonly defaultOpen?: boolean;
}

const selectClass =
  "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:opacity-60";

const GlobalModelPopoverInner: React.FC<{
  readonly state: ModelCatalogState;
  readonly defaultOpen?: boolean;
}> = ({ state, defaultOpen = false }) => {
  const { catalog, selection, setSelection, loading, error } = state;
  const { mask } = useSafeMode();
  const [open, setOpen] = useState(defaultOpen);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open || typeof document === "undefined") return;
    const onPointerDown = (evt: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(evt.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (evt: KeyboardEvent): void => {
      if (evt.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const recommendations = useMemo(
    () => (catalog?.recommended_pairs && catalog.recommended_pairs.length > 0
      ? catalog.recommended_pairs
      : recommendedPairsForCatalog(catalog)),
    [catalog]
  );

  const workerId = selection?.synthesis ?? catalog?.defaults.synthesis_model ?? "gemini-4-argon";
  const judgeId = selection?.judge ?? catalog?.defaults.judge_model ?? "claude-opus-5-5";
  const workerShort = shortModelLabel(catalog, workerId);
  const judgeShort = shortModelLabel(catalog, judgeId);
  const crossFamily = isCrossFamilyPair(workerId, judgeId);

  const synthOptions = useMemo(
    () => (catalog ? optionStates(catalog, "synthesis", judgeId, null) : []),
    [catalog, judgeId]
  );
  const judgeOptions = useMemo(
    () => (catalog ? optionStates(catalog, "judge", workerId, null) : []),
    [catalog, workerId]
  );

  const activeWorkerEntry = findModel(catalog, workerId);
  const activeJudgeEntry = findModel(catalog, judgeId);

  const applyPair = (next: ModelSelection): void => {
    if (next.synthesis === next.judge) return;
    setSelection(next);
  };

  return (
    <div ref={rootRef} className="relative" data-testid="global-model-control">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls="global-model-popover"
        data-testid="global-model-trigger"
        title="App-wide Worker & Judge models (synced across Overview, Levels, Compare, and Workbench)"
        className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
          open
            ? "border-moss bg-winner text-ink-1"
            : "border-line bg-surface-0 text-ink-2 hover:border-ink-3 hover:text-ink-1"
        }`}
      >
        <Cpu className="h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />
        <span className="text-ink-3">Models:</span>
        <span className="font-mono text-ink-1">
          {loading && !catalog ? "loading…" : `${workerShort} · ${judgeShort}`}
        </span>
        <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>

      {open && (
        <div
          id="global-model-popover"
          role="dialog"
          aria-label="App-wide Worker and Judge model selector"
          data-testid="global-model-popover"
          className="absolute right-0 top-full z-50 mt-2 w-[27rem] max-w-[calc(100vw-2rem)] rounded-lg border border-line bg-surface-1 p-3.5 shadow-lg"
        >
          <div className="flex items-start justify-between gap-2 border-b border-line pb-2.5">
            <div className="min-w-0">
              <h2 className="text-xs font-semibold text-ink-1">App-Wide Worker &amp; Judge Models</h2>
              <p className="text-xs text-ink-3">
                Synced across Overview, Levels 1–8, Compare, and Workbench.
              </p>
            </div>
            <Pill tone={crossFamily ? "ok" : "warn"}>
              {crossFamily ? "Cross-family" : "Same family"}
            </Pill>
          </div>

          {error && !catalog ? (
            <p className="mt-2.5 text-xs text-clay [overflow-wrap:anywhere]" role="alert">
              Model catalog unavailable: {mask(error)}
            </p>
          ) : !catalog ? (
            <p className="mt-2.5 text-xs text-ink-3">Loading verified model catalog…</p>
          ) : (
            <div className="mt-3 flex flex-col gap-3">
              {/* 4 Quick-Pick Recommender Presets */}
              <div className="flex flex-col gap-1.5">
                <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-2">
                  <Sparkles className="h-3.5 w-3.5 text-ochre" aria-hidden />
                  Model Pair Recommender presets
                </span>
                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2" role="group" aria-label="Recommended model pairs">
                  {recommendations.map((rec) => {
                    const isActive =
                      workerId === rec.synthesis_model && judgeId === rec.judge_model;
                    return (
                      <button
                        key={rec.profile_id}
                        type="button"
                        aria-pressed={isActive}
                        data-testid={`global-model-preset-${rec.profile_id}`}
                        onClick={() =>
                          applyPair({ synthesis: rec.synthesis_model, judge: rec.judge_model })
                        }
                        className={`flex flex-col items-start gap-0.5 rounded-md border px-2.5 py-2 text-left transition-colors ${
                          isActive
                            ? "border-moss bg-winner text-ink-1"
                            : "border-line bg-surface-0 text-ink-2 hover:border-ink-3 hover:text-ink-1"
                        }`}
                      >
                        <div className="flex w-full items-center justify-between gap-1">
                          <span className="text-xs font-semibold text-ink-1">{rec.profile_label}</span>
                          {isActive && <Check className="h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />}
                        </div>
                        <span className="font-mono text-xs text-moss">
                          {shortModelLabel(catalog, rec.synthesis_model)} → {shortModelLabel(catalog, rec.judge_model)}
                        </span>
                        <span className="line-clamp-2 text-xs text-ink-3">{rec.rationale}</span>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Direct Worker & Judge Selects */}
              <div className="grid grid-cols-1 gap-2 border-t border-line pt-2.5 sm:grid-cols-2">
                <div className="flex min-w-0 flex-col gap-1">
                  <label htmlFor="global-worker-model" className="text-xs font-medium text-ink-2">
                    Worker (Synthesis)
                  </label>
                  <select
                    id="global-worker-model"
                    data-testid="global-worker-select"
                    value={workerId}
                    onChange={(e) => applyPair({ synthesis: e.target.value, judge: judgeId })}
                    className={selectClass}
                  >
                    {synthOptions.map((o) => {
                      const suffix = o.unavailable
                        ? " — unavailable"
                        : o.restricted
                          ? restrictedOptionSuffix(o.entry)
                          : o.disabled
                            ? " — used as judge"
                            : "";
                      return (
                        <option
                          key={o.entry.id}
                          value={o.entry.id}
                          disabled={o.disabled}
                          title={o.reason ? mask(o.reason) : undefined}
                        >
                          {o.entry.label} · {providerLabel(o.entry.provider)}
                          {suffix}
                        </option>
                      );
                    })}
                  </select>
                </div>

                <div className="flex min-w-0 flex-col gap-1">
                  <label htmlFor="global-judge-model" className="text-xs font-medium text-ink-2">
                    Judge (Evaluator)
                  </label>
                  <select
                    id="global-judge-model"
                    data-testid="global-judge-select"
                    value={judgeId}
                    onChange={(e) => applyPair({ synthesis: workerId, judge: e.target.value })}
                    className={selectClass}
                  >
                    {judgeOptions.map((o) => {
                      const suffix = o.unavailable
                        ? " — unavailable"
                        : o.disabled
                          ? " — used as worker"
                          : "";
                      return (
                        <option
                          key={o.entry.id}
                          value={o.entry.id}
                          disabled={o.disabled}
                          title={o.reason ? mask(o.reason) : undefined}
                        >
                          {o.entry.label} · {providerLabel(o.entry.provider)}
                          {suffix}
                        </option>
                      );
                    })}
                  </select>
                </div>
              </div>

              {/* Active Pair Pricing */}
              {(activeWorkerEntry || activeJudgeEntry) && (
                <div className="flex flex-col gap-0.5 rounded-md border border-line bg-surface-0 px-2.5 py-2 text-xs text-ink-3">
                  {activeWorkerEntry && (
                    <p>
                      <span className="font-medium text-ink-2">Worker:</span>{" "}
                      <span className="font-mono">{formatModelPrice(activeWorkerEntry)}</span>
                    </p>
                  )}
                  {activeJudgeEntry && (
                    <p>
                      <span className="font-medium text-ink-2">Judge:</span>{" "}
                      <span className="font-mono">{formatModelPrice(activeJudgeEntry)}</span>
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const GlobalModelPopover: React.FC<GlobalModelPopoverProps> = ({ modelState, defaultOpen = false }) => {
  if (modelState) {
    return <GlobalModelPopoverInner state={modelState} defaultOpen={defaultOpen} />;
  }
  return <GlobalModelPopoverConnected defaultOpen={defaultOpen} />;
};

const GlobalModelPopoverConnected: React.FC<{ readonly defaultOpen?: boolean }> = ({ defaultOpen }) => {
  const state = useModelCatalog(null);
  return <GlobalModelPopoverInner state={state} defaultOpen={defaultOpen} />;
};

/** Helper for SSR/unit tests constructing a minimal `ModelCatalogState`. */
export function staticModelCatalogState(
  catalog: ModelsResponse,
  selection: ModelSelection,
  setSelection: (sel: ModelSelection) => void = () => {}
): ModelCatalogState {
  return {
    catalog,
    error: null,
    loading: false,
    isFixture: false,
    selection,
    setSelection,
  };
}
