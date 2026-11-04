import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { useSessionUi } from "../../app/BattleSessions";
import { useMasked, useSafeMode } from "../../app/SafeMode";
import { DocumentDrawer } from "../../components/DocumentDrawer";
import { HighlightMetricSelect, useHighlightMetric } from "../../components/ui/HighlightMetricSelect";
import { Panel } from "../../components/ui/Panel";
import { api } from "../../lib/api";
import type { HighlightMetric } from "../../lib/winnerHighlight";
import { summaryTextForMetric } from "../../lib/winnerHighlight";
import type { Citation, LeaderboardResponse, LevelDescriptor, PipelinePresetEntry } from "../../types/arena";
import { useIngestStatus } from "../ingest/ingestStatus";
import { modelLabel, type ModelSelection } from "../levels/modelSelection";
import { useBattleStream } from "../levels/useBattleStream";
import { useModelCatalog, type ModelCatalogState } from "../levels/useModelCatalog";
import { COMPARE_SESSION } from "../levels/useLevelRun";
import { ScopeSelector } from "../uploads/ScopeSelector";
import { applyUploadScopeToChoices } from "../uploads/uploadModel";
import { useUploadWorkspaces, type UploadWorkspacesState } from "../uploads/useUploadWorkspaces";
import {
  ABLATION_SESSION,
  ablationSlotChoices,
  buildAblationWire,
  defaultAblationSlots,
  type AblationChoice,
  type AblationSlot,
} from "./ablationModel";
import { AblationSetup } from "./AblationSetup";
import { CompareAnswers, type SelectedCitation } from "./CompareAnswers";
import { CompareRails } from "./CompareRails";
import { CompareSetup } from "./CompareSetup";
import type { QuestionValue } from "./CompareQuestionFields";
import { RetrievalDiffPanel } from "./RetrievalDiffPanel";
import { Scoreboard } from "./Scoreboard";
import { costEstimate, enginesToRun, levelChoices, scoreboardRows } from "./compareModel";
import { RunStatusPills } from "./RunStatusPills";

export { COMPARE_SESSION };

type CompareMode = "ladder" | "ablation";

const NO_QUESTION: QuestionValue = { query: "", questionId: "" };
const NO_LEVELS: readonly string[] = [];
const NO_PICKS: Readonly<Record<string, string>> = {};
const NO_AB_RUN: readonly AblationChoice[] = [];

const isQuestion = (v: unknown): v is QuestionValue =>
  typeof v === "object" && v !== null && typeof (v as QuestionValue).query === "string" && typeof (v as QuestionValue).questionId === "string";
const isPicks = (v: unknown): v is Readonly<Record<string, string>> =>
  typeof v === "object" && v !== null && !Array.isArray(v) && Object.values(v).every((x) => typeof x === "string");
const isMode = (v: unknown): v is CompareMode => v === "ladder" || v === "ablation";
const isSlots = (v: unknown): v is readonly AblationSlot[] =>
  Array.isArray(v) && v.length >= 2 && v.every((s) => typeof s === "object" && s !== null && "slotId" in s && "spec" in s);

