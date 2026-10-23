import React from "react";
import { DataTable, type DataTableColumn } from "../../components/ui/DataTable";
import { Pill } from "../../components/ui/Pill";
import { formatMs, formatUsd } from "../../lib/format";
import type { ScoreRow } from "./compareModel";

const score2 = (v: number | null): string => (v === null ? "—" : v.toFixed(2));

const StatusCell: React.FC<{ readonly row: ScoreRow }> = ({ row }) => {
  if (row.status === "failed" && row.error) {
    return (
      <div className="flex max-w-[32rem] flex-col gap-0.5" role="alert">
        <span className="font-mono font-semibold text-err">
          failed · {row.error.type}{row.error.stage ? ` @ ${row.error.stage}` : ""} — not ranked
        </span>
        <span className="break-words text-ink-1">{row.error.message}</span>
      </div>
    );
  }
  if (row.status === "failed") return <span className="text-err">failed — not ranked</span>;
  if (row.status === "unscored") return <span className="text-clay">eval_complete carried no battle_score — not ranked</span>;
  if (row.status === "scored") return <span className="text-ok">scored</span>;
  return <span className="text-ink-3">{row.status === "running" ? "running…" : "waiting for events"}</span>;
};

function columns(labelFor: (id: string | null) => string | null): DataTableColumn<ScoreRow>[] {
  return [
    { key: "rank", header: "rank", render: (r) => (r.rank === null ? "—" : r.rank === 1 ? <Pill tone="ok" mono>#1 best</Pill> : `#${r.rank}`), mono: true },
    { key: "level", header: "level", render: (r) => <span><span className="font-semibold">{r.short}</span> · {r.label}</span> },
    { key: "score", header: "Battle Score", render: (r) => (r.battleScore === null ? "—" : <span className="font-semibold">{r.battleScore.toFixed(1)}</span>), align: "right", mono: true },
    { key: "g", header: "groundedness", render: (r) => score2(r.groundedness), align: "right", mono: true },
    { key: "qa", header: "QA relevance", render: (r) => score2(r.qaRelevance), align: "right", mono: true },
    { key: "cp", header: "context precision", render: (r) => score2(r.contextPrecision), align: "right", mono: true },
    { key: "eff", header: "efficiency", render: (r) => score2(r.breakdown?.efficiency ?? null), align: "right", mono: true },
    { key: "lat", header: "latency", render: (r) => formatMs(r.latencyMs), align: "right", mono: true },
    { key: "cost", header: "run cost", render: (r) => formatUsd(r.costUsd), align: "right", mono: true },
    { key: "judge", header: "judge", render: (r) => (r.judgeModel ? labelFor(r.judgeModel) ?? r.judgeModel : "—"), mono: true },
    { key: "status", header: "status", render: (r) => <StatusCell row={r} /> },
  ];
}

export interface ScoreboardProps {
  readonly rows: readonly ScoreRow[];
  readonly labelFor: (id: string | null) => string | null;
}

/** One row per included level; Battle Score comes only from eval_complete (server formula, scoring.battle_score). */
export const Scoreboard: React.FC<ScoreboardProps> = ({ rows, labelFor }) => (
  <div className="flex flex-col gap-2">
    <DataTable caption="Compare scoreboard: one row per level" columns={columns(labelFor)} rows={rows} rowKey={(r) => r.engine} emptyText="No levels in this run." />
    <p className="text-xs text-ink-3">
      Battle Score (0–100) = 40% QA relevance + 35% groundedness + 15% context precision + 10% efficiency (latency vs 5 s, run cost vs $0.01),
      computed by the server from this run's judge metrics, latency and run cost (engine + judge). Failed levels are never scored or ranked.
    </p>
  </div>
);
