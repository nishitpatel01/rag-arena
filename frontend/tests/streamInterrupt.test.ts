/**
 * Stream-interruption tests — run with `npm test` (esbuild bundle → plain `node`).
 * When a battle or ingest SSE stream ends (reader done, network error, abort) without its terminal
 * event, the synthetic client event `stream_interrupted` must fail every `active` stage loudly, stop
 * its timer (status no longer `active`), and never mark anything `done`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  INITIAL_STREAM_STATE,
  STREAM_INTERRUPTED_ERROR_TYPE,
  activeStageId,
  interruptionMessage,
  isStreamTerminated,
  reduceStreamEvent,
  reduceStreamEvents,
  streamEndReason,
  streamInterruptedEvent,
  type TimelineEvent,
} from "../src/features/levels/model";

const RUN = "run_x";
const E = "level2_hybrid";
const T = "2026-10-21T19:00:00.000Z";

const runStarted: TimelineEvent = { event: "run_started", run_id: RUN, engines: [E], demo_mode: "live", question_id: "q1", ts: T };
const started = (stage: string): TimelineEvent => ({ event: "stage_started", run_id: RUN, engine: E, stage, label: stage, service: "svc", request: {}, ts: T });
const completed = (stage: string, duration_ms: number): TimelineEvent => ({ event: "stage_completed", run_id: RUN, engine: E, stage, duration_ms, payload: {}, ts: T });

test("battle stream cut mid-stage: the active stage fails with the interruption message; done stays done", () => {
  const before = reduceStreamEvents([runStarted, started("retrieve"), completed("retrieve", 900), started("evaluate")]);
  assert.equal(activeStageId(before.timelines[E]), "evaluate");
  const after = reduceStreamEvent(before, streamInterruptedEvent(RUN, "server closed the stream"));
  const ev = after.timelines[E].stages.evaluate;
  assert.equal(ev.status, "failed");
  assert.equal(ev.durationMs, null, "no client-invented duration");
  assert.equal(ev.error?.type, STREAM_INTERRUPTED_ERROR_TYPE);
  assert.equal(ev.error?.message, "Stream ended before completion (server closed the stream) — server restarted or connection lost");
  assert.equal(after.timelines[E].stages.retrieve.status, "done");
  assert.equal(after.timelines[E].stages.retrieve.durationMs, 900);
  assert.equal(activeStageId(after.timelines[E]), null, "elapsed timer stops: nothing active");
  assert.deepEqual(after.interrupted, { reason: "server closed the stream" });
  assert.equal(after.complete, false, "never reported as complete");
  assert.equal(after.events.length, before.events.length + 1, "synthetic event is kept in the log");
});

test("interruption never marks any stage done and leaves idle/skipped stages untouched", () => {
  const s = reduceStreamEvents([
    runStarted,
    started("query_prep"),
    { event: "stage_skipped", run_id: RUN, engine: E, stage: "embed_query", reason: "n/a", ts: T },
    started("rerank"),
    streamInterruptedEvent(RUN, "network error: Failed to fetch"),
  ]);
  const st = s.timelines[E].stages;
  assert.equal(st.query_prep.status, "failed");
  assert.equal(st.rerank.status, "failed");
  assert.equal(st.embed_query.status, "skipped");
  assert.ok(Object.values(st).every((x) => x.status !== "done"));
  assert.equal(st.synthesize, undefined, "unstarted stages are not invented");
});

test("no-op after battle_complete, after engine_error for every engine, before any event, and when repeated", () => {
  const done = reduceStreamEvents([runStarted, started("retrieve"), completed("retrieve", 5), { event: "battle_complete", run_id: RUN, engines_completed: [E], engines_failed: [], ts: T }]);
  assert.equal(isStreamTerminated(done), true);
  assert.equal(reduceStreamEvent(done, streamInterruptedEvent(RUN, "x")), done);

  const errored = reduceStreamEvents([runStarted, started("rerank"), { event: "stage_failed", run_id: RUN, engine: E, stage: "rerank", duration_ms: 3, error_type: "PermissionDenied", error: "denied", ts: T }, { event: "engine_error", run_id: RUN, engine: E, error_type: "PermissionDenied", error: "denied", failed_stage: "rerank", ts: T }]);
  assert.equal(isStreamTerminated(errored), true);
  assert.equal(reduceStreamEvent(errored, streamInterruptedEvent(RUN, "x")), errored);

  assert.equal(reduceStreamEvent(INITIAL_STREAM_STATE, streamInterruptedEvent(null, "x")), INITIAL_STREAM_STATE, "nothing started → nothing to fail");

  const once = reduceStreamEvents([runStarted, started("synthesize"), streamInterruptedEvent(RUN, "first")]);
  assert.equal(reduceStreamEvent(once, streamInterruptedEvent(RUN, "second")), once);
  assert.deepEqual(once.interrupted, { reason: "first" });
});

test("ingest stream cut mid-stage fails the active stage; job_complete and job_failed are terminal", () => {
  const J = "ing_1";
  const jobStarted: TimelineEvent = { event: "job_started", job_id: J, level: E, tier_profile: "hybrid", ts: T };
  const jStarted = (stage: string): TimelineEvent => ({ event: "stage_started", job_id: J, engine: E, stage, label: stage, service: "svc", request: {}, ts: T });
  const cut = reduceStreamEvents([jobStarted, jStarted("embed"), streamInterruptedEvent(J, "server closed the stream")]);
  assert.equal(cut.timelines[E].stages.embed.status, "failed");
  assert.equal(cut.timelines[E].stages.embed.error?.type, STREAM_INTERRUPTED_ERROR_TYPE);

  const ok = reduceStreamEvents([jobStarted, jStarted("verify_counts"), { event: "stage_completed", job_id: J, engine: E, stage: "verify_counts", duration_ms: 4, payload: {}, ts: T }, { event: "job_complete", job_id: J, counts_by_tier: { hybrid: 526 }, index_states: {}, ts: T }]);
  assert.equal(reduceStreamEvent(ok, streamInterruptedEvent(J, "x")), ok);

  const failed = reduceStreamEvents([jobStarted, jStarted("load_bigquery"), { event: "job_failed", job_id: J, engine: E, error_type: "Forbidden", error: "403 bq", failed_stage: "load_bigquery", ts: T }]);
  assert.equal(isStreamTerminated(failed), true);
  assert.equal(failed.complete, false, "job_failed is terminal but not complete");
  assert.deepEqual(failed.timelines[E].error, { type: "Forbidden", message: "403 bq", failedStage: "load_bigquery" });
  assert.equal(reduceStreamEvent(failed, streamInterruptedEvent(J, "x")), failed);
});

test("streamEndReason / interruptionMessage wording", () => {
  assert.equal(streamEndReason({ userAborted: true, error: null }), "stopped by user");
  assert.equal(streamEndReason({ userAborted: false, error: "SSE timeout after 180000 ms" }), "network error: SSE timeout after 180000 ms");
  assert.equal(streamEndReason({ userAborted: false, error: null }), "server closed the stream");
  assert.equal(interruptionMessage("stopped by user"), "Stream ended before completion (stopped by user) — server restarted or connection lost");
});
