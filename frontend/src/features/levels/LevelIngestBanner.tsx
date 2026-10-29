import React from "react";
import { Link } from "react-router-dom";
import type { LevelIngestView } from "../../app/IngestSessions";
import { Button } from "../../components/ui/Button";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import { INGEST_STAGE_IDS, type LevelDescriptor } from "../../types/arena";
import { ingestStageOrder } from "./level5Payloads";
import { deriveIngestProgress } from "./model";
import { StageRail } from "./StageRail";

export interface LevelIngestBannerProps {
  readonly currentLevelId: string;
  readonly backingLevel: LevelDescriptor;
  readonly view: LevelIngestView;
}

function statusPillFor(view: LevelIngestView): React.ReactNode {
  const { entry, backingLevelId } = view;
  const timeline = entry.state.timelines[backingLevelId];
  if (entry.state.complete) return <Pill tone="ok">job_complete</Pill>;
  if (timeline?.error) {
    return (
      <Pill tone="err" mono title={timeline.error.message}>
        job_failed · {timeline.error.type}
      </Pill>
    );
  }
  if (entry.state.interrupted) {
    return <Pill tone="err">stream interrupted · {entry.state.interrupted.reason}</Pill>;
  }
  if (entry.isStreaming) return <Pill tone="active">streaming</Pill>;
  if (entry.transportError) return <Pill tone="err">stream error</Pill>;
  return null;
}

/**
 * Live ingestion progress panel shown on a RAG level page (`/levels/<levelId>`) while an ingestion job
 * for its backing tier is running, or after completion until dismissed.
 */
export const LevelIngestBanner: React.FC<LevelIngestBannerProps> = ({ currentLevelId, backingLevel, view }) => {
  const { entry, backingLevelId, selectStage, dismiss } = view;
  const timeline = entry.state.timelines[backingLevelId];
  const progress = deriveIngestProgress(entry.state.events);
  const stageIds = ingestStageOrder(backingLevelId, INGEST_STAGE_IDS, timeline?.stageOrder ?? []);
  const startedEvent = entry.state.events.find((e) => e.event === "job_started");
  const jobTier = startedEvent && startedEvent.event === "job_started" ? startedEvent.tier_profile : null;
  const embedPct =
    progress.embed && progress.embed.total > 0
      ? Math.round((progress.embed.done / progress.embed.total) * 100)
      : null;
  const sharedNote = currentLevelId !== backingLevelId ? " (feeds Level 4 Agentic RAG)" : "";
  const scopeLabel = entry.scope ? `workspace ${entry.scope}` : "procurement corpus";

  return (
    <div data-testid="level-ingest-panel">
      <Panel
        title={`Ingestion job · ${backingLevel.label}${sharedNote}`}
        subtitle={
          entry.jobId
            ? `job ${entry.jobId} · ${scopeLabel}${jobTier ? ` · tier_profile=${jobTier}` : ""}`
            : `creating job · ${scopeLabel}…`
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {statusPillFor(view)}
            {progress.docs.length > 0 && (
              <Pill tone="neutral" mono>
                {progress.docs.length} {progress.docs.length === 1 ? "doc" : "docs"}
              </Pill>
            )}
            <Link to="/ingest" className="text-xs font-medium text-moss underline">
              Open Ingest →
            </Link>
            {!entry.isStreaming && (
              <Button size="sm" variant="ghost" onClick={dismiss}>
                Dismiss
              </Button>
            )}
          </div>
        }
        bodyClassName="flex flex-col gap-2.5"
      >
        <StageRail
          label="Ingestion stages"
          stageIds={stageIds}
          stages={timeline?.stages ?? {}}
          selectedStage={entry.selectedStage}
          onSelectStage={selectStage}
        />
        {progress.embed && (
          <div className="flex flex-col gap-1" data-testid="level-ingest-embed-progress">
            <div
              className="h-1.5 w-full overflow-hidden rounded bg-surface-2"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={progress.embed.total}
              aria-valuenow={progress.embed.done}
              aria-label="Chunks embedded"
            >
              <div className="h-full bg-moss" style={{ width: `${embedPct ?? 0}%` }} />
            </div>
            <p className="font-mono text-xs text-ink-2">
              embedded {progress.embed.done.toLocaleString()} / {progress.embed.total.toLocaleString()} ({embedPct ?? 0}%) · last batch{" "}
              {formatMs(progress.embed.batchMs)}
            </p>
          </div>
        )}
        {entry.transportError && (
          <p className="text-xs text-err" role="alert">
            {entry.transportError}
          </p>
        )}
        {timeline?.error && (
          <p className="text-xs text-err" role="alert">
            {timeline.error.type}: {timeline.error.message}
          </p>
        )}
        {entry.state.warnings.map((w, i) => (
          <p key={i} className="text-xs text-clay" role="status">
            {w}
          </p>
        ))}
      </Panel>
    </div>
  );
};
