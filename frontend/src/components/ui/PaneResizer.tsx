import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  PANE_STORAGE_PREFIX,
  clampPaneWidth,
  parseStoredWidth,
  widthFromDrag,
  widthFromKey,
  type PaneBounds,
  type PaneSide,
} from "../../lib/paneWidth";

function readStored(key: string, bounds: PaneBounds): number {
  try {
    return parseStoredWidth(window.localStorage.getItem(PANE_STORAGE_PREFIX + key), bounds);
  } catch {
    return bounds.defaultPx; // storage disabled (private mode): fall back to the default width
  }
}

/**
 * Preferred width of a pane, persisted per key. The rendered width is `Math.min(preferred, max)` at the
 * call site, so shrinking the window narrows panes without overwriting the user's preference.
 */
export function usePaneWidth(key: string, bounds: PaneBounds): [number, (px: number) => void] {
  const [width, setWidthState] = useState(() => readStored(key, bounds));
  const setWidth = useCallback(
    (px: number) => {
      const next = clampPaneWidth(px, bounds);
      setWidthState(next);
      try {
        window.localStorage.setItem(PANE_STORAGE_PREFIX + key, String(next));
      } catch {
        /* storage disabled: width still applies for this session */
      }
    },
    [key, bounds]
  );
  return [width, setWidth];
}

/** Element width via ResizeObserver (disconnected on unmount); null until measured. */
export function useElementWidth<T extends HTMLElement>(): [React.RefObject<T>, number | null] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState<number | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export interface PaneResizerProps {
  readonly label: string;
  /** Which side of the layout the pane sits on (the handle is on its inner edge). */
  readonly side: PaneSide;
  /** Rendered width of the pane. */
  readonly width: number;
  readonly bounds: PaneBounds;
  /** Current effective maximum (fixed cap and the room the middle column needs). */
  readonly maxPx: number;
  readonly onChange: (px: number) => void;
  readonly className?: string;
}

/**
 * Vertical separator on the inner edge of a side pane (xl+ only): drag, arrow keys (toward the middle
 * widens; Shift = ×4), Home/End, double-click resets to the default width.
 */
export const PaneResizer: React.FC<PaneResizerProps> = ({ label, side, width, bounds, maxPx, onChange, className = "" }) => {
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
    if (d) onChange(widthFromDrag(d.startWidth, d.startX, e.clientX, side, bounds, maxPx));
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (!drag.current) return;
    drag.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    const next = widthFromKey(width, e.key, e.shiftKey, side, bounds, maxPx);
    if (next === null) return;
    e.preventDefault();
    onChange(next);
  };
  const widen = side === "right" ? "←" : "→";

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={bounds.minPx}
      aria-valuemax={Math.max(bounds.minPx, maxPx)}
      aria-valuetext={`${width} pixels wide`}
      title={`${label} — drag, or ${widen} to widen (Shift ×4); double-click to reset`}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={() => onChange(bounds.defaultPx)}
      className={`group hidden w-3 shrink-0 cursor-col-resize touch-none select-none items-stretch justify-center outline-none xl:flex ${className}`}
      data-testid={`pane-resizer-${side}`}
    >
      <span
        aria-hidden
        className={`my-2 w-1 rounded-full transition-colors group-hover:bg-moss group-focus-visible:bg-moss ${dragging ? "bg-moss" : "bg-line"}`}
      />
    </div>
  );
};
