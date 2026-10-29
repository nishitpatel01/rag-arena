import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { useLevelIngest, type LevelIngestView } from "../../app/IngestSessions";
import { useSafeMode } from "../../app/SafeMode";
import { DocumentDrawer } from "../../components/DocumentDrawer";
import { DocumentViewer } from "../../components/DocumentViewer";
import { EmptyState } from "../../components/ui/EmptyState";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { PaneResizer, useElementWidth, usePaneWidth } from "../../components/ui/PaneResizer";
import { DOC_PANE, MIN_MIDDLE_PX, QUESTION_PANE, paneMax, sideMaxima } from "../../lib/paneWidth";
import type { Citation, LevelDescriptor } from "../../types/arena";
import { ingestPillText, runGateFor, useIngestStatus, type IngestStatusState } from "../ingest/ingestStatus";
import { parseAgentStagePayload } from "./agentTimelineModel";
import { AnswerPanel, MetricsPanel } from "./AnswerPanels";
import { CompareTeaser } from "./CompareTeaser";
import { lowerEnabledLevels, teaserTitle, type LowerLevel } from "./compareTeaserModel";
import { ArchitectureDiagram } from "./ArchitectureDiagram";
import { CompareRunBanner } from "./CompareRunBanner";
import { LevelIngestBanner } from "./LevelIngestBanner";
import { LevelQuestionPanel } from "./LevelQuestionPanel";
import { RunPills, runStateAttr } from "./RunPills";
import { StageDetail, type AnswerSource } from "./StageDetail";
import { StageRail } from "./StageRail";
import { activeStageId } from "./model";
import { modelLabel, type ModelSelection } from "./modelSelection";
import { useLevelRun } from "./useLevelRun";
import { useModelCatalog } from "./useModelCatalog";
import { useRunSelection } from "./useRunSelection";
import { useViewedRunInputs } from "./useViewedRunInputs";
import { ScopeSelector } from "../uploads/ScopeSelector";
import { uploadRunGateFor, type UploadWorkspace } from "../uploads/uploadModel";
import { useUploadWorkspaces, type UploadWorkspacesState } from "../uploads/useUploadWorkspaces";

/** Two `gap-4` (16 px) column gaps in the level grid. */
const GRID_GAPS_PX = 32;

function mergeOrder(declared: readonly string[], seen: readonly string[]): string[] {
  const out = [...declared];
  for (const s of seen) if (!out.includes(s)) out.push(s);
  return out;
}

