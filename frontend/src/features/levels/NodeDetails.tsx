import React from "react";
import type { ArchNode, EngineTimeline } from "../../types/arena";
import { CodeBlock } from "../../components/ui/CodeBlock";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { formatMs, summarizePayload } from "../../lib/format";
import { IDLE_STAGE } from "./model";
import { splitPayload } from "./StageDetail";
import { STAGE_LABELS, STATUS_META } from "./StageRail";

export interface NodeDetailsProps {
  readonly node: ArchNode | null;
  readonly timeline: EngineTimeline | undefined;
}

/** Details pane next to the diagram: service/region, request, response summary, code ref, duration. */
export const NodeDetails: React.FC<NodeDetailsProps> = ({ node, timeline }) => {
  if (!node) {
    return <p className="text-xs text-ink-3">Run the level or click a node to see what that service received and returned.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-1">{node.label}</h3>
        <p className="text-xs text-ink-2">{node.service}</p>
        <KeyValue
          items={[
            ["kind", node.kind],
            ["region", node.region ?? "—"],
            ["code ref", node.code_ref ?? "—"],
          ]}
        />
      </div>
      <div className="flex min-w-0 flex-col gap-2">
      {node.stage_ids.map((stageId) => {
        const st = timeline?.stages[stageId] ?? IDLE_STAGE;
        const meta = STATUS_META[st.status];
        const req = splitPayload(st.request);
        const summary = summarizePayload(st.payload);
        const turns = stageId === "agent_turn" ? timeline?.agentTurns ?? [] : [];
        const lastTurn = turns[turns.length - 1];
        return (
          <section key={stageId} className="rounded-md border border-line bg-surface-0 p-2.5" aria-label={`Stage ${stageId}`}>
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-xs font-semibold text-ink-1">{STAGE_LABELS[stageId] ?? stageId}</span>
              <Pill tone={st.status === "done" ? "ok" : st.status === "failed" ? "err" : st.status === "active" ? "active" : st.status === "skipped" ? "locked" : "neutral"}>
                {meta.text}
              </Pill>
              {st.durationMs !== null && <span className="font-mono text-xs text-ink-2">{formatMs(st.durationMs)}</span>}
            </div>
            {summary && <p className="mb-1.5 font-mono text-xs text-ink-2">{summary}</p>}
            {st.error && (
              <p className="mb-1.5 text-xs text-err">
                <span className="font-mono font-semibold">{st.error.type}</span>: {st.error.message}
              </p>
            )}
            {st.skipReason && <p className="mb-1.5 text-xs text-ink-2">Skipped: {st.skipReason}</p>}
            {req.scalars.length > 0 && <KeyValue items={req.scalars} />}
            {req.code.slice(0, 1).map(([k, v]) => (
              <div key={k} className="mt-1.5">
                <CodeBlock label={k} language={k === "sql" ? "sql" : "json"} code={v} maxHeightClass="max-h-40" />
              </div>
            ))}
            {lastTurn && (
              <p className="mt-1.5 text-xs text-ink-2">
                Current tool turn: <span className="font-mono">#{lastTurn.turnIndex} {lastTurn.toolName}</span> ({formatMs(lastTurn.durationMs)})
              </p>
            )}
          </section>
        );
      })}
      </div>
    </div>
  );
};
