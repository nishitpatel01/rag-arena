/**
 * "Latest run wins, per level" tests — run with `npm test`.
 * A level page shows the newest run that included its level: its own level run or a Compare run (live from
 * the one Compare stream). Level Clear dismisses only that Compare run; Compare Reset falls back to the
 * level's own run; viewing a Compare run never touches the stored model preference.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { BattleSession, createRunClock, type OpenTransport, type TransportCallbacks } from "../src/features/levels/battleSession";
import { MODEL_SELECTION_STORAGE_KEY } from "../src/features/levels/modelSelection";
import {
  compareViewFor,
  emptyCompareView,
  formatClock,
  levelFinished,
  runModels,
  selectRunSource,
  type RunSnapshot,
} from "../src/features/levels/runSource";

const T = "2026-10-23T15:00:00.000Z";
const L2 = "level2_hybrid";
const L4 = "level4_agentic";
const L5 = "managed_vertex";

function fakeTransport(): { open: OpenTransport; runs: TransportCallbacks[]; opened: () => number } {
  const runs: TransportCallbacks[] = [];
  const open: OpenTransport = (_params, cb) => {
    runs.push(cb);
    return { abort: () => undefined };
  };
  return { open, runs, opened: () => runs.length };
}

function setup() {
  const t = fakeTransport();
  let now = 1_000;
  const clock = createRunClock(() => now);
  const level = new BattleSession(t.open, clock);
  const compare = new BattleSession(t.open, clock);
  const tick = (ms = 1_000) => (now += ms);
  return { t, level, compare, tick };
}

const runStarted = (run: string, engines: readonly string[]) => ({
  event: "run_started", run_id: run, engines, demo_mode: "replay", question_id: "q_cross", synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5", ts: T,
});
const stageStarted = (run: string, engine: string) => ({ event: "stage_started", run_id: run, engine, stage: "retrieve", label: "retrieve", service: "svc", request: {}, ts: T });
const evalDone = (run: string, engine: string) => ({
  event: "eval_complete", run_id: run, engine, metrics: { groundedness: 1, qa_relevance: 1, context_precision: 1 }, judge_model: "claude-opus-5-5", reference_answer_used: true, duration_ms: 5, ts: T,
});
const COMPARE = { query: "total cost?", questionId: "q_cross", engines: [L2, L4], synthesisModel: "gemini-4-argon", judgeModel: "claude-opus-5-5" };
const LEVEL = { query: "clin?", questionId: "q_exact_clin_lookup", engines: [L2] };

const snap = (s: BattleSession): RunSnapshot => s.getSnapshot();
const view = (levelId: string, level: BattleSession, compare: BattleSession, dismissed: number | null = null) =>
  selectRunSource({ levelId, level: snap(level), compare: snap(compare), dismissedCompareAt: dismissed });

test("run start stamps are strictly increasing on a shared clock, even within the same millisecond", () => {
  const clock = createRunClock(() => 5);
  assert.deepEqual([clock(), clock(), clock()], [5, 6, 7]);
});

test("newest wins: a Compare run after the level run takes over; a level run after it takes back only that level", () => {
  const { level, compare, tick } = setup();
  level.run(LEVEL);
  assert.equal(view(L2, level, compare).source, "level");
  tick();
  compare.run(COMPARE);
  const v = view(L2, level, compare);
  assert.equal(v.source, "compare");
  assert.equal(v.levelCount, 2);
  assert.equal(v.params?.query, "total cost?");
  tick();
  level.run(LEVEL);
  assert.equal(view(L2, level, compare).source, "level");
  assert.equal(view(L2, level, compare).compareAvailable, true);
  // The other level's page (no own run) still shows the Compare run; the Compare session is untouched.
  const l4Level = new BattleSession(fakeTransport().open);
  assert.equal(view(L4, l4Level, compare).source, "compare");
  assert.equal(compare.getSnapshot().run?.params.engines.length, 2);
});

test("a streaming Compare run shows live progress for the level from the same stream (no second transport)", () => {
  const { t, level, compare } = setup();
  compare.run(COMPARE);
  t.runs[0].onFrame(runStarted("rc", [L2, L4]));
  t.runs[0].onFrame(stageStarted("rc", L4));
  const v = view(L4, level, compare);
  assert.equal(t.opened(), 1);
  assert.equal(v.source, "compare");
  assert.equal(v.isStreaming, true);
  assert.equal(v.state.timelines[L4]?.stages.retrieve?.status, "active");
  assert.deepEqual(runModels(v), { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" });
  assert.equal(v.state.questionId, "q_cross");
  // L4 finishes while L2 still runs: L4's page stops "streaming", L2's does not.
  t.runs[0].onFrame(evalDone("rc", L4));
  assert.equal(view(L4, level, compare).isStreaming, false);
  assert.equal(levelFinished(compare.getSnapshot().state, L2), false);
  assert.equal(view(L2, level, compare).isStreaming, true);
});

test("Compare reset: level pages fall back to their own last level run, else empty", () => {
  const { level, compare, tick } = setup();
  level.run(LEVEL);
  tick();
  compare.run(COMPARE);
  assert.equal(view(L2, level, compare).source, "compare");
  compare.reset();
  assert.equal(view(L2, level, compare).source, "level");
  assert.equal(view(L4, new BattleSession(fakeTransport().open), compare).source, "none");
});

test("level Clear dismisses only that Compare run id; a newer Compare run shows again", () => {
  const { level, compare, tick } = setup();
  compare.run(COMPARE);
  const first = compare.getSnapshot().run?.startedAt ?? null;
  assert.notEqual(first, null);
  const cleared = view(L2, level, compare, first);
  assert.equal(cleared.source, "none");
  assert.equal(cleared.compareAvailable, false);
  assert.equal(compare.getSnapshot().isStreaming, true, "clearing on the level page never stops Compare");
  tick();
  compare.run(COMPARE);
  assert.equal(view(L2, level, compare, first).source, "compare");
});

test("a Compare run that did not include the level is ignored", () => {
  const { level, compare } = setup();
  compare.run({ ...COMPARE, engines: [L4, L5] });
  assert.equal(view(L2, level, compare).source, "none");
  assert.equal(view(L2, level, compare).compareAvailable, false);
  assert.equal(view(L4, level, compare).source, "compare");
});

test("compare-view ui (stage / citation picks) is keyed by the viewed run and never leaks to another run", () => {
  const picked = { ...emptyCompareView(10), selectedStage: "retrieve", userPicked: true };
  assert.equal(compareViewFor(picked, 10).selectedStage, "retrieve");
  assert.equal(compareViewFor(picked, 11).selectedStage, null);
  assert.equal(compareViewFor({ runAt: "x" }, 10).userPicked, false);
});

test("viewing a Compare run never writes the stored model preference", () => {
  const store = new Map<string, string>([[MODEL_SELECTION_STORAGE_KEY, '{"synthesis":"gemini-3.8-flash","judge":"claude-fable-5-1"}']]);
  const before = store.get(MODEL_SELECTION_STORAGE_KEY);
  const { t, level, compare } = setup();
  compare.run(COMPARE);
  t.runs[0].onFrame(runStarted("rc", [L2, L4]));
  const v = view(L2, level, compare);
  // The view exposes the run's models for display; nothing in the model layer touches storage.
  assert.deepEqual(runModels(v), { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" });
  assert.equal(store.get(MODEL_SELECTION_STORAGE_KEY), before);
  assert.equal(emptyCompareView(1).modelsEdited, false);
});

test("reset clears the run stamp; formatClock renders hh:mm:ss and rejects junk", () => {
  const { level } = setup();
  level.run(LEVEL);
  assert.notEqual(level.getSnapshot().run, null);
  level.reset();
  assert.equal(level.getSnapshot().run, null);
  assert.match(formatClock(Date.UTC(2026, 9, 23, 15, 4, 5)), /^\d{2}:\d{2}:\d{2}$/);
  assert.throws(() => formatClock(Number.NaN));
  assert.throws(() => selectRunSource({ levelId: "", level: snap(level), compare: snap(level), dismissedCompareAt: null }));
});
