import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Play, RotateCcw, Square } from "lucide-react";
import type {
  ArchitectureAclCapability,
  BenchmarkQuestion,
  PipelinePresetEntry,
  PipelineSpec,
  RecordedAclDemo,
} from "../../types/arena";
import { useAppData } from "../../app/AppDataProvider";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { QUESTION_ID_ERROR, QUESTION_ID_PATTERN, validateQueryInput } from "../../lib/validation";
import { Button } from "../../components/ui/Button";
import { Pill } from "../../components/ui/Pill";
import { aclReplayBlockedReason, isPersonaRestricted } from "../acl/aclModel";
import type { RunGate } from "../ingest/ingestStatus";
import {
  GOLDEN_BADGE_LABEL,
  INDUSTRY_PACKS,
  LIVE_TEMPLATE_BADGE_LABEL,
  REPLAY_LIVE_TEMPLATE_REASON,
  filterPackItems,
  findMatchingLiveTemplate,
  resolvePackSelection,
  type IndustryPackId,
} from "../packs/industryPacks";
import { validateModelsForSpec, workbenchReplayGate } from "../workbench/pipelineSpecModel";
import { ModelPicker } from "./ModelPicker";
import { isMockRequested } from "./battleTransport";
import { replayGate, validateSelection, type ModelSelection } from "./modelSelection";
import type { ModelCatalogState } from "./useModelCatalog";
import { useQuestionFields, type QuestionOverride } from "./useQuestionFields";
import { isUploadScope } from "../uploads/uploadModel";

export interface QuestionPickerProps {
  readonly levelId: string;
  /** Page session holding the question fields, so they survive navigation. */
  readonly sessionKey: string;
  readonly gate: RunGate;
  readonly isStreaming: boolean;
  /** `models` is null when the catalog is unavailable (pre-picker backend): server defaults apply. */
  readonly onRun: (query: string, questionId: string | null, models: ModelSelection | null) => void;
  readonly onAbort: () => void;
  /** Stops any run and clears the page's results, selections and question fields. */
  readonly onReset: () => void;
  /** True when the page's results / selections hold something besides the question fields. */
  readonly hasResults: boolean;
  readonly models: ModelCatalogState;
  /** Question of a viewed Compare run: shown in the form until the user edits it. */
  readonly viewing?: QuestionOverride | null;
  /** "clear" while a Compare run is shown: the button only hides it on this page. */
  readonly resetMode?: "reset" | "clear";
  readonly scopeControl?: React.ReactNode;
  readonly aclControl?: React.ReactNode;
  readonly accessPersona?: string | null;
  readonly aclDemos?: readonly RecordedAclDemo[];
  readonly aclCapabilities?: Readonly<Record<string, ArchitectureAclCapability>>;
  readonly uploadScopeId?: string | null;
  /** Optional Composable `PipelineSpec` (Workbench): switches validation & replay gate to recipe rules. */
  readonly spec?: PipelineSpec | null;
  readonly presets?: readonly PipelinePresetEntry[];
  readonly runLabel?: string;
  readonly demoMode?: string | null;
}

const fieldClass = "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 placeholder:text-ink-3";

function useOptionalAppDemoMode(): string | null {
  try {
    return useAppData().health?.demo_mode ?? null;
  } catch {
    return null;
  }
}

function safeIsMockRequested(): boolean {
  try {
    return isMockRequested();
  } catch {
    return false;
  }
}

/**
 * Golden-question select + Industry Benchmark Pack filter + editable question + explicit `question_id`
 * (the recording key in replay mode, the reference-answer key for the judge). Run is gated by validation and the ingest gate.
 */
