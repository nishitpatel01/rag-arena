/** Pure helpers for user-resizable side panes (width in CSS px, persisted in localStorage). */

export interface PaneBounds {
  /** Default width when nothing valid is stored. */
  readonly defaultPx: number;
  readonly minPx: number;
  readonly maxPx: number;
  /** Upper bound as a fraction of the viewport width (keeps the middle column usable). */
  readonly maxViewportFraction: number;
}

export const PANE_STORAGE_PREFIX = "rag-arena.pane.";
/** Keyboard step (ArrowLeft/ArrowRight); Shift multiplies by 4. */
export const PANE_KEY_STEP_PX = 16;

/** Document viewer (top-right column of a level page). */
export const DOC_PANE: PaneBounds = { defaultPx: 448, minPx: 272, maxPx: 800, maxViewportFraction: 0.45 };
/**
 * Node details (right of the architecture diagram). Default 360 px keeps the 6-column Level 4 diagram
 * free of horizontal scroll at a 1600 px viewport; wider is one drag away (the diagram then scrolls).
 */
export const NODE_PANE: PaneBounds = { defaultPx: 360, minPx: 256, maxPx: 720, maxViewportFraction: 0.45 };

/** Effective maximum: the smaller of the fixed cap and the viewport fraction, never below the minimum. */
export function effectiveMax(bounds: PaneBounds, viewportPx: number | null): number {
  if (viewportPx === null || !Number.isFinite(viewportPx) || viewportPx <= 0) return bounds.maxPx;
  return Math.max(bounds.minPx, Math.min(bounds.maxPx, Math.floor(viewportPx * bounds.maxViewportFraction)));
}

export function clampPaneWidth(px: number, bounds: PaneBounds, viewportPx: number | null = null): number {
  if (!Number.isFinite(px)) return bounds.defaultPx;
  return Math.round(Math.min(effectiveMax(bounds, viewportPx), Math.max(bounds.minPx, px)));
}

/** Stored value → width; anything missing or malformed → default (never throws). */
export function parseStoredWidth(raw: string | null, bounds: PaneBounds): number {
  if (raw === null || !/^\d{2,4}$/.test(raw.trim())) return bounds.defaultPx;
  return clampPaneWidth(Number(raw.trim()), bounds);
}

/**
 * Drag maths for a pane anchored on the RIGHT: moving the handle left (smaller clientX) widens it.
 */
export function widthFromDrag(startWidth: number, startX: number, currentX: number, bounds: PaneBounds, viewportPx: number | null): number {
  return clampPaneWidth(startWidth + (startX - currentX), bounds, viewportPx);
}

/** Keyboard resize; returns null for keys the separator does not handle. */
export function widthFromKey(current: number, key: string, shift: boolean, bounds: PaneBounds, viewportPx: number | null): number | null {
  const step = PANE_KEY_STEP_PX * (shift ? 4 : 1);
  switch (key) {
    case "ArrowLeft":
      return clampPaneWidth(current + step, bounds, viewportPx);
    case "ArrowRight":
      return clampPaneWidth(current - step, bounds, viewportPx);
    case "Home":
      return clampPaneWidth(bounds.minPx, bounds, viewportPx);
    case "End":
      return effectiveMax(bounds, viewportPx);
    default:
      return null;
  }
}
