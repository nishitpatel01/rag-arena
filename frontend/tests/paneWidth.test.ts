import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DOC_PANE,
  MIN_MIDDLE_PX,
  NODE_PANE,
  QUESTION_PANE,
  clampPaneWidth,
  paneMax,
  parseStoredWidth,
  sideMaxima,
  widthFromDrag,
  widthFromKey,
} from "../src/lib/paneWidth";

test("defaults: doc and node panes wider than the old fixed 21rem / 20rem; question keeps 18rem", () => {
  assert.ok(DOC_PANE.defaultPx > 21 * 16);
  assert.ok(NODE_PANE.defaultPx > 20 * 16);
  assert.equal(QUESTION_PANE.defaultPx, 18 * 16);
});

test("clamp respects min and the effective max", () => {
  assert.equal(clampPaneWidth(100, DOC_PANE), DOC_PANE.minPx);
  assert.equal(clampPaneWidth(5000, DOC_PANE), DOC_PANE.maxPx);
  assert.equal(clampPaneWidth(700, DOC_PANE, 500), 500);
  assert.equal(clampPaneWidth(700, DOC_PANE, 100), DOC_PANE.minPx, "a cap below min never shrinks the pane below min");
  assert.equal(clampPaneWidth(Number.NaN, DOC_PANE), DOC_PANE.defaultPx);
  assert.equal(paneMax(DOC_PANE, null), DOC_PANE.maxPx);
  assert.equal(paneMax(DOC_PANE, 600.7), 600);
  assert.equal(paneMax(DOC_PANE, 10), DOC_PANE.minPx);
});

test("stored values are validated; malformed → default", () => {
  assert.equal(parseStoredWidth(null, NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("abc", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("-50", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("1e9", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("512", NODE_PANE), 512);
  assert.equal(parseStoredWidth("9999", NODE_PANE), NODE_PANE.maxPx);
});

test("both sides share the room left after the middle column's minimum", () => {
  // 1520 px grid, two 16 px gaps → room = 1520 - 32 - 480 = 1008
  const m = sideMaxima(1520, 288, 448, 32);
  assert.equal(m.leftAvailable, 1008 - 448);
  assert.equal(m.rightAvailable, 1008 - 288);
  // Growing the left pane to its cap leaves the right pane only what is left → middle stays ≥ MIN_MIDDLE_PX.
  const left = clampPaneWidth(5000, QUESTION_PANE, paneMax(QUESTION_PANE, m.leftAvailable));
  const right = clampPaneWidth(5000, DOC_PANE, paneMax(DOC_PANE, sideMaxima(1520, left, 448, 32).rightAvailable));
  assert.ok(1520 - 32 - left - right >= MIN_MIDDLE_PX, `middle ${1520 - 32 - left - right}`);
  assert.deepEqual(sideMaxima(null, 288, 448, 32), { leftAvailable: null, rightAvailable: null });
});

test("drag: right pane widens moving left, left pane widens moving right", () => {
  assert.equal(widthFromDrag(400, 1000, 900, "right", NODE_PANE, 720), 500);
  assert.equal(widthFromDrag(400, 1000, 1100, "right", NODE_PANE, 720), 300);
  assert.equal(widthFromDrag(288, 300, 400, "left", QUESTION_PANE, 560), 388);
  assert.equal(widthFromDrag(288, 300, 100, "left", QUESTION_PANE, 560), QUESTION_PANE.minPx);
  assert.equal(widthFromDrag(288, 300, 2000, "left", QUESTION_PANE, 450), 450);
});

test("keyboard: arrow toward the middle widens (×4 with shift); Home/End; other keys ignored", () => {
  assert.equal(widthFromKey(400, "ArrowLeft", false, "right", NODE_PANE, 720), 416);
  assert.equal(widthFromKey(400, "ArrowRight", true, "right", NODE_PANE, 720), 336);
  assert.equal(widthFromKey(288, "ArrowRight", false, "left", QUESTION_PANE, 560), 304);
  assert.equal(widthFromKey(288, "ArrowLeft", true, "left", QUESTION_PANE, 560), 224);
  assert.equal(widthFromKey(400, "Home", false, "right", NODE_PANE, 720), NODE_PANE.minPx);
  assert.equal(widthFromKey(400, "End", false, "right", NODE_PANE, 600), 600);
  assert.equal(widthFromKey(400, "Enter", false, "right", NODE_PANE, 720), null);
});
