import React from "react";
import type { LevelDescriptor } from "../../types/arena";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import { KeyValue } from "../../components/ui/KeyValue";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import { ArchitectureDiagram } from "../levels/ArchitectureDiagram";
import { StageRail } from "../levels/StageRail";
import { deriveIngestProgress } from "../levels/model";
import { INGEST_STAGE_IDS } from "../../types/arena";
import { ingestParseItems, isReleaseCandidateChannel, parseIngestParse } from "../levels/level3Payloads";
import type { IngestJobStream } from "./useIngestJobStream";
import { MANAGED_INGEST_STAGES, importErrorSamples, ingestStageOrder, managedIngestItems } from "../levels/level5Payloads";
import { isManagedLevel } from "./ingestStatus";
import { STAGE_LABELS } from "../levels/StageRail";

export interface IngestJobViewProps {
  readonly level: LevelDescriptor;
  readonly job: IngestJobStream;
  readonly selectedStage: string | null;
  readonly onSelectStage: (id: string) => void;
  readonly onDismiss?: () => void;
}

/** Live view of one ingestion job: rail + diagram (ingest nodes) + per-doc + embed progress. */
export const IngestJobView: React.FC<IngestJobViewProps> = ({ level, job, selectedStage, onSelectStage, onDismiss }) => {
  const timeline = job.state.timelines[level.level_id];
  const progress = deriveIngestProgress(job.state.events);
  const stageIds = ingestStageOrder(level.level_id, INGEST_STAGE_IDS, timeline?.stageOrder ?? []);
  const managed = isManagedLevel(level.level_id);
  const skipped = Object.entries(timeline?.stages ?? {}).filter(([, st]) => st.status === "skipped");
  const embedSkipped = timeline?.stages.embed?.status === "skipped";
  const managedRows = managed
    ? MANAGED_INGEST_STAGES.map((id) => ({ id, items: managedIngestItems(id, timeline?.stages[id]?.payload ?? null), status: timeline?.stages[id]?.status ?? "idle" }))
    : [];
  const importErrors = managed ? importErrorSamples(timeline?.stages.import_documents?.payload ?? null) : [];
  // tier_profile the backend reports for this job (job_started), e.g. "hybrid" for Level 2.
  const startedEvent = job.state.events.find((e) => e.event === "job_started");
  const jobTier = startedEvent && startedEvent.event === "job_started" ? startedEvent.tier_profile : null;
  const embedPct = progress.embed && progress.embed.total > 0 ? Math.round((progress.embed.done / progress.embed.total) * 100) : null;
  // Level 3 parse stage (Document AI layout + Gemini vision); null for Levels 1–2 parse payloads.
  const parseView = parseIngestParse(timeline?.stages.parse?.payload ?? null);
  const showTables = progress.docs.some((d) => d.tables !== null);
  const showDiagrams = progress.docs.some((d) => d.diagrams !== null);
  const showVision = progress.docs.some((d) => d.visionOk !== undefined && d.visionOk !== null);

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,26rem)]" data-testid="ingest-job-view">
      <Panel
        title={`Ingestion job · ${level.label}`}
        subtitle={
          job.jobId
            ? `job ${job.jobId} · ${job.scope ? `workspace ${job.scope}` : "procurement corpus"}${jobTier ? ` · tier_profile=${jobTier}` : ""}`
            : "creating job…"
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {job.state.complete ? (
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
            ) : null}
            {!job.isStreaming && onDismiss && (
              <Button size="sm" variant="ghost" onClick={onDismiss}>
                Clear
              </Button>
            )}
          </div>
        }
        className="xl:col-span-2"
      >
        <StageRail label="Ingestion stages" stageIds={stageIds} stages={timeline?.stages ?? {}} selectedStage={selectedStage} onSelectStage={onSelectStage} />
        {job.transportError && <p className="mt-2 text-xs text-err" role="alert">{job.transportError}</p>}
        {job.state.warnings.map((w, i) => (
          <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>
        ))}
      </Panel>

      <Panel
        title="Ingestion architecture"
        subtitle={managed ? "corpus → Cloud Storage → Vertex AI Search import → document count" : "corpus → parser → chunker → embeddings → BigQuery → indexes"}
        className="xl:col-span-2"
      >
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
        {managed && (
          <Panel title="Managed import" subtitle="upload_gcs / import_documents / verify_counts payloads (counts as reported)" className="xl:col-span-2">
            <div className="flex flex-col gap-3" data-testid="managed-import-summary">
              <div className="grid gap-3 md:grid-cols-3">
                {managedRows.map((row) => (
                  <section key={row.id} aria-label={`Stage ${row.id}`} className="flex min-w-0 flex-col gap-1 rounded-md border border-line bg-surface-0 p-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono text-xs font-semibold text-ink-1">{STAGE_LABELS[row.id] ?? row.id}</span>
                      <Pill tone={row.status === "done" ? "ok" : row.status === "failed" ? "err" : row.status === "active" ? "active" : row.status === "skipped" ? "locked" : "neutral"}>{row.status}</Pill>
                    </div>
                    {row.items.length > 0 ? <KeyValue items={row.items} /> : <p className="text-xs text-ink-3">{row.status === "done" ? "No counts in the payload (see stage detail)." : "—"}</p>}
                  </section>
                ))}
              </div>
              {importErrors.length > 0 && (
                <div role="alert" className="rounded-md border border-err p-2 text-xs">
                  <p className="mb-1 font-semibold text-err">Import errors reported by Vertex AI Search (verbatim, first {importErrors.length})</p>
                  <ul className="list-disc pl-5 font-mono text-ink-1">
                    {importErrors.map((e, i) => (
                      <li key={i} className="break-words">{e}</li>
                    ))}
                  </ul>
                </div>
              )}
              {skipped.length > 0 && (
                <div className="rounded-md border border-dashed border-line p-2 text-xs" data-testid="managed-skipped-stages">
                  <p className="mb-1 font-semibold text-ink-2">Stages not run by this app (stage_skipped, reason verbatim)</p>
                  <ul className="flex flex-col gap-0.5">
                    {skipped.map(([id, st]) => (
                      <li key={id} className="break-words">
                        <span className="font-mono text-ink-1">{id}</span> — <span className="text-ink-2">{st.skipReason ?? "no reason given"}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </Panel>
        )}
        {parseView && (
          <Panel title="Parse summary" subtitle="parse stage payload · Document AI layout + Gemini vision" className="xl:col-span-2">
            <div className="flex flex-col gap-3" data-testid="ingest-parse-summary">
              {parseView.truncated && (
                <p className="text-xs text-clay" role="status">
                  Note: payload truncated by telemetry bounds — some rows below may be missing (see raw payload).
                </p>
              )}
              {isReleaseCandidateChannel(parseView.processorVersionChannel) && (
                <p className="text-xs text-clay" role="status">
                  Warning: Document AI processor version is a release candidate ({parseView.processorVersionChannel}).
                </p>
              )}
              <KeyValue columns={2} label="Layout parse summary" items={ingestParseItems(parseView)} />
              {parseView.visionPageRows.length > 0 && (
                <DataTable
                  caption="Diagram pages transcribed by Gemini vision"
                  rows={parseView.visionPageRows}
                  rowKey={(r, i) => `${r.docName}-${r.page}-${i}`}
                  columns={[
                    { key: "doc", header: "Document · page", render: (r) => `${r.docName}${r.page !== null ? ` · p.${r.page}` : ""}`, mono: true },
                    { key: "title", header: "Diagram title", render: (r) => r.title ?? "—" },
                    { key: "components", header: "Components", render: (r) => r.components ?? "—", align: "right" },
                    { key: "connections", header: "Connections", render: (r) => r.connections ?? "—", align: "right" },
                    { key: "ms", header: "Vision", render: (r) => formatMs(r.visionMs), align: "right", mono: true },
                  ]}
                />
              )}
              {parseView.visionFailedPages.length > 0 && (
                <div role="alert" className="rounded-md border border-err p-2">
                  <p className="mb-1 text-xs font-semibold text-err">Gemini vision failed on {parseView.visionFailedPages.length} page(s) — no diagram chunk was created for them</p>
                  <DataTable
                    caption="Vision transcription failures"
                    rows={parseView.visionFailedPages}
                    rowKey={(r, i) => `${r.docName}-${r.page}-${i}`}
                    columns={[
                      { key: "doc", header: "Document · page", render: (r) => `${r.docName}${r.page !== null ? ` · p.${r.page}` : ""}`, mono: true },
                      { key: "type", header: "Error", render: (r) => r.errorType ?? "—", mono: true },
                      { key: "msg", header: "Message", render: (r) => r.error ?? r.reason ?? "—" },
                    ]}
                  />
                </div>
              )}
            </div>
          </Panel>
        )}
        {!embedSkipped && (
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
        )}
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
              ...(showTables ? [{ key: "tables", header: "Tables", render: (r: (typeof progress.docs)[number]) => r.tables ?? "—", align: "right" as const }] : []),
              ...(showVision
                ? [
                    {
                      key: "vision",
                      header: "Vision pages",
                      render: (r: (typeof progress.docs)[number]) => `${r.visionOk ?? 0} ok${r.visionFailed ? ` · ${r.visionFailed} failed` : ""}`,
                      align: "right" as const,
                    },
                  ]
                : []),
              ...(showDiagrams ? [{ key: "diagrams", header: "Diagrams", render: (r: (typeof progress.docs)[number]) => r.diagrams ?? "—", align: "right" as const }] : []),
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
