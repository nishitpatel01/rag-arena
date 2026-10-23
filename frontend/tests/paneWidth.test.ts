import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DOC_PANE,
  NODE_PANE,
  clampPaneWidth,
  effectiveMax,
  parseStoredWidth,
  widthFromDrag,
  widthFromKey,
} from "../src/lib/paneWidth";

test("defaults are wider than the old fixed panes (21rem doc / 20rem node)", () => {
  assert.ok(DOC_PANE.defaultPx > 21 * 16);
  assert.ok(NODE_PANE.defaultPx > 20 * 16);
});

test("clamp respects min, fixed max and the viewport fraction", () => {
  assert.equal(clampPaneWidth(100, DOC_PANE), DOC_PANE.minPx);
  assert.equal(clampPaneWidth(5000, DOC_PANE), DOC_PANE.maxPx);
  assert.equal(effectiveMax(DOC_PANE, 1280), Math.floor(1280 * 0.45));
  assert.equal(clampPaneWidth(5000, DOC_PANE, 1280), 576);
  assert.equal(effectiveMax(DOC_PANE, 400), DOC_PANE.minPx, "tiny viewport never drops below min");
  assert.equal(clampPaneWidth(Number.NaN, DOC_PANE), DOC_PANE.defaultPx);
});

test("stored values are validated; malformed → default", () => {
  assert.equal(parseStoredWidth(null, NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("abc", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("-50", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("1e9", NODE_PANE), NODE_PANE.defaultPx);
  assert.equal(parseStoredWidth("512", NODE_PANE), 512);
  assert.equal(parseStoredWidth("9999", NODE_PANE), NODE_PANE.maxPx);
});

test("dragging the handle left widens a right-anchored pane", () => {
  assert.equal(widthFromDrag(400, 1000, 900, NODE_PANE, 1600), 500);
  assert.equal(widthFromDrag(400, 1000, 1100, NODE_PANE, 1600), 300);
  assert.equal(widthFromDrag(400, 1000, 0, NODE_PANE, 1600), 720);
});

test("keyboard: arrows step 16 px (×4 with shift), Home/End jump to bounds, other keys ignored", () => {
  assert.equal(widthFromKey(400, "ArrowLeft", false, NODE_PANE, 1600), 416);
  assert.equal(widthFromKey(400, "ArrowRight", true, NODE_PANE, 1600), 336);
  assert.equal(widthFromKey(400, "Home", false, NODE_PANE, 1600), NODE_PANE.minPx);
  assert.equal(widthFromKey(400, "End", false, NODE_PANE, 1600), 720);
  assert.equal(widthFromKey(400, "Enter", false, NODE_PANE, 1600), null);
});
