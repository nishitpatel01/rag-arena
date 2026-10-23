import React, { useState } from "react";
import { useSafeMode } from "../app/SafeMode";
import { formatMs, formatTimestamp, formatUsd } from "../lib/format";
import {
  formatBattleScore,
  formatMetric,
  provenanceDetails,
  provenanceSummary,
  type LeaderboardGroup,
  type RankedRow,
} from "./leaderboardModel";

const HEADERS: readonly { label: string; title?: string; right?: boolean }[] = [
  { label: "#", title: "Rank within this pair (scored rows only)", right: true },
  { label: "Level" },
  { label: "Battle Score", right: true },
  { label: "G", title: "Groundedness (avg, 0–1)", right: true },
  { label: "QA", title: "QA relevance (avg, 0–1)", right: true },
  { label: "CP", title: "Context precision (avg, 0–1)", right: true },
  { label: "Latency", title: "Average engine latency", right: true },
  { label: "Run cost", title: "Average run cost incl. the judge", right: true },
  { label: "Runs", title: "Scored runs", right: true },
  { label: "Questions", title: "Distinct questions covered", right: true },
  { label: "Failures", title: "Runs without judge metrics", right: true },
  { label: "Last run" },
];

const cell = "px-2 py-1 whitespace-nowrap";
const num = `${cell} text-right font-mono tabular-nums`;

const ProvenanceRow: React.FC<{ readonly item: RankedRow; readonly id: string }> = ({ item, id }) => {
  const [open, setOpen] = useState(false);
  const { mask } = useSafeMode();
  const details = provenanceDetails(item.row.provenance);
  return (
    <tr className="bg-surface-1 text-ink-3">
      <td colSpan={HEADERS.length} className="px-2 pb-1.5 pt-0">
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <span>Provenance: {mask(provenanceSummary(item.row.provenance))}</span>
          {details.length > 0 && (
            <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)}
              className="rounded px-1 text-slate underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-slate">
              {open ? "Hide details" : "Details"}
            </button>
          )}
        </div>
        {open && (
          <dl id={id} className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-[11px]">
            {details.map(([k, v]) => (
              <React.Fragment key={k}>
                <dt className="text-ink-3">{k}</dt>
                <dd className="break-all font-mono text-ink-2">{mask(v)}</dd>
              </React.Fragment>
            ))}
          </dl>
        )}
      </td>
    </tr>
  );
};

const RowCells: React.FC<{ readonly item: RankedRow }> = ({ item }) => {
  const r = item.row;
  return (
    <tr className="border-t border-line align-top text-ink-2">
      <td className={num}>{item.rank ?? "—"}</td>
      <td className={cell}>
        <span className="font-medium text-ink-1">{r.level_label ?? r.engine}</span>{" "}
        <span className="font-mono text-[11px] text-ink-3">{r.engine}</span>
      </td>
      <td className={`${num} font-semibold text-ink-1`}>{formatBattleScore(r.avg_battle_score)}</td>
      <td className={num}>{formatMetric(r.avg_groundedness)}</td>
      <td className={num}>{formatMetric(r.avg_qa_relevance)}</td>
      <td className={num}>{formatMetric(r.avg_context_precision)}</td>
      <td className={num}>{formatMs(r.avg_latency_ms ?? null)}</td>
      <td className={num}>{formatUsd(r.avg_cost_usd ?? null)}</td>
      <td className={num}>{r.runs ?? 0}</td>
      <td className={num}>{r.questions_covered ?? 0}</td>
      <td className={`${num} ${(r.failures ?? 0) > 0 ? "text-clay" : ""}`}>{r.failures ?? 0}</td>
      <td className={cell}>{formatTimestamp(r.last_run_at ?? null)}</td>
    </tr>
  );
};

/** One table per (synthesis, judge) pair; rows exactly as aggregated by the server. */
export const LeaderboardTable: React.FC<{ readonly group: LeaderboardGroup; readonly index: number }> = ({ group, index }) => {
  const { mask } = useSafeMode();
  const pairText = `synthesis ${group.synthesisModel}, judge ${group.judgeModel}`;
  return (
    <section className="flex flex-col gap-1.5" aria-label={mask(pairText)}>
      <h2 className="text-sm font-semibold text-ink-1">
        synthesis <span className="font-mono">{mask(group.synthesisModel)}</span> · judge{" "}
        <span className="font-mono">{mask(group.judgeModel)}</span>
      </h2>
      {group.note && <p className="text-xs text-ochre" role="note">{mask(group.note)}</p>}
      <div className="w-full overflow-x-auto rounded-md border border-line">
        <table className="w-full border-collapse text-xs">
          <caption className="sr-only">{mask(`Leaderboard for ${pairText}`)}</caption>
          <thead className="bg-surface-2 text-ink-3">
            <tr>
              {HEADERS.map((h) => (
                <th key={h.label} scope="col" title={h.title} className={`${cell} font-semibold ${h.right ? "text-right" : "text-left"}`}>
                  {h.label}
                </th>
              ))}
            </tr>
          </thead>
          {group.rows.map((item, i) => (
            <tbody key={`${item.row.engine}-${i}`}>
              <RowCells item={item} />
              <ProvenanceRow item={item} id={`lb-prov-${index}-${i}`} />
            </tbody>
          ))}
        </table>
      </div>
    </section>
  );
};
