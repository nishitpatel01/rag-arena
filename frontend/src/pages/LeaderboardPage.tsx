import React, { useEffect, useState } from "react";
import { useAppData } from "../app/AppDataProvider";
import { useSafeMode } from "../app/SafeMode";
import { EmptyState } from "../components/ui/EmptyState";
import { HighlightMetricSelect, useHighlightMetric } from "../components/ui/HighlightMetricSelect";
import { Pill } from "../components/ui/Pill";
import { SegmentedControl } from "../components/ui/SegmentedControl";
import { api } from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import type { BenchmarkQuestion, LeaderboardResponse, LeaderboardScope, LeaderboardSource } from "../types/arena";
import { LeaderboardTable } from "./LeaderboardTable";
import {
  BATTLE_SCORE_FOOTNOTE,
  defaultSource,
  effectiveScope,
  groupLeaderboard,
  scopeOptions,
  sourceOptions,
} from "./leaderboardModel";

const selectClass = "rounded-md border border-line bg-surface-0 px-2 py-1 text-xs text-ink-1 disabled:opacity-60";

type Load = { readonly status: "loading" } | { readonly status: "error"; readonly message: string } | { readonly status: "ok"; readonly data: LeaderboardResponse };

function useBenchmarkQuestions(): readonly BenchmarkQuestion[] {
  const [questions, setQuestions] = useState<readonly BenchmarkQuestion[]>([]);
  useEffect(() => {
    let cancelled = false;
    api.benchmarks().then((res) => !cancelled && setQuestions(res.questions), () => !cancelled && setQuestions([]));
    return () => {
      cancelled = true;
    };
  }, []);
  return questions;
}

function useLeaderboardData(
  source: LeaderboardSource | null,
  scope: LeaderboardScope,
  questionId: string,
  engine: string
): Load {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  useEffect(() => {
    if (source === null) return undefined;
    let cancelled = false;
    setLoad({ status: "loading" });
    api.leaderboard({ source, scope, questionId: scope === "uploads" ? "" : questionId, engine }).then(
      (data) => !cancelled && setLoad({ status: "ok", data }),
      (err: unknown) => !cancelled && setLoad({ status: "error", message: err instanceof Error ? err.message : String(err) })
    );
    return () => {
      cancelled = true;
    };
  }, [source, scope, questionId, engine]);
  return load;
}

export const LeaderboardPage: React.FC = () => {
  const { health, levels } = useAppData();
  const demoMode = health?.demo_mode ?? null;
  const [picked, setPicked] = useState<LeaderboardSource | null>(null);
  const [pickedScope, setPickedScope] = useState<LeaderboardScope>("golden");
  const [questionId, setQuestionId] = useState("");
  const [engine, setEngine] = useState("");
  const [highlightMetric, setHighlightMetric] = useHighlightMetric();
  const source = picked ?? (health ? defaultSource(demoMode) : null);
  const options = sourceOptions(demoMode);
  const sOptions = scopeOptions(demoMode, source ?? "recordings");
  const scope = effectiveScope(pickedScope, demoMode, source ?? "recordings");
  const disabledReason = options.find((o) => o.disabled)?.reason ?? null;
  const questions = useBenchmarkQuestions();
  const load = useLeaderboardData(source, scope, questionId, engine);
  const { mask, maskValue } = useSafeMode();
  const data = load.status === "ok" ? maskValue(load.data) : null;
  const groups = groupLeaderboard(data);

  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-lg font-semibold text-ink-1">Leaderboard</h1>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink-2">Source</span>
          <SegmentedControl<LeaderboardSource> label="Leaderboard source" value={source ?? "recordings"}
            options={options.map((o) => ({ value: o.value, label: o.label, disabled: o.disabled, title: o.reason ?? undefined }))}
            onChange={setPicked} />
        </div>
        {demoMode !== "replay" && (
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-ink-2">Scope</span>
            <SegmentedControl<LeaderboardScope>
              label="Leaderboard scope"
              value={scope}
              options={sOptions.map((o) => ({ value: o.value, label: o.label, disabled: o.disabled, title: o.reason ?? undefined }))}
              onChange={setPickedScope}
            />
          </div>
        )}
        <HighlightMetricSelect value={highlightMetric} onChange={setHighlightMetric} id="leaderboard-highlight-metric" />
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-2">
          Question
          <select
            className={selectClass}
            value={scope === "uploads" ? "" : questionId}
            disabled={scope === "uploads"}
            title={scope === "uploads" ? "Uploaded document runs use ad-hoc questions without golden question_ids" : undefined}
            onChange={(e) => setQuestionId(e.target.value)}
          >
            <option value="">{scope === "uploads" ? "Ad-hoc upload questions" : "All questions"}</option>
            {scope !== "uploads" && questions.map((q) => <option key={q.question_id} value={q.question_id}>{q.question_id}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-2">
          Level
          <select className={selectClass} value={engine} onChange={(e) => setEngine(e.target.value)}>
            <option value="">All levels</option>
            {levels.map((l) => <option key={l.level_id} value={l.level_id}>{l.label}</option>)}
          </select>
        </label>
      </div>
      {disabledReason && <p className="text-xs text-ink-3">Live runs unavailable: {disabledReason}</p>}
      {scope === "uploads" && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
          <Pill tone="warn">no reference answer</Pill>
          <span>Uploaded document runs are judged without golden reference answers and never averaged with the Golden set.</span>
        </div>
      )}
      {load.status === "loading" && <p className="text-xs text-ink-3" role="status">Loading leaderboard…</p>}
      {load.status === "error" && (
        <p className="text-xs text-clay" role="alert">GET /api/v1/leaderboard: {mask(load.message)}</p>
      )}
      {data && (
        <p className="text-xs text-ink-3">
          Source: <span className="font-mono">{data.source}</span> · scope: <span className="font-mono">{scope}</span> · generated {formatTimestamp(data.generated_at ?? null)}
          {data.stats?.rows_read !== undefined ? ` · ${data.stats.rows_read} run(s) read` : ""}
          {data.stats?.bytes_processed != null ? ` · ${formatBytes(data.stats.bytes_processed)} processed` : ""}
          {" "}· grouped by model pair; scores are never averaged across pairs or judges.
        </p>
      )}
      {data?.notes?.map((n) => <p key={n} className="text-xs text-ink-3">{n}</p>)}
      {data && groups.length === 0 && (
        <EmptyState title="No measured runs match" description="No runs were found for this source and these filters. Hand-typed metrics are never shown." />
      )}
      {groups.map((g, i) => <LeaderboardTable key={g.key} group={g} index={i} highlightMetric={highlightMetric} />)}
      <p className="border-t border-line pt-2 text-[11px] text-ink-3">{BATTLE_SCORE_FOOTNOTE}</p>
    </div>
  );
};
