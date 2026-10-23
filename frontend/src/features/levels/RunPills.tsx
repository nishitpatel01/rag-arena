import React from "react";
import { Pill } from "../../components/ui/Pill";
import type { EngineTimeline } from "../../types/arena";
import type { StreamState } from "./model";

export interface RunPillsProps {
  readonly state: StreamState;
  readonly timeline: EngineTimeline | undefined;
  readonly isStreaming: boolean;
  readonly synthLabel: string | null;
  readonly judgeLabel: string | null;
}

/** Stage-rail header pills: demo mode, run models (from run_started), engine error, interruption, status. */
export const RunPills: React.FC<RunPillsProps> = ({ state, timeline, isStreaming, synthLabel, judgeLabel }) => (
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
    {timeline?.error && <Pill tone="err" mono title={timeline.error.message}>engine_error · {timeline.error.type}</Pill>}
    {state.interrupted && (
      <Pill tone="err" title="The SSE stream ended without battle_complete / engine_error; running stages were marked failed.">
        stream interrupted · {state.interrupted.reason}
      </Pill>
    )}
    {state.complete ? <Pill tone="ok">battle_complete</Pill> : isStreaming ? <Pill tone="active">streaming</Pill> : null}
  </>
);

export function runStateAttr(state: StreamState, isStreaming: boolean): string {
  if (state.complete) return "complete";
  if (state.interrupted) return "interrupted";
  return isStreaming ? "streaming" : "idle";
}
