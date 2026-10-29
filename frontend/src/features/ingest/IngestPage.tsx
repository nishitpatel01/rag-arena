import React from "react";
import { Play, RefreshCw } from "lucide-react";
import { useAppData } from "../../app/AppDataProvider";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { KeyValue } from "../../components/ui/KeyValue";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { formatTimestamp, statusTone } from "../../lib/format";
import type { IngestRunSummary, LevelDescriptor } from "../../types/arena";
import { IngestJobView } from "./IngestJobView";
import { TIER_FOR_LEVEL, formatIngestRunCount, isManagedLevel, managedStoreFor, tierCountFor, useIngestStatus } from "./ingestStatus";
import { DEV_INGEST_FIXTURE_LEVELS, useIngestJobStream } from "./useIngestJobStream";
import { isMockRequested } from "../levels/useBattleStream";
import { UploadWorkspacePanel } from "../uploads/UploadWorkspacePanel";

const LevelIngestCard: React.FC<{
  readonly level: LevelDescriptor;
  readonly count: number | null;
  readonly lastRuns: readonly IngestRunSummary[];
  readonly busy: boolean;
  readonly unavailableReason: string | null;
  readonly onRun: () => void;
  /** Level 5: data store state from `GET /ingest/status` → `managed_vertex` (null when not reported). */
  readonly storeState?: string | null;
  readonly storeReason?: string | null;
}> = ({ level, count, lastRuns, busy, unavailableReason, onRun, storeState = null, storeReason = null }) => {
  const tier = TIER_FOR_LEVEL[level.level_id];
  const managed = isManagedLevel(level.level_id);
  // DEV `?mock=1`: levels with an ingest fixture play it instead of calling the backend.
  const devIngestFixture = import.meta.env.DEV && isMockRequested() && DEV_INGEST_FIXTURE_LEVELS.includes(level.level_id);
  const alreadyLoaded = count !== null && count > 0;
  const countItems: (readonly [string, string])[] = managed
    ? [
        ["corpus documents in data store", count === null ? "not reported" : count.toLocaleString()],
        ["data store state", storeState ?? "not reported"],
        ...(storeReason ? [["status note", storeReason] as const] : []),
      ]
    : [["corpus chunks in BigQuery", count === null ? (tier === null ? "n/a (managed)" : "—") : count.toLocaleString()]];
  return (
    <Panel
      title={/^level\s*\d/i.test(level.label) ? level.label : `Level ${level.order} · ${level.label}`}
      subtitle={tier ? `benchmark corpus · tier_profile = ${tier}` : managed ? "benchmark corpus · Vertex AI Search data store" : "managed service — no BigQuery tier"}
      actions={
        <div className="flex flex-wrap items-center gap-1.5">
          {alreadyLoaded && <Pill tone="ok">ready to query</Pill>}
          {level.dev_fixture ? <Pill tone="warn">dev fixture</Pill> : level.enabled ? <Pill tone="neutral">enabled</Pill> : <Pill tone="locked">locked</Pill>}
        </div>
      }
      bodyClassName="flex flex-col gap-3"
    >
      <KeyValue items={countItems} />
      <DataTable
        caption={`Last ingestion runs for ${level.label}`}
        rows={lastRuns}
        rowKey={(r) => r.run_id}
        emptyText="No ingest_runs rows for this level."
        columns={[
          { key: "run", header: "run_id", render: (r) => <span title={r.run_id}>{r.run_id}</span>, mono: true },
          { key: "status", header: "Status", render: (r) => <Pill tone={statusTone(r.status)}>{r.status}</Pill> },
          { key: "docs", header: "Docs", render: (r) => formatIngestRunCount(r.docs), align: "right" },
          { key: "pages", header: "Pages", render: (r) => formatIngestRunCount(r.pages), align: "right" },
          { key: "chunks", header: "Chunks", render: (r) => formatIngestRunCount(r.chunks), align: "right" },
          { key: "layout", header: "Parser", render: (r) => r.layout_source ?? "—", mono: true },
          { key: "sha", header: "git", render: (r) => (r.git_sha && r.git_sha !== "unknown" ? <span title={r.git_sha}>{r.git_sha.slice(0, 8)}</span> : "—"), mono: true },
          { key: "finished", header: "Finished", render: (r) => formatTimestamp(r.finished_at) },
        ]}
      />
      <div>
        <Button
          variant="primary"
          size="sm"
          disabled={busy || !level.enabled || (level.dev_fixture === true && !devIngestFixture) || (tier === null && !managed) || (unavailableReason !== null && !devIngestFixture)}
          onClick={onRun}
          leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}
          title={
            tier === null && !managed
              ? "Managed service; no ingestion job"
              : level.dev_fixture
                ? "Unlocked only by the DEV ?mock=1 fixture — the backend has this level disabled"
                : unavailableReason ?? undefined
          }
        >
          {managed
            ? alreadyLoaded
              ? "Re-import corpus"
              : "Run managed import"
            : alreadyLoaded
              ? "Re-ingest corpus"
              : "Run ingestion"}
        </Button>
        {managed && (
          <p className="mt-1 text-xs text-ink-3">
            Uploads the corpus to Cloud Storage and imports it into the data store; parsing, chunking, embedding and indexing are done by Vertex AI Search (reported as skipped stages).
          </p>
        )}
        {level.dev_fixture && !devIngestFixture && (
          <p className="mt-1 text-xs text-clay" role="status">
            DEV fixture: this level is enabled only client-side; ingestion runs against the real backend and is disabled here.
          </p>
        )}
      </div>
    </Panel>
  );
};