export const QuestionPicker: React.FC<QuestionPickerProps> = ({
  levelId,
  sessionKey,
  gate,
  isStreaming,
  onRun,
  onAbort,
  onReset,
  hasResults,
  models,
  viewing = null,
  resetMode = "reset",
  scopeControl = null,
  aclControl = null,
  accessPersona = null,
  aclDemos = [],
  aclCapabilities,
  uploadScopeId = null,
  spec = null,
  presets,
  runLabel = "Run level",
  demoMode: explicitDemoMode,
}) => {
  const [questions, setQuestions] = useState<readonly BenchmarkQuestion[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [packId, setPackId] = useState<IndustryPackId>("all");
  const [replayTemplateNotice, setReplayTemplateNotice] = useState<string | null>(null);
  const { mask } = useSafeMode();
  const appDemoMode = useOptionalAppDemoMode();
  const goldenIds = useMemo(() => questions.map((q) => q.question_id), [questions]);
  const { selectedId, setSelectedId, questionId, setQuestionId, query, setQuery } = useQuestionFields(sessionKey, viewing, goldenIds);
  const uploadMode = isUploadScope(uploadScopeId);
  const canReset = hasResults || selectedId !== "" || questionId !== "" || query !== "";

  const effectiveDemoMode = explicitDemoMode ?? models.catalog?.demo_mode ?? appDemoMode;
  const mockRequested = safeIsMockRequested();
  const isReplayMode = effectiveDemoMode === "replay" && !mockRequested;

  useEffect(() => {
    let cancelled = false;
    api.benchmarks().then(
      (res) => !cancelled && setQuestions(res.questions),
      (err: unknown) => !cancelled && setLoadError(err instanceof Error ? err.message : String(err))
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => filterPackItems(packId, questions), [packId, questions]);
  const activeGolden = useMemo(
    () => questions.find((q) => q.question_id === selectedId) ?? null,
    [questions, selectedId],
  );
  const activeTemplate = useMemo(
    () => (activeGolden ? null : findMatchingLiveTemplate(query)),
    [activeGolden, query],
  );

  const selectValue = activeGolden
    ? activeGolden.question_id
    : activeTemplate
      ? `tpl:${activeTemplate.id}`
      : "";

  const visibleRecorded = useMemo(() => {
    if (!activeGolden) return filtered.recordedQuestions;
    if (filtered.recordedQuestions.some((q) => q.question_id === activeGolden.question_id)) {
      return filtered.recordedQuestions;
    }
    return [activeGolden, ...filtered.recordedQuestions];
  }, [activeGolden, filtered.recordedQuestions]);

  const onSelectPack = (nextPack: IndustryPackId): void => {
    setPackId(nextPack);
    if (nextPack === "legal_cuad" && isReplayMode) {
      setReplayTemplateNotice(REPLAY_LIVE_TEMPLATE_REASON);
    } else {
      setReplayTemplateNotice(null);
    }
  };

  const applySelectionValue = (rawVal: string): void => {
    if (!rawVal) {
      setSelectedId("");
      setReplayTemplateNotice(null);
      return;
    }
    const resolved = resolvePackSelection(rawVal, questions, isReplayMode ? "replay" : (effectiveDemoMode ?? "live"));
    if (!resolved) return;
    if (resolved.blockedInReplay) {
      setReplayTemplateNotice(resolved.reason);
      return;
    }
    setReplayTemplateNotice(null);
    if (resolved.kind === "recorded") {
      setSelectedId(resolved.questionId);
      setQuery(resolved.query);
      setQuestionId(resolved.questionId);
    } else {
      setSelectedId("");
      setQuestionId("");
      setQuery(resolved.query);
    }
  };

  const onPick = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    applySelectionValue(e.target.value);
  };

  const onType = (e: React.ChangeEvent<HTMLTextAreaElement>): void => {
    setReplayTemplateNotice(null);
    setQuery(e.target.value);
    const match = questions.find((x) => x.question === e.target.value);
    setSelectedId(match ? match.question_id : "");
    if (!match && questionId === selectedId) setQuestionId("");
  };

  const validation = validateQueryInput(query, [levelId]);
  const idError = uploadMode || QUESTION_ID_PATTERN.test(questionId) ? null : QUESTION_ID_ERROR;
  const { catalog, selection } = models;
  const modelError =
    catalog && selection
      ? spec
        ? validateModelsForSpec(catalog, selection, spec)
        : validateSelection(catalog, selection, levelId)
      : null;
  // DEV `?mock=1` plays a local fixture (never the backend), so the replay-recording gate does not apply.
  const baseReplay =
    !uploadMode && catalog && selection && !mockRequested
      ? spec
        ? workbenchReplayGate(catalog, spec, questionId, selection, presets)
        : replayGate(catalog, levelId, questionId, selection)
      : null;
  const aclBlocked =
    !uploadMode && !mockRequested && isPersonaRestricted(accessPersona)
      ? aclReplayBlockedReason({
          demoMode: isReplayMode ? "replay" : effectiveDemoMode,
          levelId,
          personaId: accessPersona,
          questionId,
          synthesisModel: selection?.synthesis ?? null,
          judgeModel: selection?.judge ?? null,
          demos: aclDemos,
          capabilities: aclCapabilities,
        })
      : null;
  const replay =
    baseReplay && isPersonaRestricted(accessPersona) && isReplayMode
      ? { ...baseReplay, blockedReason: aclBlocked }
      : baseReplay;
  const disabled =
    isStreaming ||
    validation !== null ||
    idError !== null ||
    gate.blocked ||
    modelError !== null ||
    Boolean(replay?.blockedReason) ||
    Boolean(!isReplayMode && aclBlocked);
  const runModels = catalog && selection ? selection : null;

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled) onRun(query.trim(), uploadMode ? null : questionId.trim() || null, runModels);
      }}
    >
      {scopeControl}
      {aclControl}
      {!uploadMode && (
        <div className="flex flex-col gap-1.5" data-print-hide="true">
          <div className="flex flex-wrap items-center justify-between gap-1">
            <span className="text-xs font-medium text-ink-2">Industry pack</span>
            {activeGolden && <Pill tone="ok">{GOLDEN_BADGE_LABEL}</Pill>}
            {activeTemplate && (
              <Pill tone="info">
                {LIVE_TEMPLATE_BADGE_LABEL} · {activeTemplate.vertical}
              </Pill>
            )}
          </div>
          <div
            role="group"
            aria-label="Industry benchmark pack"
            data-testid="industry-pack-bar"
            className="flex flex-wrap gap-1"
          >
            {INDUSTRY_PACKS.map((pack) => {
              const pressed = pack.id === packId;
              return (
                <button
                  key={pack.id}
                  type="button"
                  disabled={isStreaming}
                  aria-pressed={pressed}
                  title={`${pack.label}: ${pack.description}`}
                  onClick={() => onSelectPack(pack.id)}
                  data-testid={`industry-pack-${pack.id}`}
                  className={`rounded-md border px-2 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    pressed
                      ? "border-moss bg-surface-2 text-ink-1"
                      : "border-line bg-surface-1 text-ink-2 hover:text-ink-1"
                  }`}
                >
                  {pack.shortLabel}
                </button>
              );
            })}
          </div>
          {packId !== "all" && (
            <p className="text-xs text-ink-3">{filtered.pack.description}</p>
          )}
        </div>
      )}
      {!uploadMode && (
        <div className="flex flex-col gap-1">
          <label htmlFor="golden-question" className="text-xs font-medium text-ink-2">Golden question</label>
          <select id="golden-question" value={selectValue} onChange={onPick} disabled={isStreaming} className={fieldClass}>
            <option value="">— choose a benchmark question —</option>
            {visibleRecorded.length > 0 && (
              <optgroup label={GOLDEN_BADGE_LABEL}>
                {visibleRecorded.map((q) => (
                  <option key={q.question_id} value={q.question_id}>
                    {q.question_id} · {q.failure_mode_category}
                  </option>
                ))}
              </optgroup>
            )}
            {filtered.liveTemplates.length > 0 && (
              <optgroup
                label={
                  isReplayMode
                    ? `${LIVE_TEMPLATE_BADGE_LABEL} (requires DEMO_MODE=live)`
                    : `${LIVE_TEMPLATE_BADGE_LABEL} (CUAD / FSI / Healthcare)`
                }
              >
                {filtered.liveTemplates.map((tpl) => (
                  <option
                    key={tpl.id}
                    value={`tpl:${tpl.id}`}
                    disabled={isReplayMode}
                  >
                    [{LIVE_TEMPLATE_BADGE_LABEL}] {tpl.vertical} · {tpl.title}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          {packId === "legal_cuad" && filtered.liveTemplates.length > 0 && (
            <div className="mt-1 flex flex-col gap-1 rounded-md border border-line bg-surface-1 p-2">
              <div className="flex flex-wrap items-center justify-between gap-1">
                <span className="text-xs font-medium text-ink-2">CUAD / FSI / Healthcare corpus templates</span>
                <Pill tone={isReplayMode ? "warn" : "info"}>{LIVE_TEMPLATE_BADGE_LABEL}</Pill>
              </div>
              <div className="flex flex-wrap gap-1">
                {filtered.liveTemplates.map((tpl) => (
                  <button
                    key={tpl.id}
                    type="button"
                    disabled={isStreaming || isReplayMode}
                    title={isReplayMode ? REPLAY_LIVE_TEMPLATE_REASON : `Populate prompt from ${tpl.sourceDoc}`}
                    onClick={() => applySelectionValue(`tpl:${tpl.id}`)}
                    data-testid={`live-template-chip-${tpl.id}`}
                    className="rounded border border-line bg-surface-0 px-2 py-0.5 text-left text-xs text-ink-1 transition-colors hover:border-ink-3 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {tpl.vertical}: {tpl.title.split("(")[0].trim()}
                  </button>
                ))}
              </div>
            </div>
          )}
          {replayTemplateNotice && (
            <p className="text-xs text-clay" role="status" data-testid="replay-live-template-notice">
              {replayTemplateNotice}
            </p>
          )}
          {activeTemplate && (
            <p className="text-xs text-ink-3">
              Corpus doc: <span className="font-mono">{activeTemplate.sourceDoc}</span>
            </p>
          )}
          {loadError && <p className="text-xs text-err" role="alert">Benchmarks unavailable: {mask(loadError)}</p>}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <label htmlFor="custom-question" className="text-xs font-medium text-ink-2">
          {uploadMode ? "Question for your uploaded PDFs" : "Question (editable)"}
        </label>
        <textarea
          id="custom-question"
          value={query}
          onChange={onType}
          rows={5}
          placeholder={uploadMode ? "Ask a question grounded in your uploaded documents…" : "Ask about CLIN rates, SLA matrices, amendments…"}
          className={`${fieldClass} resize-y`}
        />
        <div className="flex items-center justify-end text-xs text-ink-3">
          <span>{query.length} chars</span>
        </div>
      </div>
      {!uploadMode && (
        <div className="flex flex-col gap-1">
          <label htmlFor="question-id" className="text-xs font-medium text-ink-2">
            question_id <span className="font-normal text-ink-3">(recording key in replay; empty = ad-hoc)</span>
          </label>
          <input id="question-id" value={questionId} onChange={(e) => setQuestionId(e.target.value)} placeholder="e.g. q_exact_clin_lookup" spellCheck={false} className={`${fieldClass} font-mono`} />
          {idError && <p className="text-xs text-clay" role="alert">{idError}</p>}
        </div>
      )}
      <ModelPicker
        catalog={catalog}
        loading={models.loading}
        error={models.error}
        selection={selection}
        onChange={models.setSelection}
        disabled={isStreaming}
        validationError={modelError}
        replay={replay}
        levelId={levelId}
        spec={spec}
      />
      {validation && query.length > 0 && <p className="text-xs text-clay">{validation}</p>}
      {gate.blocked && gate.count === 0 && (
        <p className="text-xs text-clay" role="alert">
          {uploadMode ? "This upload workspace has not been ingested for this level yet." : "No chunks ingested for this level's tier."}{" "}
          <Link to="/ingest" className="font-medium underline">Run ingestion</Link> first.
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" disabled={disabled} leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}>
          {isStreaming ? "Running…" : runLabel}
        </Button>
        {isStreaming && (
          <Button variant="danger" onClick={onAbort} leadingIcon={<Square className="h-3.5 w-3.5" aria-hidden />}>Stop</Button>
        )}
        <Button variant="ghost" onClick={onReset} disabled={!canReset} data-testid="reset-page"
          title={resetMode === "clear" ? "Hide the Compare run on this page (Compare keeps it)" : "Clear this page's results, selections and question (models are kept)"}
          leadingIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}>
          {resetMode === "clear" ? "Clear" : isStreaming ? "Stop & reset" : "Reset"}
        </Button>
      </div>
    </form>
  );
};
