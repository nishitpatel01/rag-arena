import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { useSessionUi } from "../../app/BattleSessions";
import { useLevelIngest, type LevelIngestView } from "../../app/IngestSessions";
import { useSafeMode } from "../../app/SafeMode";
import { SHORT_LEVEL_LABEL } from "../../app/Shell";
import { DocumentDrawer } from "../../components/DocumentDrawer";
import { DocumentViewer } from "../../components/DocumentViewer";
import { Panel } from "../../components/ui/Panel";
import { PaneResizer, useElementWidth, usePaneWidth } from "../../components/ui/PaneResizer";
import { Pill } from "../../components/ui/Pill";
import { api } from "../../lib/api";
import { DOC_PANE, MIN_MIDDLE_PX, QUESTION_PANE, paneMax, sideMaxima } from "../../lib/paneWidth";
import {
  LEVEL_IDS,
  type Citation,
  type LevelId,
  type PipelinePresetEntry,
  type PipelineSpec,
} from "../../types/arena";
import { ingestPillText, runGateFor, useIngestStatus, type IngestStatusState, type RunGate } from "../ingest/ingestStatus";
import { parseAgentStagePayload } from "../levels/agentTimelineModel";
import { AnswerPanel, MetricsPanel } from "../levels/AnswerPanels";
import { ArchitectureDiagram } from "../levels/ArchitectureDiagram";
import { LevelIngestBanner } from "../levels/LevelIngestBanner";
import { activeStageId } from "../levels/model";
import { modelLabel, type ModelSelection } from "../levels/modelSelection";
import { QuestionPicker } from "../levels/QuestionPicker";
import { RunPills, runStateAttr } from "../levels/RunPills";
import { StageDetail, type AnswerSource } from "../levels/StageDetail";
import { StageRail } from "../levels/StageRail";
import { useBattleStream } from "../levels/useBattleStream";
import { useModelCatalog } from "../levels/useModelCatalog";
import { ScopeSelector } from "../uploads/ScopeSelector";
import { uploadRunGateFor } from "../uploads/uploadModel";
import { useUploadWorkspaces, type UploadWorkspacesState } from "../uploads/useUploadWorkspaces";
import {
  WORKBENCH_SESSION,
  WORKBENCH_SLOT_ID,
  activeQueryStages,
  backingLevelForProfile,
  buildPipelineDescriptor,
  coerceSelectionForSpec,
  coerceSpec,
  defaultPresetSpec,
  isSpecEnabled,
  matchingPresetLevelId,
  pipelineSummaryLabel,
  toRecipeWire,
} from "./pipelineSpecModel";
import { SlotControls } from "./SlotControls";

const GRID_GAPS_PX = 32;

const isStageId = (v: unknown): v is string | null => v === null || typeof v === "string";
const isCitation = (v: unknown): v is Citation | null => v === null || (typeof v === "object" && !Array.isArray(v));
const isSpecShape = (v: unknown): v is PipelineSpec =>
  typeof v === "object" && v !== null && "index_profile" in v && "slots" in v;

function mergeStages(declared: readonly string[], seen: readonly string[]): string[] {
  const out = [...declared];
  for (const s of seen) if (!out.includes(s)) out.push(s);
  return out;
}

function parsePresetParam(raw: string | null): LevelId | null {
  if (!raw) return null;
  return (LEVEL_IDS as readonly string[]).includes(raw) ? (raw as LevelId) : null;
}

