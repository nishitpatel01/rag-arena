import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  PANE_STORAGE_PREFIX,
  clampPaneWidth,
  effectiveMax,
  parseStoredWidth,
  widthFromDrag,
  widthFromKey,
  type PaneBounds,
} from "../../lib/paneWidth";

function viewportWidth(): number | null {
  return typeof window === "undefined" ? null : window.innerWidth;
}

function readStored(key: string, bounds: PaneBounds): number {
  try {
    return parseStoredWidth(window.localStorage.getItem(PANE_STORAGE_PREFIX + key), bounds);
  } catch {
    return bounds.defaultPx; // storage disabled (private mode): fall back to the default width
  }
}

/** Width state for a right-anchored pane, persisted per key and re-clamped when the viewport shrinks. */
export function usePaneWidth(key: string, bounds: PaneBounds): [number, (px: number) => void] {
  const [width, setWidthState] = useState(() => clampPaneWidth(readStored(key, bounds), bounds, viewportWidth()));
  const setWidth = useCallback(
    (px: number) => {
      const next = clampPaneWidth(px, bounds, viewportWidth());
      setWidthState(next);
      try {
        window.localStorage.setItem(PANE_STORAGE_PREFIX + key, String(next));
      } catch {
        /* storage disabled: width still applies for this session */
      }
    },
    [key, bounds]
  );
  useEffect(() => {
    const onResize = (): void => setWidthState((w) => clampPaneWidth(w, bounds, viewportWidth()));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [bounds]);
  return [width, setWidth];
}

export interface PaneResizerProps {
  readonly label: string;
  readonly width: number;
  readonly bounds: PaneBounds;
  readonly onChange: (px: number) => void;
  readonly className?: string;
}

/**
 * Vertical separator on the LEFT edge of a right-anchored pane (xl+ only): drag, arrow keys
 * (Shift = ×4), Home/End, double-click resets to the default width.
 */
export const PaneResizer: React.FC<PaneResizerProps> = ({ label, width, bounds, onChange, className = "" }) => {
  const drag = useRef<{ startX: number; startWidth: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { startX: e.clientX, startWidth: width };
    setDragging(true);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current;
    if (d) onChange(widthFromDrag(d.startWidth, d.startX, e.clientX, bounds, viewportWidth()));
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (!drag.current) return;
    drag.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    const next = widthFromKey(width, e.key, e.shiftKey, bounds, viewportWidth());
    if (next === null) return;
    e.preventDefault();
    onChange(next);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={bounds.minPx}
      aria-valuemax={effectiveMax(bounds, viewportWidth())}
      aria-valuetext={`${width} pixels wide`}
      title={`${label} — drag, or use ←/→ (Shift ×4); double-click to reset`}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={() => onChange(bounds.defaultPx)}
      className={`group hidden w-3 shrink-0 cursor-col-resize touch-none select-none items-stretch justify-center outline-none xl:flex ${className}`}
      data-testid="pane-resizer"
    >
      <span
        aria-hidden
        className={`my-2 w-1 rounded-full transition-colors group-hover:bg-moss group-focus-visible:bg-moss ${dragging ? "bg-moss" : "bg-line"}`}
      />
    </div>
  );
};