export const IngestPage: React.FC = () => {
  const { levels, health } = useAppData();
  const { status, error, reload } = useIngestStatus();
  const job = useIngestJobStream();
  const jobLevel = levels.find((l) => l.level_id === job.level) ?? null;
  const unavailableReason = status?.available === false ? status.reason || "ingestion not available in this mode" : null;

  const onRun = (level: LevelDescriptor): void => {
    void job.start(level.level_id, reload);
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-ink-1">Ingestion</h1>
        <Button size="sm" variant="ghost" onClick={reload} leadingIcon={<RefreshCw className="h-3.5 w-3.5" aria-hidden />}>
          Refresh status
        </Button>
        {error && <Pill tone="err" title={error}>GET /api/v1/ingest/status failed</Pill>}
        {unavailableReason && <Pill tone="info" title={unavailableReason}>{unavailableReason}</Pill>}
        {status?.table && <Pill tone="neutral" mono title="BigQuery table (from /api/v1/ingest/status)">{status.table}</Pill>}
        {status?.last_runs_source && <Pill tone="neutral" title="Source of last_runs">{status.last_runs_source}</Pill>}
      </div>

      <UploadWorkspacePanel
        demoMode={health?.demo_mode ?? null}
        levels={levels}
        job={job}
        onJobStart={() => job.selectStage(null)}
        onJobFinished={reload}
      />

      {jobLevel && (
        <IngestJobView
          level={jobLevel}
          job={job}
          selectedStage={job.selectedStage}
          onSelectStage={(id) => job.selectStage(id)}
          onDismiss={job.dismiss}
        />
      )}

      <section className="flex flex-col gap-3 border-t border-line pt-4" aria-label="Pre-loaded benchmark corpus" data-testid="benchmark-corpus-section">
        <div className="flex flex-col gap-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-ink-1">Pre-loaded benchmark corpus (45 procurement PDFs)</h2>
            {levels.some((l) => (tierCountFor(l.level_id, status) ?? 0) > 0) && (
              <Pill tone="ok">loaded &amp; ready to query</Pill>
            )}
          </div>
          <p className="text-xs text-ink-2">
            Already ingested across Levels 1–5 for the default <span className="font-medium text-ink-1">Procurement corpus</span> scope — no action needed before running benchmark queries. Creating an upload workspace above starts completely empty (0 files, all levels <span className="font-mono">not ingested</span>) and never clears or overwrites this corpus.
          </p>
        </div>

      {status && Object.keys(status.index_states).length > 0 && (
        <Panel title="Index states (benchmark corpus table)" subtitle="Verbatim from INFORMATION_SCHEMA — shown as reported, never summarised">
          <ul className="flex flex-col gap-2">
            {Object.entries(status.index_states).map(([k, v]) => (
              <li key={k} className="flex flex-wrap items-start gap-2 text-xs">
                <span className="shrink-0 font-mono text-ink-1">{k}</span>
                <Pill tone={statusTone(v)}>{v.split(" (")[0]}</Pill>
                <span className="min-w-0 break-words text-ink-2">{v}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {levels.length === 0 ? (
        <EmptyState title="No levels" description="GET /api/v1/levels returned nothing; the ingest page needs level descriptors." />
      ) : (
        <ol className="grid gap-4 xl:grid-cols-2">
          {levels.map((level) => (
            <li key={level.level_id}>
              <LevelIngestCard
                level={level}
                count={tierCountFor(level.level_id, status)}
                lastRuns={(status?.last_runs ?? []).filter((r) => r.level === level.level_id)}
                busy={job.isStreaming}
                unavailableReason={unavailableReason}
                onRun={() => onRun(level)}
                storeState={isManagedLevel(level.level_id) ? managedStoreFor(status)?.state ?? null : null}
                storeReason={isManagedLevel(level.level_id) ? managedStoreFor(status)?.reason ?? null : null}
              />
            </li>
          ))}
        </ol>
      )}
      </section>
    </div>
  );
};