function useLeaderboard(): { data: LeaderboardResponse | null; error: string | null } {
  const [data, setData] = useState<LeaderboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.leaderboard({ source: "recordings" }).then(
      (res) => !cancelled && setData(res),
      (err: unknown) => !cancelled && setError(err instanceof Error ? err.message : String(err)),
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return { data: useMasked(data), error };
}

interface SharedCompareProps {
  readonly levels: readonly LevelDescriptor[];
  readonly demoMode: string | null;
  readonly models: ModelCatalogState;
  readonly uploadScope: UploadWorkspacesState;
  readonly highlightMetric: HighlightMetric;
  readonly setHighlightMetric: (m: HighlightMetric) => void;
}

const PresetLadderView: React.FC<SharedCompareProps> = ({
  levels,
  demoMode,
  models,
  uploadScope,
  highlightMetric,
  setHighlightMetric,
}) => {
  const stream = useBattleStream(COMPARE_SESSION);
  const leaderboard = useLeaderboard();
  const { mask } = useSafeMode();
  const [question, setQuestion] = useSessionUi(COMPARE_SESSION, "question", NO_QUESTION, isQuestion);
  const [uncheckedList, setUnchecked] = useSessionUi(COMPARE_SESSION, "unchecked", NO_LEVELS);
  const unchecked = useMemo(() => new Set(uncheckedList), [uncheckedList]);
  const [runEngines, setRunEngines] = useSessionUi(COMPARE_SESSION, "runEngines", NO_LEVELS);
  const [railPicks, setRailPicks] = useSessionUi(COMPARE_SESSION, "railPicks", NO_PICKS, isPicks);
  const [selected, setSelected] = useState<SelectedCitation | null>(null);

  const effectiveQid = uploadScope.isUploadActive ? "" : question.questionId;
  const choices = useMemo(() => {
    const base = levelChoices(levels, models.catalog, models.selection, effectiveQid);
    return uploadScope.isUploadActive ? applyUploadScopeToChoices(base, uploadScope.activeWorkspace) : base;
  }, [levels, models.catalog, models.selection, effectiveQid, uploadScope.isUploadActive, uploadScope.activeWorkspace]);
  const checked = useMemo(() => new Set(choices.map((c) => c.levelId).filter((id) => !unchecked.has(id))), [choices, unchecked]);
  const engines = enginesToRun(choices, checked);
  const estimate = costEstimate(leaderboard.data, engines, models.selection);
  const labelFor = useCallback((id: string | null) => modelLabel(models.catalog, id), [models.catalog]);
  const levelLabel = useCallback((engine: string) => choices.find((c) => c.levelId === engine)?.short ?? engine, [choices]);

  const runLevels = useMemo(
    () => choices.filter((c) => runEngines.includes(c.levelId)).map((c) => ({ ...c, stages: levels.find((l) => l.level_id === c.levelId)?.stages ?? [] })),
    [choices, runEngines, levels],
  );
  const diffCols = useMemo(
    () => runLevels.map((l) => ({ engineId: l.levelId, short: l.short, label: l.label })),
    [runLevels],
  );
  const rows = scoreboardRows(runLevels, runEngines, stream.state);

  const onToggle = (levelId: string, next: boolean): void =>
    setUnchecked(next ? uncheckedList.filter((id) => id !== levelId) : [...uncheckedList.filter((id) => id !== levelId), levelId]);
  const onRun = (toRun: readonly string[], sel: ModelSelection | null): void => {
    setSelected(null);
    setRailPicks(NO_PICKS);
    setRunEngines(toRun);
    stream.run({
      query: question.query.trim(),
      questionId: uploadScope.isUploadActive ? null : question.questionId.trim() || null,
      engines: toRun,
      solicitationFilter: uploadScope.isUploadActive ? uploadScope.scope : null,
      synthesisModel: sel?.synthesis ?? null,
      judgeModel: sel?.judge ?? null,
    });
  };
  const openCitation = useCallback((engine: string, citation: Citation) => setSelected({ engine, citation }), []);
  const onReset = (): void => {
    setSelected(null);
    stream.reset();
  };
  const hasSetup = question.query !== "" || question.questionId !== "" || uncheckedList.length > 0;
  const started = runEngines.length > 0 && (stream.isStreaming || stream.state.events.length > 0 || stream.transportError !== null);

  return (
    <>
      {stream.state.complete && (
        <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-1 px-4 py-2 shadow-sm" role="status" data-testid="compare-summary">
          <span className="text-sm font-semibold text-ink-1">{mask(summaryTextForMetric(rows, highlightMetric))}</span>
          <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} />
        </div>
      )}
      <Panel title="Setup" subtitle="One question · one synthesis + judge pair · the enabled levels you tick">
        <CompareSetup
          question={question}
          onQuestion={setQuestion}
          models={models}
          choices={choices}
          checked={checked}
          onToggle={onToggle}
          engines={engines}
          estimate={estimate}
          leaderboardError={leaderboard.error ? mask(leaderboard.error) : null}
          labelFor={levelLabel}
          isStreaming={stream.isStreaming}
          onRun={onRun}
          onAbort={stream.abort}
          onReset={onReset}
          canReset={stream.hasContent || hasSetup || runEngines.length > 0}
          uploadScopeId={uploadScope.isUploadActive ? uploadScope.scope : null}
          scopeControl={
            <ScopeSelector
              id="compare-scope-select"
              demoMode={demoMode}
              scope={uploadScope.scope}
              workspaces={uploadScope.workspaces}
              loading={uploadScope.loading}
              error={uploadScope.error}
              disabled={stream.isStreaming}
              onChange={uploadScope.setScope}
            />
          }
        />
        {stream.transportError && (
          <div className="mt-3 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
            <span className="font-semibold">Run rejected / stream error:</span> <span className="break-words">{stream.transportError}</span>
          </div>
        )}
        {stream.state.warnings.map((w, i) => <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>)}
      </Panel>
      {started && (
        <>
          <Panel
            title="Scoreboard"
            subtitle={stream.state.runId ? `run ${stream.state.runId}` : "waiting for run_started"}
            actions={
              <div className="flex flex-wrap items-end gap-3">
                <HighlightMetricSelect value={highlightMetric} onChange={setHighlightMetric} id="compare-highlight-metric" />
                {!stream.state.complete && <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} />}
              </div>
            }
          >
            <Scoreboard rows={rows} labelFor={labelFor} highlightMetric={highlightMetric} />
          </Panel>
          <Panel title="Stage rails" subtitle="One rail per level — statuses come only from that engine's events">
            <div data-run-state={stream.state.complete ? "complete" : stream.state.interrupted ? "interrupted" : stream.isStreaming ? "streaming" : "idle"}>
              <CompareRails
                levels={runLevels}
                timelines={stream.state.timelines}
                rows={rows}
                picked={railPicks}
                onPick={(engine, stageId) => setRailPicks({ ...railPicks, [engine]: stageId })}
              />
            </div>
          </Panel>
          <Panel
            title="Retrieval Diff Inspector"
            subtitle="Exact rank shifts and chunk/page overlap relative to the chosen baseline"
          >
            <RetrievalDiffPanel
              columns={diffCols}
              timelines={stream.state.timelines}
              onSelectCitation={openCitation}
            />
          </Panel>
          <CompareAnswers
            levels={runLevels}
            timelines={stream.state.timelines}
            rows={rows}
            running={stream.isStreaming}
            selected={selected}
            onSelectCitation={openCitation}
            labelFor={labelFor}
          />
        </>
      )}
      <DocumentDrawer
        citation={selected?.citation ?? null}
        open={selected !== null}
        onClose={() => setSelected(null)}
        retrievedChunks={selected ? stream.state.timelines[selected.engine]?.answer?.retrievedChunks : undefined}
      />
    </>
  );
};

