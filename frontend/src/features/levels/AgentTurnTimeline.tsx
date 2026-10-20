import React, { useState } from "react";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import type { AgentTurnRecord } from "../../types/arena";
import { CodeBlock, stringifyJson } from "../../components/ui/CodeBlock";
import { EmptyState } from "../../components/ui/EmptyState";
import { formatMs } from "../../lib/format";

interface TurnRowProps {
  readonly turn: AgentTurnRecord;
}

const TurnRow: React.FC<TurnRowProps> = ({ turn }) => {
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <li className="rounded-md border border-line bg-surface-1">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-2"
      >
        <Chevron className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        <span className="font-mono text-xs text-ink-3">#{turn.turnIndex}</span>
        <Wrench className="h-3.5 w-3.5 shrink-0 text-slate" aria-hidden />
        <span className="font-mono text-xs font-semibold text-ink-1">{turn.toolName}</span>
        <span className="ml-auto font-mono text-xs tabular-nums text-ink-2">{formatMs(turn.durationMs)}</span>
      </button>
      {open && (
        <div className="grid gap-2 border-t border-line p-3 xl:grid-cols-2">
          <CodeBlock label="tool_args" language="json" code={stringifyJson(turn.toolArgs)} maxHeightClass="max-h-48" />
          <div>
            <h5 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Result summary</h5>
            <p className="whitespace-pre-wrap break-words text-xs text-ink-1">{turn.resultSummary}</p>
          </div>
        </div>
      )}
    </li>
  );
};

export interface AgentTurnTimelineProps {
  readonly turns: readonly AgentTurnRecord[];
  readonly active: boolean;
}

/** Level 4: `agent_turn` events rendered as an expanding tool-call list (order = arrival order). */
export const AgentTurnTimeline: React.FC<AgentTurnTimelineProps> = ({ turns, active }) => {
  if (turns.length === 0) {
    return (
      <EmptyState
        compact
        title={active ? "Waiting for the first tool call…" : "No agent turns"}
        description="Each ADK tool invocation is appended here the moment the backend reports it."
      />
    );
  }
  return (
    <ol aria-label="Agent tool-call timeline" className="flex flex-col gap-1.5">
      {turns.map((turn) => (
        <TurnRow key={`${turn.turnIndex}-${turn.ts}`} turn={turn} />
      ))}
    </ol>
  );
};
