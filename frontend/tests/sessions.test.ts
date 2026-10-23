/**
 * Persistent run-state tests — run with `npm test`.
 * A page's BattleSession must keep its events after the page unsubscribes (route change), stream on
 * without aborting, stay independent per key, and only end on abort / reset / a new run. The registry
 * is bounded and never evicts a streaming session.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BattleSession,
  MAX_UI_FIELDS,
  SessionRegistry,
  readUi,
  sameKindAs,
  type OpenTransport,
  type TransportCallbacks,
} from "../src/features/levels/battleSession";

const T = "2026-10-23T15:00:00.000Z";

interface FakeRun {
  readonly cb: TransportCallbacks;
  aborted: boolean;
}

function fakeTransport(): { open: OpenTransport; runs: FakeRun[] } {
  const runs: FakeRun[] = [];
  const open: OpenTransport = (_params, cb) => {
    const run: FakeRun = { cb, aborted: false };
    runs.push(run);
    return { abort: () => (run.aborted = true) };
  };
  return { open, runs };
}

const PARAMS = { query: "q", engines: ["level2_hybrid"] };
const runStarted = (run: string) => ({ event: "run_started", run_id: run, engines: ["level2_hybrid"], demo_mode: "replay", question_id: "q1", ts: T });
const stageStarted = (run: string) => ({ event: "stage_started", run_id: run, engine: "level2_hybrid", stage: "retrieve", label: "retrieve", service: "svc", request: {}, ts: T });
const complete = (run: string) => ({ event: "battle_complete", run_id: run, ts: T });

test("unsubscribing (page unmount) neither aborts the stream nor drops events; frames keep arriving", () => {
  const { open, runs } = fakeTransport();
  const s = new BattleSession(open);
  let calls = 0;
  const unsubscribe = s.subscribe(() => (calls += 1));
  s.run(PARAMS);
  runs[0].cb.onFrame(runStarted("r1"));
  unsubscribe();
  assert.equal(runs[0].aborted, false);
  runs[0].cb.onFrame(stageStarted("r1"));
  runs[0].cb.onFrame(complete("r1"));
  runs[0].cb.onEnd();
  const snap = s.getSnapshot();
  assert.equal(snap.state.events.length, 3);
  assert.equal(snap.state.complete, true);
  assert.equal(snap.isStreaming, false);
  assert.equal(snap.hasContent, true);
  assert.ok(calls >= 2);
});

test("separate keys are independent sessions; both may stream at once", () => {
  const { open, runs } = fakeTransport();
  const reg = new SessionRegistry(() => new BattleSession(open));
  const a = reg.get("level:level2_hybrid");
  const b = reg.get("compare");
  assert.notEqual(a, b);
  assert.equal(reg.get("compare"), b);
  a.run(PARAMS);
  b.run(PARAMS);
  runs[0].cb.onFrame(runStarted("ra"));
  assert.equal(a.getSnapshot().state.runId, "ra");
  assert.equal(b.getSnapshot().state.runId, null);
  assert.equal(a.isStreaming && b.isStreaming, true);
  a.setUi("query", "only a");
  assert.equal(b.getUi("query"), undefined);
});

test("reset stops a streaming run and clears results + ui; late frames are ignored", () => {
  const { open, runs } = fakeTransport();
  const s = new BattleSession(open);
  s.setUi("query", "What is CLIN 0001?");
  s.run(PARAMS);
  runs[0].cb.onFrame(runStarted("r1"));
  s.reset();
  assert.equal(runs[0].aborted, true);
  runs[0].cb.onFrame(stageStarted("r1"));
  runs[0].cb.onEnd();
  const snap = s.getSnapshot();
  assert.equal(snap.isStreaming, false);
  assert.equal(snap.state.events.length, 0);
  assert.equal(snap.transportError, null);
  assert.deepEqual(snap.ui, {});
  assert.equal(snap.hasContent, false);
});

test("a new run replaces the previous run's events and keeps the ui (question) record", () => {
  const { open, runs } = fakeTransport();
  const s = new BattleSession(open);
  s.setUi("query", "q");
  s.run(PARAMS);
  runs[0].cb.onFrame(runStarted("r1"));
  runs[0].cb.onFrame(complete("r1"));
  runs[0].cb.onEnd();
  s.run(PARAMS);
  assert.equal(s.getSnapshot().state.events.length, 0);
  runs[0].cb.onFrame(runStarted("stale")); // superseded run: ignored
  runs[1].cb.onFrame(runStarted("r2"));
  assert.equal(s.getSnapshot().state.runId, "r2");
  assert.equal(s.getSnapshot().state.events.length, 1);
  assert.equal(s.getUi("query"), "q");
});

test("abort marks the run interrupted; transport errors surface", () => {
  const { open, runs } = fakeTransport();
  const s = new BattleSession(open);
  s.run(PARAMS);
  runs[0].cb.onFrame(runStarted("r1"));
  runs[0].cb.onFrame(stageStarted("r1"));
  s.abort();
  assert.equal(runs[0].aborted, true);
  assert.ok(s.getSnapshot().state.interrupted);
  s.run(PARAMS);
  runs[1].cb.onError(new Error("HTTP 400: bad model"));
  runs[1].cb.onEnd();
  assert.equal(s.getSnapshot().transportError, "HTTP 400: bad model");
  assert.equal(s.getSnapshot().isStreaming, false);
});

test("registry is bounded (LRU) and never evicts a streaming session", () => {
  const { open } = fakeTransport();
  const reg = new SessionRegistry(() => new BattleSession(open), 3);
  const a = reg.get("a");
  a.run(PARAMS); // a streams
  reg.get("b");
  reg.get("c");
  reg.get("d"); // evicts b (oldest non-streaming), not a
  assert.equal(reg.size, 3);
  assert.equal(reg.has("a"), true);
  assert.equal(reg.has("b"), false);
  reg.get("c"); // touch c → d is now the LRU non-streaming
  reg.get("e");
  assert.equal(reg.has("d"), false);
  assert.equal(reg.has("c"), true);
  assert.equal(reg.get("a"), a);
});

test("registry fails loud when every session is streaming", () => {
  const { open } = fakeTransport();
  const reg = new SessionRegistry(() => new BattleSession(open), 2);
  reg.get("a").run(PARAMS);
  reg.get("b").run(PARAMS);
  assert.throws(() => reg.get("c"), /all 2 sessions are streaming/);
  assert.throws(() => reg.get(""), /invalid session key/);
});

test("ui values are type-validated on read and the record is bounded", () => {
  const { open } = fakeTransport();
  const s = new BattleSession(open);
  s.setUi("query", 42);
  const ui = s.getSnapshot().ui;
  assert.equal(readUi(ui, "query", "", sameKindAs("")), "");
  assert.equal(readUi(ui, "missing", "init", sameKindAs("init")), "init");
  s.setUi("levels", ["a", "b"]);
  assert.deepEqual(readUi(s.getSnapshot().ui, "levels", [] as string[], sameKindAs([] as string[])), ["a", "b"]);
  s.setUi("flag", "yes");
  assert.equal(readUi(s.getSnapshot().ui, "flag", false, sameKindAs(false)), false);
  s.setUi("query", undefined);
  assert.equal("query" in s.getSnapshot().ui, false);
  assert.throws(() => s.setUi("bad field!", "x"), /invalid field name/);
  for (let i = s.getSnapshot().ui ? Object.keys(s.getSnapshot().ui).length : 0; i < MAX_UI_FIELDS; i += 1) s.setUi(`f${i}`, i);
  assert.throws(() => s.setUi("oneMore", 1), /more than 32 ui fields/);
  assert.throws(() => sameKindAs(null), /explicit guard/);
});

test("snapshot identity is stable when nothing changed and changes on every real change", () => {
  const { open, runs } = fakeTransport();
  const s = new BattleSession(open);
  let notified = 0;
  s.subscribe(() => (notified += 1));
  const first = s.getSnapshot();
  assert.equal(s.getSnapshot(), first);
  s.setUi("query", "x");
  const second = s.getSnapshot();
  assert.notEqual(second, first);
  s.setUi("query", "x"); // same value: no change
  s.clearUi();
  const third = s.getSnapshot();
  s.clearUi(); // already empty: no change
  s.abort(); // idle abort on initial state: reducer no-op
  assert.equal(s.getSnapshot(), third);
  assert.equal(notified, 2);
  s.run(PARAMS);
  assert.notEqual(s.getSnapshot(), third);
  runs[0].cb.onFrame(runStarted("r1"));
  assert.equal(s.getSnapshot().state.runId, "r1");
});
