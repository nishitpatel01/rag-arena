import React from "react";
import type { ArchNode, EngineTimeline } from "../../types/arena";
import { CodeBlock } from "../../components/ui/CodeBlock";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import { IDLE_STAGE } from "./model";
import { AgentTurnTimeline } from "./AgentTurnTimeline";
import type { ToolBinding } from "./agentTimelineModel";
import { SERVICE_SOURCE_LABEL, type NodeServiceView } from "./diagramModel";
import { splitPayload } from "./StageDetail";
import { summarizeStage } from "./stagePayloads";
import { STAGE_LABELS, STATUS_META } from "./StageRail";

export interface NodeDetailsProps {
  readonly node: ArchNode | null;
  readonly timeline: EngineTimeline | undefined;
  /** Resolved service (actual stage service, run model, or labelled descriptor default). */
  readonly service: NodeServiceView | null;
  /** Level 4: the node represents these ADK tools (its timeline shows only their calls). */
  readonly toolBinding?: ToolBinding | null;
}

/** Details pane next to the diagram: service/region, request, response summary, code ref, duration. */
export const NodeDetails: React.FC<NodeDetailsProps> = ({ node, timeline, service, toolBinding = null }) => {
  if (!node) {
    return <p className="text-xs text-ink-3">Run the level or click a node to see what that service received and returned.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-1">{node.label}</h3>
        <p className="text-xs text-ink-2" data-service-source={service?.source ?? "default"}>
          {service?.text ?? node.service}
          {(service?.source ?? "default") !== "stage" && (
            <span className="ml-1 italic text-ink-3">{SERVICE_SOURCE_LABEL[service?.source ?? "default"]}</span>
          )}
        </p>
        {service?.source === "stage" && service.text !== node.service && (
          <p className="text-xs text-ink-3">descriptor default: {node.service}</p>
        )}
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
        const summary = summarizeStage(stageId, st.payload);
        const turns = stageId === "agent_turn" ? timeline?.agentTurns ?? [] : [];
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
            {stageId === "agent_turn" && (turns.length > 0 || st.status !== "idle") && (
              <div className="mt-2" data-testid="node-agent-timeline">
                <AgentTurnTimeline turns={turns} stageStatus={st.status} payload={st.payload} compact binding={toolBinding ?? undefined} />
              </div>
            )}
          </section>
        );
      })}
      </div>
    </div>
  );
};
