/** Pure helpers for user-resizable side panes (width in CSS px, persisted in localStorage). */

export type PaneSide = "left" | "right";

export interface PaneBounds {
  /** Default width when nothing valid is stored. */
  readonly defaultPx: number;
  readonly minPx: number;
  /** Absolute cap; the effective cap is also limited by the space the middle column needs. */
  readonly maxPx: number;
}

export const PANE_STORAGE_PREFIX = "rag-arena.pane.";
/** Keyboard step (arrow keys); Shift multiplies by 4. */
export const PANE_KEY_STEP_PX = 16;

/** Question (left column of a level page). */
export const QUESTION_PANE: PaneBounds = { defaultPx: 288, minPx: 224, maxPx: 560 };
/** Document viewer (right column of a level page). */
export const DOC_PANE: PaneBounds = { defaultPx: 448, minPx: 272, maxPx: 800 };
/**
 * Node details (right of the architecture diagram). Default 360 px keeps the 6-column Level 4 diagram
 * free of horizontal scroll at a 1600 px viewport; wider is one drag away (the diagram then scrolls).
 */
export const NODE_PANE: PaneBounds = { defaultPx: 360, minPx: 256, maxPx: 720 };

/** The middle column of a level page (stage rail / stage detail / answer) never gets narrower than this. */
export const MIN_MIDDLE_PX = 480;
/** The architecture diagram keeps at least this much width next to Node details. */
export const MIN_DIAGRAM_PX = 480;

/** Effective cap: min(fixed cap, available space), never below the minimum. `null` = unknown space. */
export function paneMax(bounds: PaneBounds, availablePx: number | null): number {
  if (availablePx === null || !Number.isFinite(availablePx)) return bounds.maxPx;
  return Math.max(bounds.minPx, Math.min(bounds.maxPx, Math.floor(availablePx)));
}

export function clampPaneWidth(px: number, bounds: PaneBounds, maxPx: number = bounds.maxPx): number {
  if (!Number.isFinite(px)) return Math.min(bounds.defaultPx, Math.max(bounds.minPx, maxPx));
  const cap = Math.max(bounds.minPx, maxPx);
  return Math.round(Math.min(cap, Math.max(bounds.minPx, px)));
}

/** Stored value → width; anything missing or malformed → default (never throws). */
export function parseStoredWidth(raw: string | null, bounds: PaneBounds): number {
  if (raw === null || !/^\d{2,4}$/.test(raw.trim())) return bounds.defaultPx;
  return clampPaneWidth(Number(raw.trim()), bounds);
}

/**
 * Space each side pane may take so the middle column keeps `minMiddlePx`.
 * `gridPx` is the full grid width, `gapsPx` the total column gaps, `leftPx`/`rightPx` the current widths.
 */
export function sideMaxima(
  gridPx: number | null,
  leftPx: number,
  rightPx: number,
  gapsPx: number,
  minMiddlePx: number = MIN_MIDDLE_PX
): { readonly leftAvailable: number | null; readonly rightAvailable: number | null } {
  if (gridPx === null || !Number.isFinite(gridPx) || gridPx <= 0) return { leftAvailable: null, rightAvailable: null };
  const room = gridPx - gapsPx - minMiddlePx;
  return { leftAvailable: room - rightPx, rightAvailable: room - leftPx };
}

/**
 * Drag maths. A RIGHT pane (handle on its left edge) widens when the pointer moves left;
 * a LEFT pane (handle on its right edge) widens when the pointer moves right.
 */
export function widthFromDrag(startWidth: number, startX: number, currentX: number, side: PaneSide, bounds: PaneBounds, maxPx: number): number {
  const delta = side === "right" ? startX - currentX : currentX - startX;
  return clampPaneWidth(startWidth + delta, bounds, maxPx);
}

/** Keyboard resize (arrow toward the middle widens); returns null for keys the separator ignores. */
export function widthFromKey(current: number, key: string, shift: boolean, side: PaneSide, bounds: PaneBounds, maxPx: number): number | null {
  const step = PANE_KEY_STEP_PX * (shift ? 4 : 1);
  const widenKey = side === "right" ? "ArrowLeft" : "ArrowRight";
  const narrowKey = side === "right" ? "ArrowRight" : "ArrowLeft";
  if (key === widenKey) return clampPaneWidth(current + step, bounds, maxPx);
  if (key === narrowKey) return clampPaneWidth(current - step, bounds, maxPx);
  if (key === "Home") return bounds.minPx;
  if (key === "End") return clampPaneWidth(maxPx, bounds, maxPx);
  return null;
}
