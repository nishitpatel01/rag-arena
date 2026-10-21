import React, { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { useMasked, useSafeMode } from "../../app/SafeMode";
import { DataTable } from "../../components/ui/DataTable";
import { Pill } from "../../components/ui/Pill";
import { api } from "../../lib/api";
import { formatMs } from "../../lib/format";
import type { EngineTimeline, LeaderboardResponse, ModelsResponse } from "../../types/arena";
import { BASELINE_ENGINE, baselineFor, teaserMetrics, type TeaserMetric } from "./compareTeaserModel";

export interface CompareTeaserProps {
  readonly timeline: EngineTimeline | undefined;
  readonly catalog: ModelsResponse | null;
  /** From `run_started` — the pair and question this run actually executed. */
  readonly synthesisModel: string | null;
  readonly judgeModel: string | null;
  readonly questionId: string | null;
  readonly labelFor: (id: string | null) => string | null;
}

const fmt = (m: TeaserMetric, v: number | null): string => (v === null ? "—" : m.unit === "ms" ? formatMs(v) : v.toFixed(2));

/**
 * "Same question on Level 1" (read-only teaser; the full Compare page is Phase 6). Never starts a run:
 * shows whether the same question + pair is recorded on Level 1 (GET /models.recorded_pairs) and the
 * Level 1 leaderboard averages for that pair (GET /leaderboard), next to this run's judge metrics.
 */
export const CompareTeaser: React.FC<CompareTeaserProps> = ({ timeline, catalog, synthesisModel, judgeModel, questionId, labelFor }) => {
  const [leaderboard, setLeaderboard] = useState<LeaderboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { mask } = useSafeMode();
  const shownLeaderboard = useMasked(leaderboard);

  useEffect(() => {
    let cancelled = false;
    api.leaderboard().then(
      (res) => !cancelled && setLeaderboard(res),
      (err: unknown) => !cancelled && setError(err instanceof Error ? err.message : String(err))
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const view = baselineFor(catalog, shownLeaderboard, { engine: BASELINE_ENGINE, synthesisModel, judgeModel, questionId });
  const metrics = teaserMetrics(view, {
    groundedness: timeline?.evaluation?.metrics.groundedness ?? null,
    qaRelevance: timeline?.evaluation?.metrics.qa_relevance ?? null,
    contextPrecision: timeline?.evaluation?.metrics.context_precision ?? null,
    latencyMs: timeline?.answer?.latencyMs ?? null,
  });
  const pairText = `synthesis ${labelFor(synthesisModel) ?? "—"} · judge ${labelFor(judgeModel) ?? "—"}`;

  return (
    <section aria-label="Same question on Level 1" className="flex flex-col gap-2" data-testid="compare-teaser">
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone="neutral" mono>{pairText}</Pill>
        {!view.pairKnown ? (
          <Pill tone="warn">model pair not reported by run_started</Pill>
        ) : view.questionRecorded ? (
          <Pill tone="ok">Level 1 recording exists for {questionId}</Pill>
        ) : (
          <Pill tone="locked">{questionId ? `no Level 1 recording of ${questionId} for this pair` : "ad-hoc question — no Level 1 recording"}</Pill>
        )}
      </div>
      {error && (
        <p className="text-xs text-err" role="alert">
          GET /api/v1/leaderboard failed: {mask(error)}
        </p>
      )}
      {!error && !leaderboard && <p className="text-xs text-ink-3">Loading Level 1 leaderboard…</p>}
      {leaderboard && !view.row && view.pairKnown && (
        <p className="text-xs text-ink-3">The leaderboard has no Level 1 row for this model pair.</p>
      )}
      {view.row && (
        <>
          <DataTable
            caption="This run versus Level 1 averages for the same model pair"
            rows={metrics}
            rowKey={(m) => m.key}
            columns={[
              { key: "metric", header: "Metric", render: (m) => m.label },
              { key: "current", header: "This run", render: (m) => fmt(m, m.current), align: "right", mono: true },
              {
                key: "baseline",
                header: `Level 1 avg (${view.runs ?? "?"} runs)`,
                render: (m) => fmt(m, m.baseline),
                align: "right",
                mono: true,
              },
            ]}
          />
          <p className="text-xs text-ink-3">
            Level 1 column = server-side average over all recorded runs of this pair (not only this question).
            {shownLeaderboard?.source ? ` Source: ${shownLeaderboard.source}.` : ""}
          </p>
          {view.syntheticRuns > 0 && (
            <p className="text-xs text-clay" role="status">
              Caveat: the Level 1 row includes {view.syntheticRuns} provenance entr{view.syntheticRuns === 1 ? "y" : "ies"} flagged synthetic_dev_fixture.
            </p>
          )}
        </>
      )}
      <Link to={`/levels/${BASELINE_ENGINE}`} className="inline-flex items-center gap-1 text-xs font-medium text-moss hover:underline">
        Open Level 1 <ArrowRight className="h-3.5 w-3.5" aria-hidden />
      </Link>
    </section>
  );
};
