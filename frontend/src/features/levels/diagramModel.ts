/** Pure layout + state derivation for the architecture diagram (no React). */

import type { ArchEdge, ArchNode, EngineTimeline, StageStatus } from "../../types/arena";
import { IDLE_STAGE } from "./model";

export const NODE_W = 160;
/** Label, service, status/timing, then up to SUMMARY_MAX_LINES result-summary lines. */
export const NODE_H = 92;
/** Mono 11 px ≈ 6.6 px/char → 21 chars fit inside NODE_W minus 10 px padding each side. */
export const SUMMARY_LINE_CHARS = 21;
export const SUMMARY_MAX_LINES = 2;

/**
 * Wraps a " · "-separated summary onto at most `maxLines` lines of ≤ `maxChars` characters,
 * breaking only between parts; a part longer than a line, or overflow past the last line, is cut with "…".
 */
export function wrapSummary(summary: string, maxChars = SUMMARY_LINE_CHARS, maxLines = SUMMARY_MAX_LINES): string[] {
  if (!summary || maxChars < 2 || maxLines < 1) return [];
  const cut = (s: string): string => (s.length > maxChars ? `${s.slice(0, maxChars - 1)}…` : s);
  const parts = summary.split(" · ").slice(0, 32);
  const lines: string[] = [];
  let current = "";
  let overflow = false;
  for (const part of parts) {
    const candidate = current ? `${current} · ${part}` : part;
    if (candidate.length <= maxChars || !current) {
      current = candidate;
      continue;
    }
    lines.push(cut(current));
    current = part;
    if (lines.length === maxLines) {
      overflow = true;
      break;
    }
  }
  if (!overflow && current) lines.push(cut(current));
  if (overflow) {
    const last = lines[maxLines - 1];
    lines[maxLines - 1] = last.endsWith("…") ? last : `${last.length >= maxChars ? last.slice(0, maxChars - 1) : last}…`;
  }
  return lines;
}
export const COL_GAP = 48;
/** Smallest column gap `layoutNodes` shrinks to when fitting a panel width (arrows stay visible). */
export const MIN_COL_GAP = 20;
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

/**
 * Grid layout. With `maxWidth` (the panel's inner width) the column gap shrinks from COL_GAP down to
 * MIN_COL_GAP so the diagram fits without horizontal scroll; node size and font sizes never shrink.
 */
