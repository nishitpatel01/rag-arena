import React, { useState } from "react";
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
import { TIER_FOR_LEVEL, tierCountFor, useIngestStatus } from "./ingestStatus";
import { useIngestJobStream } from "./useIngestJobStream";

const LevelIngestCard: React.FC<{
  readonly level: LevelDescriptor;
  readonly count: number | null;
  readonly lastRuns: readonly IngestRunSummary[];
  readonly busy: boolean;
  readonly unavailableReason: string | null;
  readonly onRun: () => void;
}> = ({ level, count, lastRuns, busy, unavailableReason, onRun }) => {
  const tier = TIER_FOR_LEVEL[level.level_id];
  return (
    <Panel
      title={/^level\s*\d/i.test(level.label) ? level.label : `Level ${level.order} · ${level.label}`}
      subtitle={tier ? `tier_profile = ${tier}` : "managed service — no BigQuery tier"}
      actions={level.enabled ? <Pill tone="ok">enabled</Pill> : <Pill tone="locked">locked</Pill>}
      bodyClassName="flex flex-col gap-3"
    >
      <KeyValue items={[["chunks in BigQuery", count === null ? (tier === null ? "n/a (managed)" : "—") : count.toLocaleString()]]} />
      <DataTable
        caption={`Last ingestion runs for ${level.label}`}
        rows={lastRuns}
        rowKey={(r) => r.run_id}
        emptyText="No ingest_runs rows for this level."
        columns={[
          { key: "run", header: "run_id", render: (r) => <span title={r.run_id}>{r.run_id}</span>, mono: true },
          { key: "status", header: "Status", render: (r) => <Pill tone={statusTone(r.status)}>{r.status}</Pill> },
          { key: "docs", header: "Docs", render: (r) => r.docs ?? "—", align: "right" },
          { key: "pages", header: "Pages", render: (r) => r.pages ?? "—", align: "right" },
          { key: "chunks", header: "Chunks", render: (r) => r.chunks.toLocaleString(), align: "right" },
          { key: "layout", header: "Parser", render: (r) => r.layout_source ?? "—", mono: true },
          { key: "sha", header: "git", render: (r) => (r.git_sha && r.git_sha !== "unknown" ? <span title={r.git_sha}>{r.git_sha.slice(0, 8)}</span> : "—"), mono: true },
          { key: "finished", header: "Finished", render: (r) => formatTimestamp(r.finished_at) },
        ]}
      />
      <div>
        <Button
          variant="primary"
          size="sm"
          disabled={busy || !level.enabled || tier === null || unavailableReason !== null}
          onClick={onRun}
          leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}
          title={tier === null ? "Managed Vertex AI Search indexes from GCS; no ingestion job" : unavailableReason ?? undefined}
        >
          Run ingestion
        </Button>
      </div>
    </Panel>
  );
};

export const IngestPage: React.FC = () => {
  const { levels } = useAppData();
  const { status, error, reload } = useIngestStatus();
  const job = useIngestJobStream();
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const jobLevel = levels.find((l) => l.level_id === job.level) ?? null;
  const unavailableReason = status?.available === false ? status.reason || "ingestion not available in this mode" : null;

  const onRun = (level: LevelDescriptor): void => {
    setSelectedStage(null);
    void job.start(level.level_id).then(reload);
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

      {status && Object.keys(status.index_states).length > 0 && (
        <Panel title="Index states" subtitle="Verbatim from INFORMATION_SCHEMA — shown as reported, never summarised">
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
              />
            </li>
          ))}
        </ol>
      )}

      {jobLevel && <IngestJobView level={jobLevel} job={job} selectedStage={selectedStage} onSelectStage={setSelectedStage} />}
    </div>
  );
};
