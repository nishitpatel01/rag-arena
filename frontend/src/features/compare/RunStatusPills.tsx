import React from "react";
import { Pill } from "../../components/ui/Pill";
import type { StreamState } from "../levels/model";

export interface RunStatusPillsProps {
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly labelFor: (id: string | null) => string | null;
}

/** Mode + models of the run actually executed (from run_started) + terminal state; text, never colour alone. */
export const RunStatusPills: React.FC<RunStatusPillsProps> = ({ state, isStreaming, labelFor }) => (
  <div className="flex flex-wrap items-center gap-1.5">
    {state.demoMode && (
      <Pill tone={state.replayed ? "info" : "ok"} mono title={state.replayed ? "Frames carry replayed:true (recording of a real run)" : "DEMO_MODE of this run"}>
        {state.replayed ? "replayed recording" : state.demoMode}
      </Pill>
    )}
    {(state.synthesisModel || state.judgeModel) && (
      <Pill tone="neutral" mono title="Models from run_started (what this run actually executed)">
        synthesis: {labelFor(state.synthesisModel) ?? "—"} · judge: {labelFor(state.judgeModel) ?? "—"}
      </Pill>
    )}
    {state.questionId && <Pill tone="neutral" mono>{state.questionId}</Pill>}
    {state.interrupted && (
      <Pill tone="err" title="The SSE stream ended without battle_complete; running stages were marked failed.">
        stream interrupted · {state.interrupted.reason}
      </Pill>
    )}
    {state.complete ? <Pill tone="ok">battle_complete</Pill> : isStreaming ? <Pill tone="active">streaming</Pill> : null}
  </div>
);
