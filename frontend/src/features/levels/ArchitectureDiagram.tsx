import React, { useEffect, useMemo, useRef, useState } from "react";
import { Pin, PinOff } from "lucide-react";
import type { ArchEdge, ArchNode, EngineTimeline } from "../../types/arena";
import { Button } from "../../components/ui/Button";
import { DiagramNode } from "./DiagramNode";
import { buildEdgePaths, deriveNodeView, followTarget, layoutNodes, resolveNodeService, type NodeServiceView, type NodeView } from "./diagramModel";
import { NodeDetails } from "./NodeDetails";
import { summarizeStage } from "./stagePayloads";

export const DIAGRAM_MIN_HEIGHT = 280;

export interface ArchitectureDiagramProps {
  readonly title: string;
  readonly nodes: readonly ArchNode[];
  readonly edges: readonly ArchEdge[];
  readonly timeline: EngineTimeline | undefined;
  readonly running: boolean;
  /** Selecting a node also selects its first mapped stage in the parent (keeps rail + diagram in sync). */
  readonly onStageSelect?: (stageId: string) => void;
  readonly selectedStage?: string | null;
  /** Per-stage model label from `run_started` (e.g. synthesize/evaluate) shown until the stage reports its service. */
  readonly runServiceByStage?: Readonly<Record<string, string | null | undefined>>;
  /** "side" (default): details pane beside the diagram on xl; "below": stacked, giving the SVG full width. */
  readonly detailsPlacement?: "side" | "below";
}

/** Container width via ResizeObserver (disconnected on unmount). */
function useContainerWidth(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.floor(entry.contentRect.width));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/**
 * Data-driven SVG (spec §7.2.1). All node/edge state derives from the same event timeline as the
 * stage rail. The SVG grows to the panel width but never renders below 1× (text stays ≥ its font
 * size); narrower panels scroll horizontally. The details pane follows the active node unless pinned.
 */
export const ArchitectureDiagram: React.FC<ArchitectureDiagramProps> = ({
  title,
  nodes,
  edges,
  timeline,
  running,
  onStageSelect,
  selectedStage,
  runServiceByStage,
  detailsPlacement = "side",
}) => {
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const [manualId, setManualId] = useState<string | null>(null);
  const [containerRef, containerWidth] = useContainerWidth();

  const layout = useMemo(() => layoutNodes(nodes), [nodes]);
  const views = useMemo<Record<string, NodeView>>(
    () => Object.fromEntries(nodes.map((n) => [n.id, deriveNodeView(n, timeline)])),
    [nodes, timeline]
  );
  const services = useMemo<Record<string, NodeServiceView>>(
    () => Object.fromEntries(nodes.map((n) => [n.id, resolveNodeService(n, timeline, runServiceByStage)])),
    [nodes, timeline, runServiceByStage]
  );
  const paths = useMemo(() => buildEdgePaths(edges, layout.boxes, views), [edges, layout.boxes, views]);
  const followed = useMemo(() => followTarget(nodes, timeline), [nodes, timeline]);

  // Rail → diagram sync: when the parent selects a stage, highlight the node that maps it.
  useEffect(() => {
    if (!selectedStage) return;
    const n = nodes.find((node) => node.stage_ids.includes(selectedStage));
    if (n) setManualId(n.id);
  }, [selectedStage, nodes]);

  // A new run (timeline cleared) drops stale manual selection so the pane follows again.
  useEffect(() => {
    if (timeline && timeline.stageOrder.length === 0) setManualId(null);
  }, [timeline]);

  const shownId = pinnedId ?? (running ? followed ?? manualId : manualId ?? followed);
  const shownNode = nodes.find((n) => n.id === shownId) ?? null;

  const select = (nodeId: string): void => {
    setManualId(nodeId);
    if (pinnedId) setPinnedId(nodeId);
    const node = nodes.find((n) => n.id === nodeId);
    const firstStage = node?.stage_ids[0];
    if (firstStage && onStageSelect) onStageSelect(firstStage);
  };

  const togglePin = (): void => setPinnedId((cur) => (cur ? null : shownId));

  // Scale ≥ 1 so labels never shrink below their font size; grow to fill the available width.
  const scale = containerWidth > 0 ? Math.max(1, containerWidth / layout.width) : 1;
  const svgWidth = Math.round(layout.width * scale);
  const svgHeight = Math.max(DIAGRAM_MIN_HEIGHT, Math.round(layout.height * scale));

  return (
    <div className={`grid min-w-0 gap-3 ${detailsPlacement === "side" ? "xl:grid-cols-[minmax(0,1fr)_minmax(16rem,20rem)]" : ""}`}>
      <figure ref={containerRef} className="min-w-0 overflow-x-auto rounded-md border border-line bg-surface-0 p-2">
        <figcaption className="sr-only">{title}</figcaption>
        <svg
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          preserveAspectRatio="xMinYMid meet"
          width={svgWidth}
          height={svgHeight}
          style={{ display: "block", minHeight: DIAGRAM_MIN_HEIGHT }}
          role="group"
          aria-label={title}
        >
          <defs>
            <marker id="arena-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--ink-3)" />
            </marker>
          </defs>
          {paths.map((p) => (
            <g key={`${p.edge.from}->${p.edge.to}`}>
              <path d={p.d} fill="none" stroke="var(--line)" strokeWidth={2} markerEnd="url(#arena-arrow)" />
              {p.flowing && <path d={p.d} fill="none" stroke="var(--accent-moss)" strokeWidth={2.5} className="edge-flowing" />}
              {p.edge.label && (
                <text x={p.labelX} y={p.labelY} textAnchor="middle" fontSize={11} fill="var(--ink-3)">
                  {p.edge.label.length > 12 ? `${p.edge.label.slice(0, 11)}…` : p.edge.label}
                </text>
              )}
            </g>
          ))}
          {layout.boxes.map(({ node, x, y }) => {
            const view = views[node.id];
            const st = view.stageId ? timeline?.stages[view.stageId] : undefined;
            return (
              <DiagramNode
                key={node.id}
                node={node}
                x={x}
                y={y}
                view={view}
                service={services[node.id]}
                selected={shownId === node.id}
                summary={view.status === "done" ? summarizeStage(view.stageId, st?.payload ?? null) : ""}
                onSelect={select}
              />
            );
          })}
        </svg>
      </figure>
      <aside className="min-w-0 rounded-md border border-line bg-surface-1 p-3" aria-label="Node details">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-ink-3">
            {pinnedId ? "Pinned node" : running ? "Following active node" : "Node details"}
          </span>
          <Button size="sm" variant="ghost" aria-pressed={pinnedId !== null} onClick={togglePin} disabled={!shownId}
            leadingIcon={pinnedId ? <PinOff className="h-3.5 w-3.5" aria-hidden /> : <Pin className="h-3.5 w-3.5" aria-hidden />}>
            {pinnedId ? "Unpin" : "Pin"}
          </Button>
        </div>
        <NodeDetails node={shownNode} timeline={timeline} service={shownNode ? services[shownNode.id] ?? null : null} />
      </aside>
    </div>
  );
};
