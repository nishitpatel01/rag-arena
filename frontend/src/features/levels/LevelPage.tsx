import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { useSessionUi } from "../../app/BattleSessions";
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
import { QuestionPicker } from "./QuestionPicker";
import { StageDetail, type AnswerSource } from "./StageDetail";
import { StageRail } from "./StageRail";
import { activeStageId } from "./model";
import { modelLabel, type ModelSelection } from "./modelSelection";
import { useBattleStream } from "./useBattleStream";
import { useModelCatalog } from "./useModelCatalog";

/** Two `gap-4` (16 px) column gaps in the level grid. */
const GRID_GAPS_PX = 32;

const isStageId = (v: unknown): v is string | null => v === null || typeof v === "string";
const isCitation = (v: unknown): v is Citation | null => v === null || (typeof v === "object" && !Array.isArray(v));

function mergeOrder(declared: readonly string[], seen: readonly string[]): string[] {
  const out = [...declared];
  for (const s of seen) if (!out.includes(s)) out.push(s);
  return out;
}

const LevelBody: React.FC<{ readonly level: LevelDescriptor; readonly ingestState: IngestStatusState; readonly lowerLevels: readonly LowerLevel[] }> = ({
  level,
  ingestState,
  lowerLevels,
}) => {
  // Page session: run, results and selections survive navigation (and keep streaming) until Reset / a new Run.
  const sessionKey = `level:${level.level_id}`;
  const stream = useBattleStream(sessionKey);
  const models = useModelCatalog(level.level_id);
  const { status: ingest, error: ingestError } = ingestState;
  const { maskValue } = useSafeMode();
  const [selectedStage, setSelectedStage] = useSessionUi<string | null>(sessionKey, "selectedStage", null, isStageId);
  const [userPicked, setUserPicked] = useSessionUi<boolean>(sessionKey, "userPicked", false);
  const [storedCitation, setCitation] = useSessionUi<Citation | null>(sessionKey, "citation", null, isCitation);
  const citation = useMemo(() => maskValue(storedCitation), [maskValue, storedCitation]);
  const [viewerOpen, setViewerOpen] = useState(false);
  const openCitation = useCallback((c: Citation) => {
    setCitation(c);
    setViewerOpen(true);
  }, [setCitation]);
  const openViewer = useCallback(() => setViewerOpen(true), []);
  const closeViewer = useCallback(() => setViewerOpen(false), []);

  const timeline = stream.state.timelines[level.level_id];
  const active = activeStageId(timeline);
  const gate = stream.isMock
    ? { blocked: false, note: null, tone: "neutral" as const, count: null }
    : runGateFor(level.level_id, ingest, ingestError);

  // Auto-follow the active stage until the user picks one explicitly.
  useEffect(() => {
    if (!userPicked && active) setSelectedStage(active);
  }, [active, userPicked, setSelectedStage]);

  const pickStage = useCallback((id: string) => {
    setUserPicked(true);
    setSelectedStage(id);
  }, [setUserPicked, setSelectedStage]);

  const onRun = (query: string, questionId: string | null, runModels: ModelSelection | null): void => {
    setSelectedStage(null);
    setUserPicked(false);
    setCitation(null);
    setViewerOpen(false);
    stream.run({
      query,
      questionId,
      engines: [level.level_id],
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
  // Teaser only after a finished run with judge metrics, on a level that has lower enabled levels.
  const showTeaser = lowerLevels.length > 0 && stream.state.complete && Boolean(timeline?.evaluation);
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
    <div
      ref={gridRef}
      className="grid gap-4 xl:grid-cols-[var(--question-pane-w)_minmax(0,1fr)_var(--doc-pane-w)]"
      style={{ "--question-pane-w": `${questionWidth}px`, "--doc-pane-w": `${docWidth}px` } as React.CSSProperties}
    >
      <div className="flex min-w-0 xl:row-span-2">
      <Panel title="Question" subtitle="Golden benchmark or custom" className="flex-1">
        <QuestionPicker levelId={level.level_id} sessionKey={sessionKey} gate={gate} isStreaming={stream.isStreaming} onRun={onRun}
          onAbort={stream.abort} onReset={onReset} hasResults={stream.hasContent || selectedStage !== null || citation !== null} models={models} />
        {gate.note && (
          <p className={`mt-2 text-xs ${gate.tone === "err" ? "text-err" : gate.tone === "warn" ? "text-clay" : "text-slate"}`} role="status">
            {gate.tone === "err" ? "Error" : gate.tone === "warn" ? "Blocked" : "Note"}: {gate.note}
          </p>
        )}
        {stream.transportError && (
          <div className="mt-2 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
            <span className="font-semibold">Run rejected / stream error:</span> <span className="break-words">{stream.transportError}</span>
          </div>
        )}
        {stream.state.warnings.map((w, i) => (
          <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>
        ))}
      </Panel>
      <PaneResizer label="Resize question pane" side="left" width={questionWidth} bounds={QUESTION_PANE}
        maxPx={paneMax(QUESTION_PANE, leftAvailable)} onChange={setQuestionWidth} className="-mr-3.5 ml-0.5" />
      </div>

      <Panel
        title="Stage rail"
        subtitle={stream.state.runId ? `run ${stream.state.runId}` : "idle — statuses come only from backend events"}
        actions={
          <>
            {stream.state.demoMode && (
              <Pill tone={stream.state.replayed ? "info" : "ok"} mono title={stream.state.replayed ? "Frames carry replayed:true (recording of a real run)" : "DEMO_MODE of this run"}>
                {stream.state.replayed ? "replayed recording" : stream.state.demoMode}
              </Pill>
            )}
            {(runSynthLabel || runJudgeLabel) && (
              <Pill tone="neutral" mono title="Models from run_started (what this run actually executed)">
                synthesis: {runSynthLabel ?? "—"} · judge: {runJudgeLabel ?? "—"}
              </Pill>
            )}
            {timeline?.error && <Pill tone="err" mono title={timeline.error.message}>engine_error · {timeline.error.type}</Pill>}
            {stream.state.interrupted && (
              <Pill tone="err" title="The SSE stream ended without battle_complete / engine_error; running stages were marked failed.">
                stream interrupted · {stream.state.interrupted.reason}
              </Pill>
            )}
            {stream.state.complete ? <Pill tone="ok">battle_complete</Pill> : stream.isStreaming ? <Pill tone="active">streaming</Pill> : null}
          </>
        }
      >
        <div data-run-state={stream.state.complete ? "complete" : stream.state.interrupted ? "interrupted" : stream.isStreaming ? "streaming" : "idle"}>
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
  );
};

const IngestStatusPill: React.FC<{ readonly levelId: string; readonly ingestState: IngestStatusState }> = ({ levelId, ingestState }) => {
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
  const { levels, loading } = useAppData();
  const level = levels.find((l) => l.level_id === levelId);
  // One request for the whole page (header pill + run gate share it).
  const ingestState = useIngestStatus();
  const back = <Link to="/" className="text-xs text-moss underline">Back to overview</Link>;
  const lowerLevels = useMemo(() => (level ? lowerEnabledLevels(levels, level) : []), [levels, level]);

  if (loading) return <p className="text-xs text-ink-3">Loading level…</p>;
  if (!level) {
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
        <IngestStatusPill levelId={level.level_id} ingestState={ingestState} />
      </div>
      <LevelBody key={level.level_id} level={level} ingestState={ingestState} lowerLevels={lowerLevels} />
    </div>
  );
};