const LevelBody: React.FC<{
  readonly level: LevelDescriptor;
  readonly backingIngestLevel: LevelDescriptor;
  readonly levelIngest: LevelIngestView | null;
  readonly ingestState: IngestStatusState;
  readonly lowerLevels: readonly LowerLevel[];
  readonly demoMode: string | null;
  readonly uploadScope: UploadWorkspacesState;
}> = ({
  level,
  backingIngestLevel,
  levelIngest,
  ingestState,
  lowerLevels,
  demoMode,
  uploadScope,
}) => {
  // Page session: run, results and selections survive navigation (and keep streaming) until Reset / a new Run.
  // "Latest run wins": the page shows its own run or the newer Compare run that included this level.
  const sessionKey = `level:${level.level_id}`;
  const stream = useLevelRun(level.level_id);
  const catalogModels = useModelCatalog(level.level_id);
  const { status: ingest, error: ingestError } = ingestState;
  const { maskValue } = useSafeMode();
  const { selectedStage, setSelectedStage, userPicked, setUserPicked, citation: storedCitation, setCitation, clearOwn } = useRunSelection(sessionKey, stream);
  const citation = useMemo(() => maskValue(storedCitation), [maskValue, storedCitation]);
  const [viewerOpen, setViewerOpen] = useState(false);
  const openCitation = useCallback((c: Citation) => {
    setCitation(c);
    setViewerOpen(true);
  }, [setCitation]);
  const openViewer = useCallback(() => setViewerOpen(true), []);
  const closeViewer = useCallback(() => setViewerOpen(false), []);
  const { models, viewing } = useViewedRunInputs(stream, catalogModels);

  const timeline = stream.state.timelines[level.level_id];
  const active = activeStageId(timeline);
  const rawGate = stream.isMock
    ? { blocked: false, note: null, tone: "neutral" as const, count: null }
    : uploadScope.isUploadActive
      ? uploadScope.loading
        ? { blocked: true, note: "Checking upload workspace…", tone: "neutral" as const, count: null }
        : uploadRunGateFor(level.level_id, uploadScope.activeWorkspace)
      : runGateFor(level.level_id, ingest, ingestError);
  const gate =
    rawGate.blocked && levelIngest?.entry.isStreaming
      ? {
          ...rawGate,
          count: null,
          tone: "info" as const,
          note: `Ingestion in progress (${levelIngest.entry.jobId ?? levelIngest.backingLevelId}) — Run unlocks automatically on job_complete`,
        }
      : rawGate;

  // Auto-follow the active stage until the user picks one explicitly.
  useEffect(() => {
    if (!userPicked && active) setSelectedStage(active);
  }, [active, userPicked, setSelectedStage]);

  const pickStage = useCallback((id: string) => {
    setUserPicked(true);
    setSelectedStage(id);
  }, [setUserPicked, setSelectedStage]);

  const onRun = (query: string, questionId: string | null, runModels: ModelSelection | null): void => {
    setViewerOpen(false);
    clearOwn(); // the new level run becomes the page's source (newest): start with no selections
    stream.run({
      query,
      questionId: uploadScope.isUploadActive ? null : questionId,
      engines: [level.level_id],
      solicitationFilter: uploadScope.isUploadActive ? uploadScope.scope : null,
      synthesisModel: runModels?.synthesis ?? null,
      judgeModel: runModels?.judge ?? null,
    });
  };

  const onReset = (): void => {
    setViewerOpen(false);
    stream.reset();
  };

  // Models of the run actually executed (from run_started), never the dropdown state.
  const runSynthLabel = modelLabel(models.catalog, stream.state.synthesisModel);
  const runJudgeLabel = modelLabel(models.catalog, stream.state.judgeModel);
  const runServiceByStage = useMemo(
    () => ({ synthesize: runSynthLabel, evaluate: runJudgeLabel }),
    [runSynthLabel, runJudgeLabel]
  );

  const stageIds = useMemo(
    () => (timeline?.stageOrder.length ? mergeOrder(level.stages, timeline.stageOrder) : level.stages),
    [level.stages, timeline?.stageOrder]
  );
  const isAgentic = level.stages.includes("agent_turn") || Boolean(timeline?.stages.agent_turn) || (timeline?.agentTurns.length ?? 0) > 0;
  const answerSource = useMemo<AnswerSource | null>(() => {
    if (!isAgentic) return null;
    const p = parseAgentStagePayload(timeline?.stages.agent_turn?.payload ?? null);
    const flag = timeline?.answer?.flags?.adk_runner_used;
    return { adkRunnerUsed: typeof flag === "boolean" ? flag : null, finalAnswerSource: p?.finalAnswerSource ?? null };
  }, [isAgentic, timeline?.stages.agent_turn?.payload, timeline?.answer?.flags]);
  // Teaser only after a finished golden-question run with judge metrics, on a level that has lower enabled levels.
  const showTeaser =
    !uploadScope.isUploadActive &&
    Boolean(stream.state.questionId) &&
    lowerLevels.length > 0 &&
    (stream.state.complete || stream.source === "compare") &&
    !stream.isStreaming &&
    Boolean(timeline?.evaluation);
  const labelFor = useCallback((id: string | null) => modelLabel(models.catalog, id), [models.catalog]);
  const [questionPref, setQuestionWidth] = usePaneWidth("question", QUESTION_PANE);
  const [docPref, setDocWidth] = usePaneWidth("document-viewer", DOC_PANE);
  const [gridRef, gridWidth] = useElementWidth<HTMLDivElement>();
  // Rendered widths: preferences clipped so the middle column keeps MIN_MIDDLE_PX (the 1fr middle absorbs the rest).
  const room = gridWidth === null ? null : gridWidth - GRID_GAPS_PX - MIN_MIDDLE_PX;
  const questionWidth = Math.min(questionPref, paneMax(QUESTION_PANE, room === null ? null : room - DOC_PANE.minPx));
  const docWidth = Math.min(docPref, paneMax(DOC_PANE, room === null ? null : room - questionWidth));
  const { leftAvailable, rightAvailable } = sideMaxima(gridWidth, questionWidth, docWidth, GRID_GAPS_PX);

  return (
    <>
    {levelIngest && (
      <LevelIngestBanner
        currentLevelId={level.level_id}
        backingLevel={backingIngestLevel}
        view={levelIngest}
      />
    )}
    <CompareRunBanner run={stream} labelFor={labelFor} />
    <div
      ref={gridRef}
      className="grid gap-4 xl:grid-cols-[var(--question-pane-w)_minmax(0,1fr)_var(--doc-pane-w)]"
      style={{ "--question-pane-w": `${questionWidth}px`, "--doc-pane-w": `${docWidth}px` } as React.CSSProperties}
    >
      <div className="flex min-w-0 xl:row-span-2">
        <LevelQuestionPanel
          levelId={level.level_id}
          sessionKey={sessionKey}
          gate={gate}
          run={stream}
          models={models}
          viewing={viewing}
          hasResults={stream.hasContent || selectedStage !== null || citation !== null}
          onRun={onRun}
          onReset={onReset}
          uploadScopeId={uploadScope.isUploadActive ? uploadScope.scope : null}
          scopeControl={
            <ScopeSelector
              id={`scope-${level.level_id}`}
              demoMode={demoMode}
              scope={uploadScope.scope}
              workspaces={uploadScope.workspaces}
              loading={uploadScope.loading}
              error={uploadScope.error}
              disabled={stream.ownStreaming}
              onChange={uploadScope.setScope}
            />
          }
        />
      <PaneResizer label="Resize question pane" side="left" width={questionWidth} bounds={QUESTION_PANE}
        maxPx={paneMax(QUESTION_PANE, leftAvailable)} onChange={setQuestionWidth} className="-mr-3.5 ml-0.5" />
      </div>

      <Panel
        title="Stage rail"
        subtitle={stream.state.runId ? `run ${stream.state.runId}` : "idle — statuses come only from backend events"}
        actions={<RunPills state={stream.state} timeline={timeline} isStreaming={stream.isStreaming} synthLabel={runSynthLabel} judgeLabel={runJudgeLabel} />}
      >
        <div data-run-state={runStateAttr(stream.state, stream.isStreaming)} data-run-source={stream.source}>
          <StageRail stageIds={stageIds} stages={timeline?.stages ?? {}} selectedStage={selectedStage} onSelectStage={pickStage} />
        </div>
      </Panel>

      <div className="flex min-w-0 xl:row-span-2">
        <PaneResizer label="Resize document viewer" side="right" width={docWidth} bounds={DOC_PANE}
          maxPx={paneMax(DOC_PANE, rightAvailable)} onChange={setDocWidth} className="-ml-3.5 mr-0.5" />
        <Panel title="Document viewer" subtitle="Real source page of the selected citation" className="flex-1" as="aside">
          <DocumentViewer citation={citation} onOpen={openViewer} />
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

      <Panel title="Architecture" subtitle="Live diagram — nodes light up on stage_started; details follow the active node" className="xl:col-span-3">
        <ArchitectureDiagram
          title={`${level.label} architecture`}
          nodes={level.architecture.nodes}
          edges={level.architecture.edges}
          timeline={timeline}
          running={stream.isStreaming}
          selectedStage={selectedStage}
          onStageSelect={pickStage}
          runServiceByStage={runServiceByStage}
        />
      </Panel>

      <Panel title="Answer + citations" className="xl:col-span-2">
        <AnswerPanel timeline={timeline} running={stream.isStreaming} selectedCitation={citation} onSelectCitation={openCitation} labelFor={labelFor} />
      </Panel>
      <Panel title="Metrics" subtitle="LLM judge · cost · latency">
        <MetricsPanel timeline={timeline} running={stream.isStreaming} runSynthesisModel={stream.state.synthesisModel} labelFor={labelFor} />
      </Panel>
      {showTeaser && (
        <Panel title={teaserTitle(lowerLevels)} subtitle="Read-only teaser · full Compare page in Phase 6" className="xl:col-start-3">
          <CompareTeaser
            timeline={timeline}
            catalog={models.catalog}
            synthesisModel={stream.state.synthesisModel}
            judgeModel={stream.state.judgeModel}
            questionId={stream.state.questionId}
            labelFor={labelFor}
            lowerLevels={lowerLevels}
          />
        </Panel>
      )}
      <DocumentDrawer citation={citation} open={viewerOpen} onClose={closeViewer} retrievedChunks={timeline?.answer?.retrievedChunks} />
    </div>
    </>
  );
};

