import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { DocumentDrawer } from "../../components/DocumentDrawer";
import { DocumentViewer } from "../../components/DocumentViewer";
import { EmptyState } from "../../components/ui/EmptyState";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import type { Citation, LevelDescriptor } from "../../types/arena";
import { TIER_FOR_LEVEL, runGateFor, useIngestStatus, type IngestStatusState } from "../ingest/ingestStatus";
import { AgentTurnTimeline } from "./AgentTurnTimeline";
import { AnswerPanel, MetricsPanel } from "./AnswerPanels";
import { ArchitectureDiagram } from "./ArchitectureDiagram";
import { QuestionPicker } from "./QuestionPicker";
import { StageDetail } from "./StageDetail";
import { StageRail } from "./StageRail";
import { activeStageId } from "./model";
import { modelLabel, type ModelSelection } from "./modelSelection";
import { useBattleStream } from "./useBattleStream";
import { useModelCatalog } from "./useModelCatalog";

function mergeOrder(declared: readonly string[], seen: readonly string[]): string[] {
  const out = [...declared];
  for (const s of seen) if (!out.includes(s)) out.push(s);
  return out;
}

const LevelBody: React.FC<{ readonly level: LevelDescriptor; readonly ingestState: IngestStatusState }> = ({ level, ingestState }) => {
  const stream = useBattleStream();
  const models = useModelCatalog();
  const { status: ingest, error: ingestError } = ingestState;
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const [userPicked, setUserPicked] = useState(false);
  const [citation, setCitation] = useState<Citation | null>(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  const openCitation = useCallback((c: Citation) => {
    setCitation(c);
    setViewerOpen(true);
  }, []);
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
  }, [active, userPicked]);

  const pickStage = useCallback((id: string) => {
    setUserPicked(true);
    setSelectedStage(id);
  }, []);

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
  const isAgentic = level.stages.includes("agent_turn");

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(15rem,18rem)_minmax(0,1fr)_minmax(17rem,21rem)]">
      <Panel title="Question" subtitle="Golden benchmark or custom" className="xl:row-span-2">
        <QuestionPicker levelId={level.level_id} gate={gate} isStreaming={stream.isStreaming} onRun={onRun} onAbort={stream.abort} models={models} />
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
            {stream.state.complete ? <Pill tone="ok">battle_complete</Pill> : stream.isStreaming ? <Pill tone="active">streaming</Pill> : null}
          </>
        }
      >
        <div data-run-state={stream.state.complete ? "complete" : stream.isStreaming ? "streaming" : "idle"}>
          <StageRail stageIds={stageIds} stages={timeline?.stages ?? {}} selectedStage={selectedStage} onSelectStage={pickStage} />
        </div>
      </Panel>

      <Panel title="Document viewer" subtitle="Real source page of the selected citation" className="xl:row-span-2" as="aside">
        <DocumentViewer citation={citation} onOpen={openViewer} />
      </Panel>

      <Panel title="Stage detail" subtitle="Exact request and response from the event" className="xl:col-start-2">
        <StageDetail stageId={selectedStage} state={selectedStage ? timeline?.stages[selectedStage] ?? null : null} />
        {isAgentic && selectedStage === "agent_turn" && (
          <div className="mt-3">
            <AgentTurnTimeline turns={timeline?.agentTurns ?? []} active={active === "agent_turn"} />
          </div>
        )}
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
        <AnswerPanel timeline={timeline} running={stream.isStreaming} selectedCitation={citation} onSelectCitation={openCitation} />
      </Panel>
      <Panel title="Metrics" subtitle="LLM judge · cost · latency">
        <MetricsPanel timeline={timeline} running={stream.isStreaming} runSynthesisModel={stream.state.synthesisModel} labelFor={(id) => modelLabel(models.catalog, id)} />
      </Panel>
      <DocumentDrawer citation={citation} open={viewerOpen} onClose={closeViewer} />
    </div>
  );
};

const IngestStatusPill: React.FC<{ readonly levelId: string; readonly ingestState: IngestStatusState }> = ({ levelId, ingestState }) => {
  const { status, error } = ingestState;
  const tier = TIER_FOR_LEVEL[levelId];
  const gate = runGateFor(levelId, status, error);
  if (!status && !error) return <Pill tone="neutral">ingest status…</Pill>;
  const text = gate.count !== null ? `${tier ?? "managed"} tier: ${gate.count} chunks` : gate.note ?? "ingest status";
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
      <LevelBody key={level.level_id} level={level} ingestState={ingestState} />
    </div>
  );
};