const AblationLabView: React.FC<SharedCompareProps & { readonly presets: readonly PipelinePresetEntry[] }> = ({
  levels,
  demoMode,
  models,
  uploadScope,
  highlightMetric,
  setHighlightMetric,
  presets,
}) => {
  const stream = useBattleStream(ABLATION_SESSION);
  const ingestState = useIngestStatus();
  const { mask } = useSafeMode();
  const [question, setQuestion] = useSessionUi(ABLATION_SESSION, "question", NO_QUESTION, isQuestion);
  const [slots, setSlots] = useSessionUi<readonly AblationSlot[]>(
    ABLATION_SESSION,
    "slots",
    defaultAblationSlots(),
    isSlots,
  );
  const [runChoices, setRunChoices] = useSessionUi<readonly AblationChoice[]>(ABLATION_SESSION, "runChoices", NO_AB_RUN);
  const [railPicks, setRailPicks] = useSessionUi(ABLATION_SESSION, "railPicks", NO_PICKS, isPicks);
  const [selected, setSelected] = useState<SelectedCitation | null>(null);

  const enabledLevels = useMemo(() => levels.filter((l) => l.enabled).map((l) => l.level_id), [levels]);
  const choices = useMemo(
    () =>
      ablationSlotChoices({
        slots,
        enabledLevels,
        catalog: models.catalog,
        sharedSelection: models.selection,
        questionId: uploadScope.isUploadActive ? "" : question.questionId,
        presets,
        isUploadActive: uploadScope.isUploadActive,
        activeWorkspace: uploadScope.activeWorkspace,
        ingestStatus: ingestState.status,
        ingestError: ingestState.error,
        isMock: stream.isMock,
      }),
    [
      enabledLevels,
      ingestState.error,
      ingestState.status,
      models.catalog,
      models.selection,
      presets,
      question.questionId,
      slots,
      stream.isMock,
      uploadScope.activeWorkspace,
      uploadScope.isUploadActive,
    ],
  );

  const labelFor = useCallback((id: string | null) => modelLabel(models.catalog, id), [models.catalog]);
  const activeRunChoices = runChoices.length > 0 ? runChoices : choices;
  const runIds = useMemo(() => activeRunChoices.map((c) => c.levelId), [activeRunChoices]);
  const rows = scoreboardRows(activeRunChoices, runIds, stream.state);
  const diffCols = useMemo(
    () => activeRunChoices.map((c) => ({ engineId: c.levelId, short: c.short, label: c.label })),
    [activeRunChoices],
  );

  const onRun = (sel: ModelSelection | null): void => {
    setSelected(null);
    setRailPicks(NO_PICKS);
    setRunChoices(choices);
    stream.run({
      query: question.query.trim(),
      questionId: uploadScope.isUploadActive ? null : question.questionId.trim() || null,
      engines: [],
      pipelines: buildAblationWire(slots, sel),
      solicitationFilter: uploadScope.isUploadActive ? uploadScope.scope : null,
      synthesisModel: sel?.synthesis ?? null,
      judgeModel: sel?.judge ?? null,
    });
  };
  const openCitation = useCallback((engine: string, citation: Citation) => setSelected({ engine, citation }), []);
  const onReset = (): void => {
    setSelected(null);
    setRunChoices(NO_AB_RUN);
    stream.reset();
  };
  const started = runChoices.length > 0 && (stream.isStreaming || stream.state.events.length > 0 || stream.transportError !== null);

  return (
    <>
      {stream.state.complete && (
        <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-1 px-4 py-2 shadow-sm" role="status" data-testid="compare-summary">
          <span className="text-sm font-semibold text-ink-1">{mask(summaryTextForMetric(rows, highlightMetric))}</span>
          <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} />
        </div>
      )}
      <Panel
        title="Controlled Ablation Setup"
        subtitle="Hold the question and Judge model fixed while varying 1+ pipeline slots across 2–4 concurrent recipes"
      >
        <AblationSetup
          question={question}
          onQuestion={setQuestion}
          models={models}
          slots={slots}
          onSlotsChange={setSlots}
          choices={choices}
          presets={presets}
          enabledLevels={enabledLevels}
          isStreaming={stream.isStreaming}
          onRun={onRun}
          onAbort={stream.abort}
          onReset={onReset}
          canReset={stream.hasContent || question.query !== "" || runChoices.length > 0}
          uploadScopeId={uploadScope.isUploadActive ? uploadScope.scope : null}
          scopeControl={
            <ScopeSelector
              id="ablation-scope-select"
              demoMode={demoMode}
              scope={uploadScope.scope}
              workspaces={uploadScope.workspaces}
              loading={uploadScope.loading}
              error={uploadScope.error}
              disabled={stream.isStreaming}
              onChange={uploadScope.setScope}
            />
          }
        />
        {stream.transportError && (
          <div className="mt-3 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
            <span className="font-semibold">Run rejected / stream error:</span> <span className="break-words">{stream.transportError}</span>
          </div>
        )}
        {stream.state.warnings.map((w, i) => <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>)}
      </Panel>
      {started && (
        <>
          <Panel
            title="Ablation Scoreboard"
            subtitle={stream.state.runId ? `run ${stream.state.runId}` : "waiting for run_started"}
            actions={
              <div className="flex flex-wrap items-end gap-3">
                <HighlightMetricSelect value={highlightMetric} onChange={setHighlightMetric} id="ablation-highlight-metric" />
                {!stream.state.complete && <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} />}
              </div>
            }
          >
            <Scoreboard rows={rows} labelFor={labelFor} highlightMetric={highlightMetric} />
          </Panel>
          <Panel title="Stage rails" subtitle="One rail per recipe slot — live backend events">
            <div data-run-state={stream.state.complete ? "complete" : stream.state.interrupted ? "interrupted" : stream.isStreaming ? "streaming" : "idle"}>
              <CompareRails
                levels={activeRunChoices}
                timelines={stream.state.timelines}
                rows={rows}
                picked={railPicks}
                onPick={(engine, stageId) => setRailPicks({ ...railPicks, [engine]: stageId })}
              />
            </div>
          </Panel>
          <Panel
            title="Retrieval Diff Inspector"
            subtitle="Exact chunk / page rank shifts caused by your slot changes (↑ promoted, ↓ demoted, NEW, DROPPED)"
          >
            <RetrievalDiffPanel
              columns={diffCols}
              timelines={stream.state.timelines}
              onSelectCitation={openCitation}
            />
          </Panel>
          <CompareAnswers
            levels={activeRunChoices}
            timelines={stream.state.timelines}
            rows={rows}
            running={stream.isStreaming}
            selected={selected}
            onSelectCitation={openCitation}
            labelFor={labelFor}
          />
        </>
      )}
      <DocumentDrawer
        citation={selected?.citation ?? null}
        open={selected !== null}
        onClose={() => setSelected(null)}
        retrievedChunks={selected ? stream.state.timelines[selected.engine]?.answer?.retrievedChunks : undefined}
      />
    </>
  );
};

