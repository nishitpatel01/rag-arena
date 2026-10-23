import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useAppData } from "../../app/AppDataProvider";
import { useMasked, useSafeMode } from "../../app/SafeMode";
import { DocumentDrawer } from "../../components/DocumentDrawer";
import { Panel } from "../../components/ui/Panel";
import { api } from "../../lib/api";
import type { Citation, LeaderboardResponse } from "../../types/arena";
import { modelLabel, type ModelSelection } from "../levels/modelSelection";
import { useBattleStream } from "../levels/useBattleStream";
import { useModelCatalog } from "../levels/useModelCatalog";
import { CompareAnswers, type SelectedCitation } from "./CompareAnswers";
import { CompareRails } from "./CompareRails";
import { CompareSetup } from "./CompareSetup";
import type { QuestionValue } from "./CompareQuestionFields";
import { Scoreboard } from "./Scoreboard";
import { costEstimate, enginesToRun, levelChoices, scoreboardRows, summaryText } from "./compareModel";
import { RunStatusPills } from "./RunStatusPills";

function useLeaderboard(): { data: LeaderboardResponse | null; error: string | null } {
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
  return { data: useMasked(data), error };
}

/** `/compare`: one question, one model pair, the chosen enabled levels → ONE battle stream with every engine. */
export const ComparePage: React.FC = () => {
  const { levels, loading } = useAppData();
  const models = useModelCatalog(null);
  const stream = useBattleStream();
  const leaderboard = useLeaderboard();
  const { mask } = useSafeMode();
  const [question, setQuestion] = useState<QuestionValue>({ query: "", questionId: "" });
  // Default: every enabled level included — track only what the user unticked.
  const [unchecked, setUnchecked] = useState<ReadonlySet<string>>(new Set());
  const [runEngines, setRunEngines] = useState<readonly string[]>([]);
  const [selected, setSelected] = useState<SelectedCitation | null>(null);

  const choices = useMemo(() => levelChoices(levels, models.catalog, models.selection, question.questionId), [levels, models.catalog, models.selection, question.questionId]);
  const checked = useMemo(() => new Set(choices.map((c) => c.levelId).filter((id) => !unchecked.has(id))), [choices, unchecked]);
  const engines = enginesToRun(choices, checked);
  const estimate = costEstimate(leaderboard.data, engines, models.selection);
  const labelFor = useCallback((id: string | null) => modelLabel(models.catalog, id), [models.catalog]);
  const levelLabel = useCallback((engine: string) => choices.find((c) => c.levelId === engine)?.short ?? engine, [choices]);

  // Rails / answers / scoreboard follow the levels of the run that was started (not the live checkboxes).
  const runLevels = useMemo(
    () => choices.filter((c) => runEngines.includes(c.levelId)).map((c) => ({ ...c, stages: levels.find((l) => l.level_id === c.levelId)?.stages ?? [] })),
    [choices, runEngines, levels]
  );
  const rows = scoreboardRows(runLevels, runEngines, stream.state);

  const onToggle = (levelId: string, next: boolean): void =>
    setUnchecked((prev) => {
      const out = new Set(prev);
      if (next) out.delete(levelId);
      else out.add(levelId);
      return out;
    });
  const onRun = (toRun: readonly string[], sel: ModelSelection | null): void => {
    setSelected(null);
    setRunEngines(toRun);
    stream.run({ query: question.query.trim(), questionId: question.questionId.trim() || null, engines: toRun, synthesisModel: sel?.synthesis ?? null, judgeModel: sel?.judge ?? null });
  };
  const openCitation = useCallback((engine: string, citation: Citation) => setSelected({ engine, citation }), []);
  const started = runEngines.length > 0 && (stream.isStreaming || stream.state.events.length > 0 || stream.transportError !== null);

  if (loading) return <p className="text-xs text-ink-3">Loading levels…</p>;
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold text-ink-1">Compare</h1>
      {stream.state.complete && (
        <div className="sticky top-0 z-10 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-1 px-4 py-2 shadow-sm" role="status" data-testid="compare-summary">
          <span className="text-sm font-semibold text-ink-1">{mask(summaryText(rows))}</span>
          <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} />
        </div>
      )}
      <Panel title="Setup" subtitle="One question · one synthesis + judge pair · the enabled levels you tick">
        <CompareSetup question={question} onQuestion={setQuestion} models={models} choices={choices} checked={checked} onToggle={onToggle}
          engines={engines} estimate={estimate} leaderboardError={leaderboard.error ? mask(leaderboard.error) : null} labelFor={levelLabel}
          isStreaming={stream.isStreaming} onRun={onRun} onAbort={stream.abort} />
        {stream.transportError && (
          <div className="mt-3 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
            <span className="font-semibold">Run rejected / stream error:</span> <span className="break-words">{stream.transportError}</span>
          </div>
        )}
        {stream.state.warnings.map((w, i) => <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>)}
      </Panel>
      {started && (
        <>
          <Panel title="Scoreboard" subtitle={stream.state.runId ? `run ${stream.state.runId}` : "waiting for run_started"}
            actions={!stream.state.complete ? <RunStatusPills state={stream.state} isStreaming={stream.isStreaming} labelFor={labelFor} /> : null}>
            <Scoreboard rows={rows} labelFor={labelFor} />
          </Panel>
          <Panel title="Stage rails" subtitle="One rail per level — statuses come only from that engine's events">
            <div data-run-state={stream.state.complete ? "complete" : stream.state.interrupted ? "interrupted" : stream.isStreaming ? "streaming" : "idle"}>
              <CompareRails levels={runLevels} timelines={stream.state.timelines} rows={rows} />
            </div>
          </Panel>
          <CompareAnswers levels={runLevels} timelines={stream.state.timelines} rows={rows} running={stream.isStreaming}
            selected={selected} onSelectCitation={openCitation} labelFor={labelFor} />
        </>
      )}
      <DocumentDrawer citation={selected?.citation ?? null} open={selected !== null} onClose={() => setSelected(null)}
        retrievedChunks={selected ? stream.state.timelines[selected.engine]?.answer?.retrievedChunks : undefined} />
    </div>
  );
};
