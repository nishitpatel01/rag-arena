import React, { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { useMasked, useSafeMode } from "../../app/SafeMode";
import { DataTable } from "../../components/ui/DataTable";
import { Pill } from "../../components/ui/Pill";
import { api } from "../../lib/api";
import { formatMs } from "../../lib/format";
import type { EngineTimeline, LeaderboardResponse, ModelsResponse } from "../../types/arena";
import { baselinePair, baselinesFor, teaserMatrix, teaserTitle, type BaselineEntry, type LowerLevel } from "./compareTeaserModel";

export interface CompareTeaserProps {
  readonly timeline: EngineTimeline | undefined;
  readonly catalog: ModelsResponse | null;
  /** From `run_started` — the pair and question this run actually executed. */
  readonly synthesisModel: string | null;
  readonly judgeModel: string | null;
  readonly questionId: string | null;
  readonly labelFor: (id: string | null) => string | null;
  /** Every enabled level below the current one (ascending). */
  readonly lowerLevels: readonly LowerLevel[];
}

const fmt = (unit: "score" | "ms", v: number | null): string => (v === null ? "—" : unit === "ms" ? formatMs(v) : v.toFixed(2));

const RecordingPill: React.FC<{ readonly entry: BaselineEntry; readonly questionId: string | null }> = ({ entry, questionId }) => {
  const { view, level } = entry;
  if (view.questionRecorded) return <Pill tone="ok">{level.short} recording exists for {questionId}</Pill>;
  return <Pill tone="locked">{questionId ? `no ${level.short} recording of ${questionId} for this pair` : `ad-hoc question — no ${level.short} recording`}</Pill>;
};

/**
 * "Same question on lower levels" (read-only teaser; the full Compare page is Phase 6). Never starts a run:
 * for every lower enabled level it shows whether the same question + pair is recorded
 * (GET /models.recorded_pairs) and that level's leaderboard averages for the pair (GET /leaderboard).
 */
export const CompareTeaser: React.FC<CompareTeaserProps> = ({ timeline, catalog, synthesisModel, judgeModel, questionId, labelFor, lowerLevels }) => {
  const [leaderboard, setLeaderboard] = useState<LeaderboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { mask } = useSafeMode();
  const shownLeaderboard = useMasked(leaderboard);

  useEffect(() => {
    let cancelled = false;
    api.leaderboard({ source: "recordings" }).then(
      (res) => !cancelled && setLeaderboard(res),
      (err: unknown) => !cancelled && setError(err instanceof Error ? err.message : String(err))
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // Level 5 managed summary: lower levels cannot run that pseudo model → compare against the default synthesis model + same judge.
  const pair = baselinePair(catalog, synthesisModel, judgeModel);
  const entries = baselinesFor(catalog, shownLeaderboard, lowerLevels, { synthesisModel: pair.synthesisModel, judgeModel: pair.judgeModel, questionId });
  const rows = teaserMatrix(entries, {
    groundedness: timeline?.evaluation?.metrics.groundedness ?? null,
    qaRelevance: timeline?.evaluation?.metrics.qa_relevance ?? null,
    contextPrecision: timeline?.evaluation?.metrics.context_precision ?? null,
    latencyMs: timeline?.answer?.latencyMs ?? null,
  });
  const pairKnown = Boolean(synthesisModel && judgeModel);
  const withRow = entries.filter((e) => e.view.row !== null);
  const missing = entries.filter((e) => e.view.row === null);
  const pairText = `synthesis ${labelFor(synthesisModel) ?? "—"} · judge ${labelFor(judgeModel) ?? "—"}`;
  const title = teaserTitle(lowerLevels);

  return (
    <section aria-label={title} className="flex flex-col gap-2" data-testid="compare-teaser">
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone="neutral" mono>{pairText}</Pill>
        {pair.substituted && (
          <Pill tone="warn" mono title="The managed summary exists only on Level 5; lower-level baselines use the server default synthesis model with the same judge.">
            baselines: synthesis {labelFor(pair.synthesisModel) ?? "—"} · judge {labelFor(judgeModel) ?? "—"}
          </Pill>
        )}
        {!pairKnown ? (
          <Pill tone="warn">model pair not reported by run_started</Pill>
        ) : (
          entries.map((e) => <RecordingPill key={e.level.engine} entry={e} questionId={questionId} />)
        )}
      </div>
      {error && (
        <p className="text-xs text-err" role="alert">
          GET /api/v1/leaderboard failed: {mask(error)}
        </p>
      )}
      {!error && !leaderboard && <p className="text-xs text-ink-3">Loading leaderboard…</p>}
      {leaderboard && pairKnown && missing.length > 0 && (
        <p className="text-xs text-ink-3">
          The leaderboard has no row for this model pair on {missing.map((e) => e.level.short).join(", ")}.
        </p>
      )}
      {withRow.length > 0 && (
        <>
          <DataTable
            caption={`This run versus ${withRow.map((e) => e.level.short).join(" and ")} averages for the same model pair`}
            rows={rows}
            rowKey={(m) => m.key}
            columns={[
              { key: "metric", header: "Metric", render: (m) => m.label },
              { key: "current", header: "This run", render: (m) => fmt(m.unit, m.current), align: "right", mono: true },
              ...entries.flatMap((e, i) =>
                e.view.row === null
                  ? []
                  : [
                      {
                        key: `baseline-${e.level.engine}`,
                        header: `${e.level.short.replace(/^Level /, "L")} avg · ${e.view.runs ?? "?"}`,
                        render: (m: (typeof rows)[number]) => fmt(m.unit, m.baselines[i]),
                        align: "right" as const,
                        mono: true,
                      },
                    ]
              ),
            ]}
          />
          <p className="text-xs text-ink-3">
            "Ln avg · N" = server-side average over all N recorded runs of {pair.substituted ? "the baseline pair" : "this pair"} on that level (not only this question).
            {pair.substituted ? " The Vertex AI Search summary has no equivalent on Levels 1–4, so their columns use the default synthesis model with the same judge." : ""}
            {shownLeaderboard?.source ? ` Source: ${shownLeaderboard.source}.` : ""}
          </p>
        </>
      )}
      <div className="flex flex-wrap gap-3">
        {lowerLevels.map((l) => (
          <Link key={l.engine} to={`/levels/${l.engine}`} className="inline-flex items-center gap-1 text-xs font-medium text-moss hover:underline">
            Open {l.short} <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        ))}
      </div>
    </section>
  );
};
