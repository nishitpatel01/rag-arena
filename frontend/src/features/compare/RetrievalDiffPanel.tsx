import React, { useEffect, useMemo, useState } from "react";
import { FileText } from "lucide-react";
import { Pill, type PillTone } from "../../components/ui/Pill";
import type { Citation, EngineTimeline } from "../../types/arena";
import {
  buildRetrievalDiff,
  defaultAlignmentFor,
  type DiffAlignMode,
  type DiffEngineColumn,
  type RankDeltaKind,
  type RetrievalDiffRow,
  type SlotRankCell,
} from "./retrievalDiffModel";

export interface RetrievalDiffPanelProps {
  readonly columns: readonly DiffEngineColumn[];
  readonly timelines: Readonly<Record<string, EngineTimeline>>;
  readonly onSelectCitation: (engine: string, citation: Citation) => void;
}

const DELTA_TONE: Readonly<Record<RankDeltaKind, PillTone>> = {
  promoted: "ok",
  demoted: "warn",
  added: "info",
  dropped: "err",
  same: "neutral",
  absent: "neutral",
};

const selectClass = "rounded-md border border-line bg-surface-0 px-2 py-1 text-xs text-ink-1";

function firstCitationTarget(
  row: RetrievalDiffRow,
  columns: readonly DiffEngineColumn[],
): { readonly engineId: string; readonly citation: Citation } | null {
  for (const col of columns) {
    const ch = row.cells[col.engineId]?.chunk;
    if (ch) return { engineId: col.engineId, citation: ch.citation };
  }
  return null;
}

const RankCellView: React.FC<{ readonly cell: SlotRankCell }> = ({ cell }) => {
  if (cell.kind === "absent") {
    return <span className="font-mono text-xs text-ink-3">—</span>;
  }
  return (
    <div className="flex flex-col items-start gap-0.5">
      <Pill tone={DELTA_TONE[cell.kind]} mono icon={false}>
        {cell.badgeText}
      </Pill>
      {cell.score !== null && (
        <span className="font-mono text-[11px] text-ink-3">score {cell.score.toFixed(3)}</span>
      )}
    </div>
  );
};

/**
 * Side-by-side Retrieval Diff Inspector for both Preset Ladder and Ablation Lab runs:
 * shows exact rank shifts (`↑ +k`, `↓ -k`, `NEW`, `DROPPED`, `SAME`) relative to any chosen
 * baseline column, aligned either by exact `chunk_id` or normalized `Source Doc + Page`.
 */
export const RetrievalDiffPanel: React.FC<RetrievalDiffPanelProps> = ({
  columns,
  timelines,
  onSelectCitation,
}) => {
  const autoAlign = useMemo(() => defaultAlignmentFor(columns, timelines), [columns, timelines]);
  const [userAlign, setUserAlign] = useState<DiffAlignMode | null>(null);
  const [baselineId, setBaselineId] = useState<string>(columns[0]?.engineId ?? "");

  // Reset baseline if the run's columns changed and no longer include `baselineId`.
  useEffect(() => {
    if (columns.length > 0 && !columns.some((c) => c.engineId === baselineId)) {
      setBaselineId(columns[0].engineId);
    }
  }, [baselineId, columns]);

  const alignBy = userAlign ?? autoAlign;
  const diff = useMemo(
    () => buildRetrievalDiff(columns, timelines, baselineId, alignBy),
    [alignBy, baselineId, columns, timelines],
  );

  if (columns.length === 0) return null;

  return (
    <div className="flex flex-col gap-3" data-testid="retrieval-diff-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2.5">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Pill tone="ok" mono>
            shared across all: {diff.sharedAcrossAllCount}
          </Pill>
          <Pill tone="neutral" mono>
            total distinct {alignBy === "doc_page" ? "doc pages" : "chunks"}: {diff.uniqueKeysTotal}
          </Pill>
          {columns.map((col) => {
            const st = diff.perEngineCounts[col.engineId];
            return (
              <Pill key={col.engineId} tone="neutral" mono>
                {col.short}: {st?.total ?? 0} ({st?.uniqueOnlyHere ?? 0} unique)
              </Pill>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="inline-flex items-center gap-1.5 text-ink-2">
            <span>Baseline:</span>
            <select
              aria-label="Diff baseline slot"
              value={diff.baselineEngineId}
              onChange={(e) => setBaselineId(e.target.value)}
              className={selectClass}
            >
              {columns.map((c) => (
                <option key={c.engineId} value={c.engineId}>
                  {c.short} · {c.label}
                </option>
              ))}
            </select>
          </label>

          <div className="inline-flex rounded-md border border-line bg-surface-0 p-0.5" role="group" aria-label="Chunk alignment mode">
            <button
              type="button"
              aria-pressed={alignBy === "chunk_id"}
              onClick={() => setUserAlign("chunk_id")}
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                alignBy === "chunk_id" ? "bg-surface-2 text-ink-1" : "text-ink-3 hover:text-ink-1"
              }`}
            >
              Exact chunk_id
            </button>
            <button
              type="button"
              aria-pressed={alignBy === "doc_page"}
              onClick={() => setUserAlign("doc_page")}
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                alignBy === "doc_page" ? "bg-surface-2 text-ink-1" : "text-ink-3 hover:text-ink-1"
              }`}
            >
              Source Doc + Page
            </button>
          </div>
        </div>
      </div>

      {diff.rows.length === 0 ? (
        <p className="text-xs text-ink-3">
          Waiting for <code className="font-mono">answer_complete</code> events to populate retrieved chunks…
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[44rem] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-line text-ink-3">
                <th className="py-2 pr-3 font-medium">Retrieved source chunk / page</th>
                {columns.map((col) => (
                  <th key={col.engineId} className="py-2 px-2 font-medium">
                    <span className="text-ink-1">{col.short}</span>{" "}
                    <span className="font-normal text-ink-3">· {col.label}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {diff.rows.map((row) => {
                const target = firstCitationTarget(row, columns);
                return (
                  <tr key={row.key} className="align-top hover:bg-surface-1" data-diff-key={row.key}>
                    <td className="max-w-[26rem] py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {target ? (
                          <button
                            type="button"
                            onClick={() => onSelectCitation(target.engineId, target.citation)}
                            className="inline-flex items-center gap-1 font-mono font-medium text-moss underline"
                          >
                            <FileText className="h-3 w-3 shrink-0" aria-hidden />
                            {row.docBasename}
                            {row.pageNumber !== null ? ` · p.${row.pageNumber}` : ""}
                          </button>
                        ) : (
                          <span className="font-mono font-medium text-ink-1">
                            {row.docBasename}
                            {row.pageNumber !== null ? ` · p.${row.pageNumber}` : ""}
                          </span>
                        )}
                        {row.sectionId && <Pill tone="neutral" mono icon={false}>§{row.sectionId}</Pill>}
                        {row.elementType && row.elementType !== "prose" && (
                          <Pill tone="info" mono icon={false}>{row.elementType}</Pill>
                        )}
                        {row.amendmentNumber !== null && (
                          <Pill tone="warn" mono icon={false}>Amend #{row.amendmentNumber}</Pill>
                        )}
                      </div>
                      <p className="mt-0.5 font-mono text-[10px] text-ink-3">{row.chunkId}</p>
                      {row.excerpt && <p className="mt-1 line-clamp-2 text-ink-2">{row.excerpt}</p>}
                    </td>
                    {columns.map((col) => (
                      <td key={col.engineId} className="py-2 px-2">
                        <RankCellView cell={row.cells[col.engineId]} />
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