function buildWorkbenchGate(
  spec: PipelineSpec,
  enabledLevels: readonly string[],
  isMock: boolean,
  uploadScope: UploadWorkspacesState,
  ingestState: IngestStatusState,
  levelIngest: LevelIngestView | null,
): RunGate {
  if (isMock) return { blocked: false, note: null, tone: "neutral", count: null };
  if (enabledLevels.length > 0 && !isSpecEnabled(spec, enabledLevels)) {
    const backing = backingLevelForProfile(spec.index_profile);
    return {
      blocked: true,
      note: `Backing corpus or ADK tier (${SHORT_LEVEL_LABEL[backing] ?? backing}) is locked on the server.`,
      tone: "warn",
      count: null,
    };
  }
  const backing = backingLevelForProfile(spec.index_profile);
  const raw = uploadScope.isUploadActive
    ? uploadScope.loading
      ? { blocked: true, note: "Checking upload workspace…", tone: "neutral" as const, count: null }
      : uploadRunGateFor(backing, uploadScope.activeWorkspace)
    : runGateFor(backing, ingestState.status, ingestState.error);
  if (raw.blocked && levelIngest?.entry.isStreaming) {
    return {
      ...raw,
      count: null,
      tone: "info",
      note: `Ingestion in progress (${levelIngest.entry.jobId ?? levelIngest.backingLevelId}) — Run unlocks automatically on job_complete`,
    };
  }
  return raw;
}

const BackingCorpusPill: React.FC<{
  readonly backingLevelId: LevelId;
  readonly ingestState: IngestStatusState;
  readonly uploadScope: UploadWorkspacesState;
  readonly levelIngest: LevelIngestView | null;
}> = ({ backingLevelId, ingestState, uploadScope, levelIngest }) => {
  if (levelIngest?.entry.isStreaming) {
    return (
      <Link to="/ingest" className="inline-flex max-w-[36rem]" aria-label="Open ingestion page">
        <Pill tone="active" mono>{SHORT_LEVEL_LABEL[backingLevelId]} · ingesting…</Pill>
      </Link>
    );
  }
  if (uploadScope.isUploadActive) {
    const gate = uploadRunGateFor(backingLevelId, uploadScope.activeWorkspace);
    const ws = uploadScope.activeWorkspace;
    const unit = backingLevelId === "managed_vertex" ? "docs" : backingLevelId === "rag_engine" ? "files" : "chunks";
    const text = ws ? (gate.blocked ? `${ws.id} · not ingested` : `${ws.id} · ${gate.count ?? 0} ${unit}`) : "upload workspace";
    return (
      <Link to="/ingest" className="inline-flex max-w-[36rem]" aria-label="Open ingestion page">
        <Pill tone={gate.tone} mono>{text}</Pill>
      </Link>
    );
  }
  const gate = runGateFor(backingLevelId, ingestState.status, ingestState.error);
  if (!ingestState.status && !ingestState.error) return <Pill tone="neutral">ingest status…</Pill>;
  return (
    <Link to="/ingest" className="inline-flex max-w-[36rem]" aria-label="Open ingestion page" title={gate.note ?? undefined}>
      <Pill tone={gate.tone} mono={gate.count !== null}>{ingestPillText(backingLevelId, gate, ingestState.status)}</Pill>
    </Link>
  );
};

