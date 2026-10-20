/** Pure layout + state derivation for the architecture diagram (no React). */

import type { ArchEdge, ArchNode, EngineTimeline, StageStatus } from "../../types/arena";
import { IDLE_STAGE } from "./model";

export const NODE_W = 170;
export const NODE_H = 64;
export const COL_GAP = 48;
export const ROW_GAP = 28;
export const PAD = 16;

export interface NodeBox {
  readonly node: ArchNode;
  readonly x: number;
  readonly y: number;
}

export interface DiagramLayout {
  readonly boxes: readonly NodeBox[];
  readonly width: number;
  readonly height: number;
}

/** Map sparse grid indices (e.g. cols 0,1,2,4,5) onto consecutive slots so empty columns take no space. */
function compactIndex(values: readonly number[]): Map<number, number> {
  const sorted = [...new Set(values)].sort((a, b) => a - b);
  return new Map(sorted.map((v, i) => [v, i]));
}

export function layoutNodes(nodes: readonly ArchNode[]): DiagramLayout {
  if (nodes.length === 0) return { boxes: [], width: PAD * 2, height: PAD * 2 };
  const cols = compactIndex(nodes.map((n) => n.col));
  const rows = compactIndex(nodes.map((n) => n.row));
  const maxCol = cols.size - 1;
  const maxRow = rows.size - 1;
  const boxes = nodes.map((node) => ({
    node,
    x: PAD + (cols.get(node.col) ?? 0) * (NODE_W + COL_GAP),
    y: PAD + (rows.get(node.row) ?? 0) * (NODE_H + ROW_GAP),
  }));
  return {
    boxes,
    width: PAD * 2 + (maxCol + 1) * NODE_W + maxCol * COL_GAP,
    // Extra bottom room for same-row edge labels drawn under the boxes.
    height: PAD * 2 + (maxRow + 1) * NODE_H + maxRow * ROW_GAP + 16,
  };
}

const STATUS_RANK: Record<StageStatus, number> = { failed: 5, active: 4, done: 3, skipped: 2, idle: 1 };

export interface NodeView {
  readonly status: StageStatus;
  /** The stage that determines the node's status (most significant mapped stage). */
  readonly stageId: string | null;
  readonly durationMs: number | null;
  readonly startedAt: number | null;
}

/**
 * Node status from its mapped stages: failed > active > done > skipped > idle.
 * With several `done` stages, duration is their sum (all backend-measured).
 */
export function deriveNodeView(node: ArchNode, timeline: EngineTimeline | undefined): NodeView {
  let best: NodeView = { status: "idle", stageId: null, durationMs: null, startedAt: null };
  let doneTotal = 0;
  let doneCount = 0;
  for (const stageId of node.stage_ids) {
    const st = timeline?.stages[stageId] ?? IDLE_STAGE;
    if (st.status === "done" && st.durationMs !== null) {
      doneTotal += st.durationMs;
      doneCount += 1;
    }
    if (STATUS_RANK[st.status] > STATUS_RANK[best.status]) {
      best = { status: st.status, stageId, durationMs: st.durationMs, startedAt: st.startedAt };
    }
  }
  if (best.status === "done" && doneCount > 1) {
    return { ...best, durationMs: doneTotal };
  }
  return best;
}

export interface EdgePath {
  readonly edge: ArchEdge;
  readonly d: string;
  readonly labelX: number;
  readonly labelY: number;
  readonly flowing: boolean;
}

/** Edge geometry; `flowing` only when source is `done` and target is `active` (spec §7.2.1). */
export function buildEdgePaths(
  edges: readonly ArchEdge[],
  boxes: readonly NodeBox[],
  views: Readonly<Record<string, NodeView>>
): EdgePath[] {
  const byId = new Map(boxes.map((b) => [b.node.id, b]));
  const out: EdgePath[] = [];
  for (const edge of edges) {
    const a = byId.get(edge.from);
    const b = byId.get(edge.to);
    if (!a || !b) continue;
    const x1 = a.x + NODE_W;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const backwards = x2 < x1;
    const cx = backwards ? Math.max(x1, x2) + COL_GAP : (x1 + x2) / 2;
    const d = backwards
      ? `M ${x1} ${y1} C ${x1 + COL_GAP} ${y1}, ${cx} ${y2 - ROW_GAP}, ${x2 + NODE_W} ${y2}`
      : `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
    // Same-row edges: label sits just below the boxes so it never collides with a node;
    // cross-row edges have open space at the curve midpoint.
    const sameRow = Math.abs(y1 - y2) < 1;
    out.push({
      edge,
      d,
      labelX: (x1 + x2) / 2,
      labelY: sameRow ? y1 + NODE_H / 2 + 14 : (y1 + y2) / 2 - 8,
      flowing: views[edge.from]?.status === "done" && views[edge.to]?.status === "active",
    });
  }
  return out;
}

/** The node to auto-follow: first `active`, else the most recently non-idle node, else null. */
export function followTarget(nodes: readonly ArchNode[], timeline: EngineTimeline | undefined): string | null {
  if (!timeline) return null;
  const active = nodes.find((n) => n.stage_ids.some((s) => timeline.stages[s]?.status === "active"));
  if (active) return active.id;
  for (let i = timeline.stageOrder.length - 1; i >= 0; i -= 1) {
    const stage = timeline.stageOrder[i];
    const n = nodes.find((node) => node.stage_ids.includes(stage));
    if (n) return n.id;
  }
  return null;
}