const IngestStatusPill: React.FC<{
  readonly levelId: string;
  readonly ingestState: IngestStatusState;
  readonly activeUpload: UploadWorkspace | null;
  readonly isUploadActive: boolean;
  readonly levelIngest: LevelIngestView | null;
}> = ({ levelId, ingestState, activeUpload, isUploadActive, levelIngest }) => {
  if (levelIngest?.entry.isStreaming) {
    const scopeTag = levelIngest.entry.scope ? `${levelIngest.entry.scope} · ` : "";
    const jobTag = levelIngest.entry.jobId ? ` · ${levelIngest.entry.jobId}` : "";
    return (
      <Link to="/ingest" className="inline-flex max-w-[40rem]" aria-label="Open ingestion page">
        <Pill tone="active" mono data-testid="level-ingesting-pill">
          {scopeTag}ingesting…{jobTag}
        </Pill>
      </Link>
    );
  }
  if (isUploadActive) {
    const gate = uploadRunGateFor(levelId, activeUpload);
    const label = activeUpload
      ? gate.blocked
        ? `${activeUpload.id} · not ingested`
        : `${activeUpload.id} · ${gate.count ?? 0} ${levelId === "managed_vertex" ? "docs" : "chunks"}`
      : "upload workspace";
    return (
      <Link to="/ingest" className="inline-flex max-w-[40rem]" aria-label="Open ingestion page" title={gate.note ?? undefined}>
        <Pill tone={gate.tone} mono>{label}</Pill>
      </Link>
    );
  }
  const { status, error } = ingestState;
  const gate = runGateFor(levelId, status, error);
  if (!status && !error) return <Pill tone="neutral">ingest status…</Pill>;
  const text = ingestPillText(levelId, gate, status);
  return (
    <Link to="/ingest" className="inline-flex max-w-[40rem]" aria-label="Open ingestion page" title={gate.note ?? undefined}>
      <Pill tone={gate.tone} mono={gate.count !== null}>{text}</Pill>
    </Link>
  );
};