/** `/compare`: supports both Preset Ladder (`Levels 1–6`) and Controlled Ablation Lab (`2–4` custom recipes). */
export const ComparePage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const { levels, loading, health } = useAppData();
  const models = useModelCatalog(null);
  const uploadScope = useUploadWorkspaces(health?.demo_mode ?? null);
  const [highlightMetric, setHighlightMetric] = useHighlightMetric();
  const [mode, setMode] = useSessionUi<CompareMode>(COMPARE_SESSION, "compareMode", "ladder", isMode);
  const [presets, setPresets] = useState<readonly PipelinePresetEntry[]>([]);

  const appliedModeRef = useRef<string | null>(null);
  useEffect(() => {
    const modeParam = searchParams.get("mode");
    if ((modeParam === "ablation" || modeParam === "ladder") && appliedModeRef.current !== modeParam) {
      appliedModeRef.current = modeParam;
      setMode(modeParam);
      const next = new URLSearchParams(searchParams);
      next.delete("mode");
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setMode, setSearchParams]);

  useEffect(() => {
    let cancelled = false;
    api.pipelinesPresets().then(
      (res) => {
        if (!cancelled && Array.isArray(res.presets)) setPresets(res.presets);
      },
      () => {
        // Offline / `?mock=1`: client-side `defaultPresetSpec` covers all 6 presets.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <p className="text-xs text-ink-3">Loading levels…</p>;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold text-ink-1">Compare</h1>
        <div
          className="inline-flex rounded-lg border border-line bg-surface-1 p-0.5"
          role="group"
          aria-label="Comparison mode"
          data-testid="compare-mode-toggle"
        >
          <button
            type="button"
            aria-pressed={mode === "ladder"}
            onClick={() => setMode("ladder")}
            data-testid="compare-mode-ladder"
            className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
              mode === "ladder" ? "bg-moss text-surface-0" : "text-ink-2 hover:text-ink-1"
            }`}
          >
            Preset Ladder (Levels 1–6)
          </button>
          <button
            type="button"
            aria-pressed={mode === "ablation"}
            onClick={() => setMode("ablation")}
            data-testid="compare-mode-ablation"
            className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
              mode === "ablation" ? "bg-moss text-surface-0" : "text-ink-2 hover:text-ink-1"
            }`}
          >
            Ablation Lab (2–4 Recipes)
          </button>
        </div>
      </div>

      {mode === "ladder" ? (
        <PresetLadderView
          levels={levels}
          demoMode={health?.demo_mode ?? null}
          models={models}
          uploadScope={uploadScope}
          highlightMetric={highlightMetric}
          setHighlightMetric={setHighlightMetric}
        />
      ) : (
        <AblationLabView
          levels={levels}
          demoMode={health?.demo_mode ?? null}
          models={models}
          uploadScope={uploadScope}
          highlightMetric={highlightMetric}
          setHighlightMetric={setHighlightMetric}
          presets={presets}
        />
      )}
    </div>
  );
};
