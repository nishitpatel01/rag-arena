import React from "react";
import type { BoundingBox, Citation } from "../types/arena";
import { EmptyState } from "./ui/EmptyState";
import { KeyValue } from "./ui/KeyValue";

const CANVAS_WIDTH = 1000;
const CANVAS_HEIGHT = 1400;

export interface ScaledRect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

/** Project a normalized (0–1) bbox onto the page canvas; clamps malformed boxes. */
export function projectNormalizedBbox(bbox: BoundingBox): ScaledRect {
  if (!bbox || typeof bbox.x0 !== "number" || typeof bbox.y0 !== "number") {
    return { x: 80, y: 140, width: 840, height: 220 };
  }
  const clampedX0 = Math.max(0, Math.min(0.95, bbox.x0));
  const clampedY0 = Math.max(0, Math.min(0.95, bbox.y0));
  const clampedX1 = Math.max(clampedX0 + 0.05, Math.min(1.0, bbox.x1));
  const clampedY1 = Math.max(clampedY0 + 0.05, Math.min(1.0, bbox.y1));
  return {
    x: Math.round(clampedX0 * CANVAS_WIDTH),
    y: Math.round(clampedY0 * CANVAS_HEIGHT),
    width: Math.round((clampedX1 - clampedX0) * CANVAS_WIDTH),
    height: Math.round((clampedY1 - clampedY0) * CANVAS_HEIGHT),
  };
}

export interface DocumentViewerProps {
  readonly citation: Citation | null;
}

/** Page-shaped canvas with the cited bbox highlighted; responsive via aspect-ratio. */
export const DocumentViewer: React.FC<DocumentViewerProps> = ({ citation }) => {
  if (!citation) {
    return (
      <EmptyState
        title="No citation selected"
        description="Select a citation under the answer to see its page region and the extracted span."
      />
    );
  }
  const rect = projectNormalizedBbox(citation.bbox);
  const bboxText = `[${citation.bbox.x0.toFixed(2)}, ${citation.bbox.y0.toFixed(2)}, ${citation.bbox.x1.toFixed(2)}, ${citation.bbox.y1.toFixed(2)}]`;

  return (
    <div className="flex flex-col gap-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-1">
          {citation.doc_name} · p.{citation.page_number}
        </h3>
        <p className="font-mono text-xs text-slate">
          {citation.solicitation_id} · {citation.section_id || "General"} · {citation.element_type}
        </p>
      </div>

      <div className="w-full rounded-md border border-line bg-surface-0 p-2" style={{ aspectRatio: `${CANVAS_WIDTH} / ${CANVAS_HEIGHT}` }}>
        <svg
          viewBox={`0 0 ${CANVAS_WIDTH} ${CANVAS_HEIGHT}`}
          className="h-full w-full"
          role="img"
          aria-label={`Bounding box highlight for ${citation.doc_name} page ${citation.page_number}`}
        >
          <rect x={40} y={40} width={920} height={1320} rx={12} fill="var(--surface-1)" stroke="var(--line)" strokeWidth={4} />
          <text x={80} y={100} fill="var(--ink-3)" fontSize={28} fontFamily="ui-monospace, monospace">
            {citation.doc_name} — p.{citation.page_number}
          </text>
          <line x1={80} y1={130} x2={920} y2={130} stroke="var(--line)" strokeWidth={3} />
          <rect
            x={rect.x}
            y={rect.y}
            width={rect.width}
            height={rect.height}
            rx={8}
            fill="var(--accent-ochre)"
            fillOpacity={0.22}
            stroke="var(--accent-ochre)"
            strokeWidth={6}
          />
          <text x={rect.x + 16} y={Math.max(70, rect.y - 12)} fill="var(--accent-ochre)" fontSize={26} fontFamily="ui-monospace, monospace">
            {citation.section_id || citation.element_type}
          </text>
        </svg>
      </div>

      <blockquote className="rounded-md border-l-4 border-ochre bg-surface-0 p-3 font-mono text-xs leading-relaxed text-ink-1">
        {citation.excerpt}
      </blockquote>
      <KeyValue
        items={[
          ["chunk_id", citation.chunk_id],
          ["bbox", bboxText],
        ]}
      />
    </div>
  );
};
