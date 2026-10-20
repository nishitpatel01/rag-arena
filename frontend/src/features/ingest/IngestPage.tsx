import React, { useState } from "react";
import { Play, RefreshCw } from "lucide-react";
import { useAppData } from "../../app/AppDataProvider";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { KeyValue } from "../../components/ui/KeyValue";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { formatTimestamp } from "../../lib/format";
import type { LevelDescriptor } from "../../types/arena";
import { IngestJobView } from "./IngestJobView";
import { TIER_FOR_LEVEL, tierCountFor, useIngestStatus } from "./ingestStatus";
import { useIngestJobStream } from "./useIngestJobStream";

const LevelIngestCard: React.FC<{
  readonly level: LevelDescriptor;
  readonly count: number | null;
  readonly lastRuns: readonly { run_id: string; tier_profile: string; finished_at: string | null; status: string; chunks: number }[];
  readonly busy: boolean;
  readonly unavailableReason: string | null;
  readonly onRun: () => void;
}> = ({ level, count, lastRuns, busy, unavailableReason, onRun }) => {
  const tier = TIER_FOR_LEVEL[level.level_id];
  return (
    <Panel
      title={`Level ${level.order} · ${level.label}`}
      subtitle={tier ? `tier_profile = ${tier}` : "managed service — no BigQuery tier"}
      actions={level.enabled ? <Pill tone="ok">enabled</Pill> : <Pill tone="locked">locked</Pill>}
      bodyClassName="flex flex-col gap-3"
    >
      <KeyValue items={[["chunks in BigQuery", count === null ? "—" : count.toLocaleString()]]} />
      <DataTable
        caption={`Last ingestion runs for ${level.label}`}
        rows={lastRuns}
        rowKey={(r) => r.run_id}
        emptyText="No ingest_runs rows for this level."
        columns={[
          { key: "run", header: "run_id", render: (r) => r.run_id.slice(0, 12), mono: true },
          { key: "status", header: "Status", render: (r) => <Pill tone={r.status === "succeeded" || r.status === "complete" ? "ok" : r.status === "failed" ? "err" : "neutral"}>{r.status}</Pill> },
          { key: "chunks", header: "Chunks", render: (r) => r.chunks, align: "right" },
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
        {status && Object.entries(status.index_states).map(([k, v]) => (
          <Pill key={k} tone={v.toLowerCase() === "active" ? "ok" : "info"} mono title="index state">
            {k}: {v}
          </Pill>
        ))}
      </div>

      {levels.length === 0 ? (
        <EmptyState title="No levels" description="GET /api/v1/levels returned nothing; the ingest page needs level descriptors." />
      ) : (
        <ol className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
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