export function layoutNodes(nodes: readonly ArchNode[], maxWidth?: number): DiagramLayout {
  if (nodes.length === 0) return { boxes: [], width: PAD * 2, height: PAD * 2 };
  const cols = compactIndex(nodes.map((n) => n.col));
  const rows = compactIndex(nodes.map((n) => n.row));
  const maxCol = cols.size - 1;
  const maxRow = rows.size - 1;
  let gap = COL_GAP;
  if (maxWidth !== undefined && Number.isFinite(maxWidth) && maxCol > 0) {
    const fitGap = Math.floor((maxWidth - PAD * 2 - (maxCol + 1) * NODE_W) / maxCol);
    gap = Math.max(MIN_COL_GAP, Math.min(COL_GAP, fitGap));
  }
  const boxes = nodes.map((node) => ({
    node,
    x: PAD + (cols.get(node.col) ?? 0) * (NODE_W + gap),
    y: PAD + (rows.get(node.row) ?? 0) * (NODE_H + ROW_GAP),
  }));
  return {
    boxes,
    width: PAD * 2 + (maxCol + 1) * NODE_W + maxCol * gap,
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

/** Approximate width of an 11 px edge label (≤ 12 chars shown). */
export function edgeLabelWidth(label: string): number {
  return Math.min(label.length, 12) * 6.2;
}

export interface EdgePath {
  readonly edge: ArchEdge;
  readonly d: string;
  readonly labelX: number;
  readonly labelY: number;
  /** Horizontal room (px) for the label; labels that do not fit are drawn only as a hover title. */
  readonly labelRoom: number;
  /** The reverse edge (B→A) is declared too. */
  readonly paired: boolean;
  readonly flowing: boolean;
}

/**
 * Edge geometry; `flowing` when source is `done` and target is `active` (spec §7.2.1), or when the edge key
 * "from->to" is in `extraFlowing` (Level 4: agent ⇄ tool while a tool call is open).
 */
export function buildEdgePaths(
  edges: readonly ArchEdge[],
  boxes: readonly NodeBox[],
  views: Readonly<Record<string, NodeView>>,
  extraFlowing: ReadonlySet<string> = new Set()
): EdgePath[] {
  const byId = new Map(boxes.map((b) => [b.node.id, b]));
  const keys = new Set(edges.map((e) => `${e.from}->${e.to}`));
  const out: EdgePath[] = [];
  for (const edge of edges) {
    const a = byId.get(edge.from);
    const b = byId.get(edge.to);
    if (!a || !b) continue;
    // A→B and B→A both declared (Level 4 agent ⇄ tool): forward and return lines run parallel, 7 px apart.
    const paired = keys.has(`${edge.to}->${edge.from}`);
    const x1 = a.x + NODE_W;
    const y2c = b.y + NODE_H / 2;
    const backwards = b.x < x1;
    const sameRow = Math.abs(a.y - b.y) < 1;
    const adjacentLeft = b.x + NODE_W < a.x && a.x - (b.x + NODE_W) <= COL_GAP + 1;
    let d: string;
    let labelX: number;
    let labelY: number;
    let labelRoom: number;
    if (!backwards) {
      const off = paired ? -7 : 0;
      const y1 = a.y + NODE_H / 2 + off;
      const y2 = y2c + off;
      const cx = (x1 + b.x) / 2;
      d = `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${b.x} ${y2}`;
      labelX = (x1 + b.x) / 2;
      // Same-row edges: label just below the boxes; cross-row edges: open space at the curve midpoint.
      labelY = sameRow ? y1 + NODE_H / 2 + 14 : (y1 + y2) / 2 - 8;
      // Same-row labels sit under the boxes (room = centre-to-centre); cross-row labels live in the column gap.
      // A paired (A⇄B) same-row edge shares the gap with its return line, so it only gets the gap.
      labelRoom = sameRow && !paired ? b.x + NODE_W / 2 - (a.x + NODE_W / 2) : b.x - x1;
    } else if (b.x + NODE_W <= a.x && (!sameRow || adjacentLeft)) {
      // Return edge to a column on the left: source left side → target right side (never crosses the source box).
      const off = paired ? 7 : 0;
      const sx = a.x;
      const sy = a.y + NODE_H / 2 + off;
      const tx = b.x + NODE_W;
      const ty = y2c + off;
      const cx = (sx + tx) / 2;
      d = `M ${sx} ${sy} C ${cx} ${sy}, ${cx} ${ty}, ${tx} ${ty}`;
      labelX = cx;
      // Label below the line; a paired forward edge labels above (-8), so the two never overlap.
      labelY = (sy + ty) / 2 + 12;
      labelRoom = sx - tx;
    } else {
      // Same row, non-adjacent (or overlapping columns): arc through the gap under the boxes, bottom-centre to bottom-centre.
      const sx = a.x + NODE_W / 2;
      const tx = b.x + NODE_W / 2;
      const by = Math.max(a.y, b.y) + NODE_H;
      const dip = by + ROW_GAP - 6;
      d = `M ${sx} ${a.y + NODE_H} C ${sx} ${dip}, ${tx} ${dip}, ${tx} ${b.y + NODE_H}`;
      labelX = (sx + tx) / 2;
      labelY = dip + 4;
      labelRoom = Math.abs(sx - tx);
    }
    out.push({
      edge,
      d,
      labelX,
      labelY,
      labelRoom,
      paired,
      flowing:
        extraFlowing.has(`${edge.from}->${edge.to}`) || (views[edge.from]?.status === "done" && views[edge.to]?.status === "active"),
    });
  }
  return out;
}

/**
 * Indices of edge labels to draw: a paired (A⇄B) edge label needs room in its gap, and a label whose box
 * overlaps an already placed label is skipped (it stays available as the path's hover title). Levels without
 * crowded or bidirectional edges keep every label.
 */
export function visibleEdgeLabels(paths: readonly EdgePath[]): Set<number> {
  const placed: { x0: number; x1: number; y0: number; y1: number }[] = [];
  const out = new Set<number>();
  paths.forEach((p, i) => {
    const label = p.edge.label;
    if (!label) return;
    const w = edgeLabelWidth(label);
    if (p.paired && p.labelRoom < w) return;
    const box = { x0: p.labelX - w / 2, x1: p.labelX + w / 2, y0: p.labelY - 10, y1: p.labelY + 3 };
    if (placed.some((q) => box.x0 < q.x1 && q.x0 < box.x1 && box.y0 < q.y1 && q.y0 < box.y1)) return;
    placed.push(box);
    out.add(i);
  });
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

/** Where a node's displayed service text came from. */
export type NodeServiceSource = "stage" | "run" | "default";

export interface NodeServiceView {
  readonly text: string;
  readonly source: NodeServiceSource;
}

/** Short suffix shown next to a service that is not (yet) reported by a stage event. */
export const SERVICE_SOURCE_LABEL: Readonly<Record<NodeServiceSource, string>> = {
  stage: "",
  run: "(selected for this run)",
  default: "(default)",
};

/**
 * Service text for a node, preferring what the backend actually reported:
 * 1. `stage_started.service` of a mapped stage (latest started wins) — the model really called;
 * 2. the run's model from `run_started` for that stage (e.g. synthesize/evaluate before they start);
 * 3. the descriptor's design-time default, labelled "(default)".
 */
export function resolveNodeService(
  node: ArchNode,
  timeline: EngineTimeline | undefined,
  runServiceByStage: Readonly<Record<string, string | null | undefined>> = {}
): NodeServiceView {
  let latest: { service: string; startedAt: number } | null = null;
  for (const stageId of node.stage_ids) {
    const st = timeline?.stages[stageId];
    if (!st?.service) continue;
    const at = st.startedAt ?? 0;
    if (!latest || at >= latest.startedAt) latest = { service: st.service, startedAt: at };
  }
  if (latest) return { text: latest.service, source: "stage" };
  for (const stageId of node.stage_ids) {
    const runService = runServiceByStage[stageId];
    if (runService) return { text: runService, source: "run" };
  }
  return { text: node.service, source: "default" };
}
