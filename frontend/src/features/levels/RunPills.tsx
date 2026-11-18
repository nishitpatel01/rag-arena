import React from "react";
import { Pill } from "../../components/ui/Pill";
import { formatUsd } from "../../lib/format";
import { isManagedEvalService, resolveTimelineCosts } from "../../pages/leaderboardModel";
import type { EngineTimeline } from "../../types/arena";
import type { StreamState } from "./model";

export interface RunPillsProps {
  readonly state: StreamState;
  readonly timeline: EngineTimeline | undefined;
  readonly isStreaming: boolean;
  readonly synthLabel: string | null;
  readonly judgeLabel: string | null;
}

export interface RunCostPillSummary {
  readonly text: string;
  readonly title: string;
}

/** Pure formatter for the Stage-rail Engine $ vs Judge $ cost pill; null before answer_complete arrives. */
export function formatRunCostPill(timeline: EngineTimeline | undefined): RunCostPillSummary | null {
  const costs = resolveTimelineCosts(timeline);
  if (costs.engineCostUsd === null) return null;
  const eng = formatUsd(costs.engineCostUsd);
  if (costs.judgeCostUsd !== null) {
    const jud = formatUsd(costs.judgeCostUsd);
    const tot = formatUsd(costs.totalCostUsd);
    return {
      text: `engine: ${eng} · judge: ${jud}`,
      title: `Scored operational engine cost ${eng} + external judge cost ${jud} (total ${tot})`,
    };
  }
  return {
    text: `engine: ${eng}`,
    title: `Scored operational engine cost ${eng} (external judge cost pending evaluation)`,
  };
}

/** Stage-rail header pills: demo mode, run models, split Engine vs Judge cost, engine error, interruption, status. */
export const RunPills: React.FC<RunPillsProps> = ({ state, timeline, isStreaming, synthLabel, judgeLabel }) => {
  const costPill = formatRunCostPill(timeline);
  const managedEval = isManagedEvalService(state.judgeModel ?? timeline?.evaluation?.judgeModel);
  return (
    <>
      {state.demoMode && (
        <Pill tone={state.replayed ? "info" : "ok"} mono title={state.replayed ? "Frames carry replayed:true (recording of a real run)" : "DEMO_MODE of this run"}>
          {state.replayed ? "replayed recording" : state.demoMode}
        </Pill>
      )}
      {(synthLabel || judgeLabel) && (
        <Pill tone="neutral" mono title="Models from run_started (what this run actually executed)">
          synthesis: {synthLabel ?? "—"} · judge: {judgeLabel ?? "—"}
        </Pill>
      )}
      {managedEval && (
        <Pill tone="info" title="Evaluated via Vertex AI GenAI Evaluation Service (:evaluateInstances)">
          Vertex AI GenAI Eval
        </Pill>
      )}
      {costPill && (
        <Pill tone="neutral" mono title={costPill.title}>
          {costPill.text}
        </Pill>
      )}
      {timeline?.error && <Pill tone="err" mono title={timeline.error.message}>engine_error · {timeline.error.type}</Pill>}
      {state.interrupted && (
        <Pill tone="err" title="The SSE stream ended without battle_complete / engine_error; running stages were marked failed.">
          stream interrupted · {state.interrupted.reason}
        </Pill>
      )}
      {state.complete ? <Pill tone="ok">battle_complete</Pill> : isStreaming ? <Pill tone="active">streaming</Pill> : null}
    </>
  );
};

export function runStateAttr(state: StreamState, isStreaming: boolean): string {
  if (state.complete) return "complete";
  if (state.interrupted) return "interrupted";
  return isStreaming ? "streaming" : "idle";
}
