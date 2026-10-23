import React from "react";
import { AlertTriangle, Check, Circle, CircleDashed, Loader } from "lucide-react";
import type { ArchNode, StageStatus } from "../../types/arena";
import { formatMs, summarizePayload } from "../../lib/format";
import { NODE_H, NODE_W, SERVICE_SOURCE_LABEL, wrapSummary, type NodeServiceView, type NodeView } from "./diagramModel";
import { useElapsedMs } from "./useElapsedMs";

const KIND_GLYPH: Record<ArchNode["kind"], string> = {
  actor: "◯",
  model: "◆",
  store: "▤",
  api: "⇄",
  compute: "⚙",
  index: "≡",
  agent: "✦",
  process: "⚙",
};

const STATUS_STYLE: Record<
  StageStatus,
  { readonly stroke: string; readonly fill: string; readonly text: string; readonly dash?: string; readonly Icon: React.ComponentType<{ className?: string }> }
> = {
  idle: { stroke: "var(--line)", fill: "var(--surface-1)", text: "idle", Icon: Circle },
  active: { stroke: "var(--accent-moss)", fill: "var(--surface-2)", text: "running", Icon: Loader },
  done: { stroke: "var(--status-ok)", fill: "var(--surface-2)", text: "done", Icon: Check },
  failed: { stroke: "var(--status-err)", fill: "var(--surface-1)", text: "failed", Icon: AlertTriangle },
  skipped: { stroke: "var(--line)", fill: "var(--surface-0)", text: "skipped", dash: "5 4", Icon: CircleDashed },
};

export interface DiagramNodeProps {
  readonly node: ArchNode;
  readonly x: number;
  readonly y: number;
  readonly view: NodeView;
  /** Service actually reported by the stage (or the labelled default before the run). */
  readonly service: NodeServiceView;
  readonly selected: boolean;
  readonly summary: string;
  readonly onSelect: (nodeId: string) => void;
}

/** One SVG node: icon + label + service + status text; keyboard-focusable via <g role=button>. */
export const DiagramNode: React.FC<DiagramNodeProps> = ({ node, x, y, view, service, selected, summary, onSelect }) => {
  const style = STATUS_STYLE[view.status];
  const sourceLabel = SERVICE_SOURCE_LABEL[service.source];
  // Prefix the source label so truncation never hides that the text is a default.
  const serviceLine = service.source === "default" ? `default · ${service.text}` : service.text;
  const elapsed = useElapsedMs(view.startedAt, view.status === "active");
  const timing =
    view.status === "active" ? `${formatMs(elapsed)} …` : view.durationMs !== null ? formatMs(view.durationMs) : "";
  const third = timing ? `${style.text} · ${timing}` : style.text;
  const summaryLines = wrapSummary(summary);

  return (
    <g
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={`${node.label} (${service.text}${sourceLabel ? ` ${sourceLabel}` : ""}): ${style.text}`}
      transform={`translate(${x} ${y})`}
      onClick={() => onSelect(node.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(node.id);
        }
      }}
      className={`cursor-pointer outline-none ${view.status === "active" ? "stage-active-outline" : ""}`}
      style={{ outlineOffset: 2 }}
    >
      <rect
        width={NODE_W}
        height={NODE_H}
        rx={8}
        fill={style.fill}
        stroke={selected ? "var(--accent-moss)" : style.stroke}
        strokeWidth={selected ? 3 : 2}
        strokeDasharray={style.dash}
      />
      <text x={10} y={21} fontSize={13} fontWeight={600} fill="var(--ink-1)">
        <tspan fill={style.stroke}>{KIND_GLYPH[node.kind]} </tspan>
        {node.label.length > 17 ? `${node.label.slice(0, 16)}…` : node.label}
      </text>
      <text x={10} y={37} fontSize={11.5} fill="var(--ink-3)" fontStyle={service.source === "stage" ? undefined : "italic"} data-service-source={service.source}>
        {serviceLine.length > 24 ? `${serviceLine.slice(0, 23)}…` : serviceLine}
      </text>
      <text x={10} y={53} fontSize={11.5} fontFamily="ui-monospace, monospace" fill={style.stroke}>
        {third.length > 21 ? `${third.slice(0, 20)}…` : third}
      </text>
      {summaryLines.map((line, i) => (
        <text key={i} x={10} y={69 + i * 14} fontSize={11} fontFamily="ui-monospace, monospace" fill="var(--ink-2)" data-testid="node-summary">
          {line}
        </text>
      ))}
      <title>{`${node.label} — ${service.text}${sourceLabel ? ` ${sourceLabel}` : ""}\n${style.text}${timing ? ` · ${timing}` : ""}${summary ? `\n${summary}` : ""}`}</title>
    </g>
  );
};

export { STATUS_STYLE as DIAGRAM_STATUS_STYLE };
export const summarizeNodePayload = summarizePayload;