export const LevelPage: React.FC = () => {
  const { levelId = "" } = useParams();
  const { levels, loading, health } = useAppData();
  const level = levels.find((l) => l.level_id === levelId);
  // One request for the whole page (header pill + run gate share it).
  const ingestState = useIngestStatus();
  const uploadScope = useUploadWorkspaces(health?.demo_mode ?? null);
  const levelIngest = useLevelIngest(level?.level_id ?? "");
  const backingIngestLevel =
    (levelIngest ? levels.find((l) => l.level_id === levelIngest.backingLevelId) : undefined) ?? level;
  const back = <Link to="/" className="text-xs text-moss underline">Back to overview</Link>;
  const lowerLevels = useMemo(() => (level ? lowerEnabledLevels(levels, level) : []), [levels, level]);

  if (loading) return <p className="text-xs text-ink-3">Loading level…</p>;
  if (!level || !backingIngestLevel) {
    return <EmptyState title="Unknown level" description={`No level with id "${levelId}" was returned by GET /api/v1/levels.`} action={back} />;
  }
  if (!level.enabled) {
    return <EmptyState title={`${level.label} is locked`} description="This level is enabled only after the previous level has passed its Definition of Done (spec §9)." action={back} />;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-ink-1">
          {/^level\s*\d/i.test(level.label) ? level.label : `Level ${level.order} · ${level.label}`}
        </h1>
        <IngestStatusPill
          levelId={level.level_id}
          ingestState={ingestState}
          activeUpload={uploadScope.activeWorkspace}
          isUploadActive={uploadScope.isUploadActive}
          levelIngest={levelIngest}
        />
      </div>
      <LevelBody
        key={level.level_id}
        level={level}
        backingIngestLevel={backingIngestLevel}
        levelIngest={levelIngest}
        ingestState={ingestState}
        lowerLevels={lowerLevels}
        demoMode={health?.demo_mode ?? null}
        uploadScope={uploadScope}
      />
    </div>
  );
};
