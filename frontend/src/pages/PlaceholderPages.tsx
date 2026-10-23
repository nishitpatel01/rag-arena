import React, { useEffect, useState } from "react";
import { DataTable, type DataTableColumn } from "../components/ui/DataTable";
import { EmptyState } from "../components/ui/EmptyState";
import { formatValue } from "../components/ui/KeyValue";
import { useSafeMode } from "../app/SafeMode";
import { api } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { LeaderboardResponse, LeaderboardRow } from "../types/arena";
import { groupLeaderboard, latestRecordedAt, type LeaderboardView } from "./leaderboardModel";

const LEADERBOARD_PLACEHOLDER =
  "The leaderboard aggregates query_runs rows (live) or recordings of real runs (replay), each with provenance. It is populated in Phase 6; hand-typed metrics are not shown.";

function leaderboardColumns(metricKeys: readonly string[]): DataTableColumn<LeaderboardRow>[] {
  return [
    { key: "engine", header: "engine", render: (r) => r.engine, mono: true },
    { key: "runs", header: "runs", render: (r) => formatValue(r.runs), align: "right" },
    ...metricKeys.map<DataTableColumn<LeaderboardRow>>((k) => ({
      key: k,
      header: k.replace(/^avg_/, "avg "),
      render: (r) => formatValue(r[k]),
      align: "right",
      mono: true,
    })),
    { key: "recorded", header: "latest recorded_at", render: (r) => formatTimestamp(latestRecordedAt(r)) },
  ];
}

/** One table per (synthesis, judge) pair; rows are shown as aggregated by the server (no cross-pair averages). */
const PairGroupedLeaderboard: React.FC<{ readonly view: LeaderboardView; readonly source?: string }> = ({ view, source }) => {
  const columns = leaderboardColumns(view.metricKeys);
  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-ink-3">
        Grouped by model pair; scores are never averaged across pairs.{source ? ` Source: ${source}.` : ""}
      </p>
      {view.groups.map((g) => (
        <section key={g.key} className="flex flex-col gap-1.5" aria-label={`synthesis ${g.synthesisModel}, judge ${g.judgeModel}`}>
          <h2 className="text-sm font-semibold text-ink-1">
            synthesis <span className="font-mono">{g.synthesisModel}</span> · judge <span className="font-mono">{g.judgeModel}</span>
          </h2>
          <DataTable caption={`Leaderboard for synthesis ${g.synthesisModel} and judge ${g.judgeModel}`} columns={columns} rows={g.rows}
            rowKey={(r, i) => `${r.engine}-${i}`} />
        </section>
      ))}
    </div>
  );
};

export const LeaderboardPage: React.FC = () => {
  const [data, setData] = useState<LeaderboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.leaderboard().then(
      (res) => !cancelled && setData(res),
      (err: unknown) => !cancelled && setError(err instanceof Error ? err.message : String(err))
    );
    return () => {
      cancelled = true;
    };
  }, []);
  const { mask, maskValue } = useSafeMode();
  const view = groupLeaderboard(maskValue(data));
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-lg font-semibold text-ink-1">Leaderboard</h1>
      {view.grouped ? (
        <PairGroupedLeaderboard view={view} source={data?.source} />
      ) : (
        <EmptyState title="No measured runs yet" description={LEADERBOARD_PLACEHOLDER} />
      )}
      {!view.grouped && view.totalRows > 0 && (
        <p className="text-xs text-ink-3">
          {view.totalRows} aggregated row(s) were returned without synthesis/judge model ids; they are not shown because scores from
          different model pairs must not be mixed.
        </p>
      )}
      {error && <p className="text-xs text-clay" role="status">GET /api/v1/leaderboard: {mask(error)}</p>}
    </div>
  );
};
