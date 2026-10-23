import React from "react";
import { Link } from "react-router-dom";
import { Pill } from "../../components/ui/Pill";
import { formatClock } from "./runSource";
import type { LevelRun } from "./useLevelRun";

export interface CompareRunBannerProps {
  readonly run: LevelRun;
  readonly labelFor: (id: string | null) => string | null;
}

/** Shown on a level page while it displays a Compare run's part for this level (values already masked). */
export const CompareRunBanner: React.FC<CompareRunBannerProps> = ({ run, labelFor }) => {
  if (run.source !== "compare" || run.startedAt === null) return null;
  const synth = labelFor(run.state.synthesisModel ?? run.params?.synthesisModel ?? null);
  const judge = labelFor(run.state.judgeModel ?? run.params?.judgeModel ?? null);
  const questionId = run.state.questionId ?? run.params?.questionId ?? null;
  const n = run.levelCount;
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-line bg-surface-1 px-4 py-2" role="status" data-testid="compare-run-banner">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-ink-1">
          Showing this level from the Compare run started {formatClock(run.startedAt)} — {n} {n === 1 ? "level" : "levels"}
        </span>
        <Link to="/compare" className="text-xs font-medium text-moss underline">Open Compare</Link>
        {run.isStreaming && <Pill tone="active">this level streaming</Pill>}
        <Pill tone="neutral" mono title="Models from run_started (what the Compare run executed)">
          synthesis: {synth ?? "—"} · judge: {judge ?? "—"}
        </Pill>
        <Pill tone="neutral" mono title="question_id of the Compare run">{questionId ?? "ad-hoc (no question_id)"}</Pill>
      </div>
      {run.params?.query && (
        <p className="text-xs text-ink-2" data-testid="compare-run-question">
          <span className="font-medium">Question:</span> {run.params.query}
        </p>
      )}
    </div>
  );
};