export const WorkbenchPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const { levels, health } = useAppData();
  const { maskValue } = useSafeMode();
  const [presets, setPresets] = useState<readonly PipelinePresetEntry[]>([]);
  const [rawSpec, setRawSpec] = useSessionUi<PipelineSpec>(
    WORKBENCH_SESSION,
    "spec",
    defaultPresetSpec("level2_hybrid"),
    isSpecShape,
  );
  const spec = useMemo(() => coerceSpec(rawSpec), [rawSpec]);
  const setSpec = useCallback((next: PipelineSpec) => setRawSpec(coerceSpec(next)), [setRawSpec]);

  // Honor `?preset=<levelId>` arrival links (e.g. "Customize in Workbench →" on LevelPage).
  const appliedParamRef = useRef<string | null>(null);
  useEffect(() => {
    const presetParam = parsePresetParam(searchParams.get("preset"));
    if (presetParam && appliedParamRef.current !== presetParam) {
      appliedParamRef.current = presetParam;
      setSpec(defaultPresetSpec(presetParam, presets));
      const next = new URLSearchParams(searchParams);
      next.delete("preset");
      setSearchParams(next, { replace: true });
    }
  }, [presets, searchParams, setSearchParams, setSpec]);

  useEffect(() => {
    let cancelled = false;
    api.pipelinesPresets().then(
      (res) => {
        if (!cancelled && Array.isArray(res.presets)) setPresets(res.presets);
      },
      () => {
        // Offline / `?mock=1`: `defaultPresetSpec` + `levels` provide complete client-side descriptors.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const enabledLevels = useMemo(() => levels.filter((l) => l.enabled).map((l) => l.level_id), [levels]);
  const backingLevelId = backingLevelForProfile(spec.index_profile);
  const backingDescriptor = levels.find((l) => l.level_id === backingLevelId) ?? levels[0];
  const ingestState = useIngestStatus();
  const uploadScope = useUploadWorkspaces(health?.demo_mode ?? null);
  const levelIngest = useLevelIngest(backingLevelId);
  const stream = useBattleStream(WORKBENCH_SESSION);
  const models = useModelCatalog(null);

  // Auto-coerce model selection when changing recipe slots invalidates a restricted pseudo-model.
  useEffect(() => {
    if (!models.catalog || !models.selection) return;
    const coerced = coerceSelectionForSpec(models.catalog, models.selection, spec);
    if (coerced.synthesis !== models.selection.synthesis || coerced.judge !== models.selection.judge) {
      models.setSelection(coerced);
    }
  }, [models, spec]);

  const [selectedStage, setSelectedStage] = useSessionUi<string | null>(WORKBENCH_SESSION, "selectedStage", null, isStageId);
  const [userPicked, setUserPicked] = useSessionUi<boolean>(WORKBENCH_SESSION, "userPicked", false);
  const [storedCitation, setStoredCitation] = useSessionUi<Citation | null>(WORKBENCH_SESSION, "citation", null, isCitation);
  const citation = useMemo(() => maskValue(storedCitation), [maskValue, storedCitation]);
  const [viewerOpen, setViewerOpen] = useState(false);

  const timeline = stream.state.timelines[WORKBENCH_SLOT_ID];
  const active = activeStageId(timeline);
  useEffect(() => {
    if (!userPicked && active) setSelectedStage(active);
  }, [active, setSelectedStage, userPicked]);

  const pickStage = useCallback((id: string) => {
    setUserPicked(true);
    setSelectedStage(id);
  }, [setSelectedStage, setUserPicked]);

  const openCitation = useCallback((c: Citation) => {
    setStoredCitation(c);
    setViewerOpen(true);
  }, [setStoredCitation]);

  const gate = buildWorkbenchGate(spec, enabledLevels, stream.isMock, uploadScope, ingestState, levelIngest);
  const descriptor = useMemo(() => buildPipelineDescriptor(spec, presets, levels), [levels, presets, spec]);
  const declaredStages = useMemo(() => activeQueryStages(spec), [spec]);
  const stageIds = useMemo(
    () => (timeline?.stageOrder.length ? mergeStages(declaredStages, timeline.stageOrder) : declaredStages),
    [declaredStages, timeline?.stageOrder],
  );
  const isAgentic = spec.slots.reasoning_mode === "adk_agent" || Boolean(timeline?.stages.agent_turn);
  const answerSource = useMemo<AnswerSource | null>(() => {
    if (!isAgentic) return null;
    const p = parseAgentStagePayload(timeline?.stages.agent_turn?.payload ?? null);
    const flag = timeline?.answer?.flags?.adk_runner_used;
    return { adkRunnerUsed: typeof flag === "boolean" ? flag : null, finalAnswerSource: p?.finalAnswerSource ?? null };
  }, [isAgentic, timeline?.answer?.flags, timeline?.stages.agent_turn?.payload]);

  const runSynthLabel = modelLabel(models.catalog, stream.state.synthesisModel);
  const runJudgeLabel = modelLabel(models.catalog, stream.state.judgeModel);
  const runServiceByStage = useMemo(
    () => ({ synthesize: runSynthLabel, evaluate: runJudgeLabel }),
    [runJudgeLabel, runSynthLabel],
  );
  const labelFor = useCallback((id: string | null) => modelLabel(models.catalog, id), [models.catalog]);

  const onRun = (query: string, questionId: string | null, runModels: ModelSelection | null): void => {
    setViewerOpen(false);
    setSelectedStage(null);
    setUserPicked(false);
    setStoredCitation(null);
    stream.run({
      query,
      questionId: uploadScope.isUploadActive ? null : questionId,
      engines: [],
      pipelines: [toRecipeWire(spec, WORKBENCH_SLOT_ID, runModels)],
      solicitationFilter: uploadScope.isUploadActive ? uploadScope.scope : null,
      synthesisModel: runModels?.synthesis ?? null,
      judgeModel: runModels?.judge ?? null,
    });
  };

  const onReset = (): void => {
    setViewerOpen(false);
    stream.reset();
  };

  const [questionPref, setQuestionWidth] = usePaneWidth("question", QUESTION_PANE);
  const [docPref, setDocWidth] = usePaneWidth("document-viewer", DOC_PANE);
  const [gridRef, gridWidth] = useElementWidth<HTMLDivElement>();
  const room = gridWidth === null ? null : gridWidth - GRID_GAPS_PX - MIN_MIDDLE_PX;
  const questionWidth = Math.min(questionPref, paneMax(QUESTION_PANE, room === null ? null : room - DOC_PANE.minPx));
  const docWidth = Math.min(docPref, paneMax(DOC_PANE, room === null ? null : room - questionWidth));
  const { leftAvailable, rightAvailable } = sideMaxima(gridWidth, questionWidth, docWidth, GRID_GAPS_PX);
  const activePreset = matchingPresetLevelId(spec, presets);
  const summaryLabel = pipelineSummaryLabel(spec, presets);

  return (
    <div className="flex flex-col gap-4" data-testid="workbench-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-ink-1">Composable RAG Workbench</h1>
          <Pill tone={activePreset ? "ok" : "info"} mono>{summaryLabel}</Pill>
          <BackingCorpusPill
            backingLevelId={backingLevelId}
            ingestState={ingestState}
            uploadScope={uploadScope}
            levelIngest={levelIngest}
          />
        </div>
        <Link to="/compare?mode=ablation" className="text-xs font-medium text-moss underline">
          Compare 2–4 recipes in Ablation Lab →
        </Link>
      </div>

      {levelIngest && backingDescriptor && (
        <LevelIngestBanner
          currentLevelId={backingLevelId}
          backingLevel={backingDescriptor}
          view={levelIngest}
        />
      )}

      <Panel
        title="Pipeline recipe builder"
        subtitle="Start from any of the 8 Level presets or mix-and-match index store, retriever, semantic reranker, context order, and ADK tools"
      >
        <SlotControls
          idPrefix="workbench"
          spec={spec}
          presets={presets}
          enabledLevels={enabledLevels}
          disabled={stream.isStreaming}
          onChange={setSpec}
        />
      </Panel>

      <Panel
        title="Live pipeline architecture"
        subtitle="Updates instantaneously as you toggle slots above; nodes light up from backend SSE events during runs"
      >
        <ArchitectureDiagram
          title={`${summaryLabel} architecture`}
          nodes={descriptor.nodes}
          edges={descriptor.edges}
          timeline={timeline}
          running={stream.isStreaming}
          selectedStage={selectedStage}
          onStageSelect={pickStage}
          runServiceByStage={runServiceByStage}
        />
      </Panel>

      <div
        ref={gridRef}
        className="grid gap-4 xl:grid-cols-[var(--question-pane-w)_minmax(0,1fr)_var(--doc-pane-w)]"
        style={{ "--question-pane-w": `${questionWidth}px`, "--doc-pane-w": `${docWidth}px` } as React.CSSProperties}
      >
        <div className="flex min-w-0 xl:row-span-2">
          <Panel title="Question" subtitle="Golden benchmark or custom" className="flex-1">
            <QuestionPicker
              levelId={backingLevelId}
              sessionKey={WORKBENCH_SESSION}
              gate={gate}
              isStreaming={stream.isStreaming}
              onRun={onRun}
              onAbort={stream.abort}
              onReset={onReset}
              hasResults={stream.hasContent || selectedStage !== null || citation !== null}
              models={models}
              spec={spec}
              presets={presets}
              runLabel="Run recipe"
              uploadScopeId={uploadScope.isUploadActive ? uploadScope.scope : null}
              scopeControl={
                <ScopeSelector
                  id="scope-workbench"
                  demoMode={health?.demo_mode ?? null}
                  scope={uploadScope.scope}
                  workspaces={uploadScope.workspaces}
                  loading={uploadScope.loading}
                  error={uploadScope.error}
                  disabled={stream.isStreaming}
                  onChange={uploadScope.setScope}
                />
              }
            />
            {gate.note && (
              <p className={`mt-2 text-xs ${gate.tone === "err" ? "text-err" : gate.tone === "warn" ? "text-clay" : "text-slate"}`} role="status">
                {gate.tone === "err" ? "Error" : gate.tone === "warn" ? "Blocked" : "Note"}: {gate.note}
              </p>
            )}
            {stream.transportError && (
              <div className="mt-2 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
                <span className="font-semibold">Run rejected / stream error:</span>{" "}
                <span className="break-words">{stream.transportError}</span>
              </div>
            )}
            {stream.state.warnings.map((w, i) => (
              <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>
            ))}
          </Panel>
          <PaneResizer
            label="Resize question pane"
            side="left"
            width={questionWidth}
            bounds={QUESTION_PANE}
            maxPx={paneMax(QUESTION_PANE, leftAvailable)}
            onChange={setQuestionWidth}
            className="-mr-3.5 ml-0.5"
          />
        </div>

        <Panel
          title="Stage rail"
          subtitle={stream.state.runId ? `run ${stream.state.runId}` : "idle — statuses come only from backend events"}
          actions={<RunPills state={stream.state} timeline={timeline} isStreaming={stream.isStreaming} synthLabel={runSynthLabel} judgeLabel={runJudgeLabel} />}
        >
          <div data-run-state={runStateAttr(stream.state, stream.isStreaming)}>
            <StageRail stageIds={stageIds} stages={timeline?.stages ?? {}} selectedStage={selectedStage} onSelectStage={pickStage} />
          </div>
        </Panel>

        <div className="flex min-w-0 xl:row-span-2">
          <PaneResizer
            label="Resize document viewer"
            side="right"
            width={docWidth}
            bounds={DOC_PANE}
            maxPx={paneMax(DOC_PANE, rightAvailable)}
            onChange={setDocWidth}
            className="-ml-3.5 mr-0.5"
          />
          <Panel title="Document viewer" subtitle="Real source page of the selected citation" className="flex-1" as="aside">
            <DocumentViewer citation={citation} onOpen={() => setViewerOpen(true)} />
          </Panel>
        </div>

        <Panel title="Stage detail" subtitle="Exact request and response from the event" className="xl:col-start-2">
          <StageDetail
            stageId={selectedStage}
            state={selectedStage ? timeline?.stages[selectedStage] ?? null : null}
            retrievePayload={timeline?.stages.retrieve?.payload ?? null}
            agentTurns={isAgentic ? timeline?.agentTurns ?? [] : null}
            answerSource={answerSource}
            citations={timeline?.answer?.citations}
            onSelectCitation={openCitation}
            labelFor={labelFor}
          />
        </Panel>

        <Panel title="Answer + citations" className="xl:col-span-2">
          <AnswerPanel
            timeline={timeline}
            running={stream.isStreaming}
            selectedCitation={citation}
            onSelectCitation={openCitation}
            labelFor={labelFor}
          />
        </Panel>
        <Panel title="Metrics" subtitle="LLM judge · cost · latency">
          <MetricsPanel
            timeline={timeline}
            running={stream.isStreaming}
            runSynthesisModel={stream.state.synthesisModel}
            labelFor={labelFor}
          />
        </Panel>
        <DocumentDrawer
          citation={citation}
          open={viewerOpen}
          onClose={() => setViewerOpen(false)}
          retrievedChunks={timeline?.answer?.retrievedChunks}
        />
      </div>
    </div>
  );
};
