import React from "react";
import type { LevelDescriptor } from "../../types/arena";
import { DataTable } from "../../components/ui/DataTable";
import { KeyValue } from "../../components/ui/KeyValue";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import { ArchitectureDiagram } from "../levels/ArchitectureDiagram";
import { StageRail } from "../levels/StageRail";
import { deriveIngestProgress } from "../levels/model";
import { INGEST_STAGE_IDS } from "../../types/arena";
import type { IngestJobStream } from "./useIngestJobStream";

export interface IngestJobViewProps {
  readonly level: LevelDescriptor;
  readonly job: IngestJobStream;
  readonly selectedStage: string | null;
  readonly onSelectStage: (id: string) => void;
}

/** Live view of one ingestion job: rail + diagram (ingest nodes) + per-doc + embed progress. */
export const IngestJobView: React.FC<IngestJobViewProps> = ({ level, job, selectedStage, onSelectStage }) => {
  const timeline = job.state.timelines[level.level_id];
  const progress = deriveIngestProgress(job.state.events);
  const stageIds = timeline?.stageOrder.length ? [...new Set([...INGEST_STAGE_IDS, ...timeline.stageOrder])] : [...INGEST_STAGE_IDS];
  // tier_profile the backend reports for this job (job_started), e.g. "hybrid" for Level 2.
  const startedEvent = job.state.events.find((e) => e.event === "job_started");
  const jobTier = startedEvent && startedEvent.event === "job_started" ? startedEvent.tier_profile : null;
  const embedPct = progress.embed && progress.embed.total > 0 ? Math.round((progress.embed.done / progress.embed.total) * 100) : null;

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,26rem)]" data-testid="ingest-job-view">
      <Panel
        title={`Ingestion job · ${level.label}`}
        subtitle={job.jobId ? `job ${job.jobId}${jobTier ? ` · tier_profile=${jobTier}` : ""}` : "creating job…"}
        actions={
          job.state.complete ? (
            <Pill tone="ok">job_complete</Pill>
          ) : timeline?.error ? (
            <Pill tone="err" mono title={timeline.error.message}>job_failed · {timeline.error.type}</Pill>
          ) : job.state.interrupted ? (
            <Pill tone="err" title="The job stream ended without job_complete / job_failed; running stages were marked failed.">
              stream interrupted · {job.state.interrupted.reason}
            </Pill>
          ) : job.isStreaming ? (
            <Pill tone="active">streaming</Pill>
          ) : job.transportError ? (
            <Pill tone="err">stream error</Pill>
          ) : null
        }
        className="xl:col-span-2"
      >
        <StageRail label="Ingestion stages" stageIds={stageIds} stages={timeline?.stages ?? {}} selectedStage={selectedStage} onSelectStage={onSelectStage} />
        {job.transportError && <p className="mt-2 text-xs text-err" role="alert">{job.transportError}</p>}
        {job.state.warnings.map((w, i) => (
          <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>
        ))}
      </Panel>

      <Panel title="Ingestion architecture" subtitle="corpus → parser → chunker → embeddings → BigQuery → indexes" className="xl:col-span-2">
        <ArchitectureDiagram
          title={`${level.label} ingestion architecture`}
          nodes={level.architecture.ingest_nodes}
          edges={level.architecture.ingest_edges}
          timeline={timeline}
          running={job.isStreaming}
          selectedStage={selectedStage}
          onStageSelect={onSelectStage}
          detailsPlacement="below"
        />
      </Panel>

      <div className="grid min-w-0 gap-4 xl:col-span-2 xl:grid-cols-2">
        <Panel title="Embedding progress" subtitle="embed_progress events">
          {progress.embed ? (
            <div className="flex flex-col gap-1.5">
              <div className="h-2 w-full overflow-hidden rounded bg-surface-2" role="progressbar" aria-valuemin={0} aria-valuemax={progress.embed.total} aria-valuenow={progress.embed.done} aria-label="Chunks embedded">
                <div className="h-full bg-moss" style={{ width: `${embedPct ?? 0}%` }} />
              </div>
              <KeyValue
                items={[
                  ["embedded", `${progress.embed.done.toLocaleString()} / ${progress.embed.total.toLocaleString()} (${embedPct ?? 0}%)`],
                  ["last batch", formatMs(progress.embed.batchMs)],
                ]}
              />
            </div>
          ) : (
            <p className="text-xs text-ink-3">No embed_progress yet.</p>
          )}
        </Panel>
        <Panel title="Documents" subtitle="doc_progress events">
          <DataTable
            caption="Per-document ingestion progress"
            rows={progress.docs}
            rowKey={(r, i) => `${r.doc_name}-${i}`}
            emptyText="No doc_progress yet."
            columns={[
              { key: "doc", header: "Document", render: (r) => r.doc_name, mono: true },
              { key: "pages", header: "Pages", render: (r) => r.pages, align: "right" },
              { key: "chunks", header: "Chunks", render: (r) => r.chunks, align: "right" },
            ]}
          />
        </Panel>
        {(progress.countsByTier || progress.indexStates) && (
          <Panel title="Post-run artifacts" subtitle="job_complete payload">
            <div className="grid gap-3 md:grid-cols-2">
              <KeyValue label="Counts by tier" items={Object.entries(progress.countsByTier ?? {}).map(([k, v]) => [k, v.toLocaleString()] as const)} />
              <KeyValue label="Index states" items={Object.entries(progress.indexStates ?? {}).map(([k, v]) => [k, v] as const)} />
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
};
