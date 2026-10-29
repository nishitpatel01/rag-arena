/**
 * Persistent ingestion-session tests — run with `npm test`.
 * An IngestSession lives above routes: unsubscribing (unmounting /ingest or a RAG level page) must NEVER
 * abort the SSE stream or drop progress; active jobs are visible on both /ingest and the corresponding
 * RAG level page (including L4 reading L3 multimodal ingestion), and reloaded tabs re-attach idempotently.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  IngestSession,
  MAX_ABORTED_JOB_IDS,
  MAX_TRACKED_INGEST_LEVELS,
  ingestDataLevelFor,
  selectIngestForIngestPage,
  selectIngestForLevelPage,
  type ActiveIngestJobSummary,
  type OpenIngestTransport,
  type IngestTransportCallbacks,
} from "../src/features/ingest/ingestSession";

const T = "2026-10-28T17:50:00.000Z";

interface FakeStream {
  readonly jobId: string;
  readonly level: string;
  readonly cb: IngestTransportCallbacks;
  aborted: boolean;
}

function fakeIngestTransport(): { open: OpenIngestTransport; streams: FakeStream[] } {
  const streams: FakeStream[] = [];
  const open: OpenIngestTransport = (jobId, level, cb) => {
    const s: FakeStream = { jobId, level, cb, aborted: false };
    streams.push(s);
    return { abort: () => (s.aborted = true) };
  };
  return { open, streams };
}

const jobStarted = (jobId: string, level: string) => ({
  event: "job_started",
  run_id: jobId,
  job_id: jobId,
  engine: level,
  tier_profile: level === "level3_multimodal" ? "multimodal" : "naive",
  ts: T,
});
const stageStarted = (jobId: string, level: string, stage: string) => ({
  event: "stage_started",
  run_id: jobId,
  job_id: jobId,
  engine: level,
  stage,
  label: stage,
  service: "BigQuery",
  request: {},
  ts: T,
});
const stageCompleted = (jobId: string, level: string, stage: string) => ({
  event: "stage_completed",
  run_id: jobId,
  job_id: jobId,
  engine: level,
  stage,
  duration_ms: 42,
  payload: {},
  ts: T,
});
const embedProgress = (jobId: string, level: string, done: number, total: number) => ({
  event: "embed_progress",
  run_id: jobId,
  job_id: jobId,
  engine: level,
  done,
  total,
  batch_ms: 120,
  ts: T,
});
const jobComplete = (jobId: string, level: string, chunks = 5067) => ({
  event: "job_complete",
  run_id: jobId,
  job_id: jobId,
  engine: level,
  chunks,
  counts_by_tier: { naive: chunks },
  index_states: {},
  ts: T,
});

test("unsubscribing (/ingest unmount) never aborts the stream or loses progress; visible on /ingest and target RAG level page", async () => {
  const { open, streams } = fakeIngestTransport();
  const settled: Array<{ level: string; scope: string | null }> = [];
  const session = new IngestSession(open, (entry) => settled.push({ level: entry.level, scope: entry.scope }));

  let notifications = 0;
  const unsubscribeIngestPage = session.subscribe(() => (notifications += 1));

  await session.start("level1_naive", async () => ({ job_id: "ing_nav000001" }));
  assert.equal(streams.length, 1);
  streams[0].cb.onFrame(jobStarted("ing_nav000001", "level1_naive"));
  streams[0].cb.onFrame(stageStarted("ing_nav000001", "level1_naive", "embed"));
  streams[0].cb.onFrame(embedProgress("ing_nav000001", "level1_naive", 64, 128));

  // User navigates away from /ingest -> IngestPage unmounts
  unsubscribeIngestPage();
  assert.equal(streams[0].aborted, false);

  // User opens /levels/level1_naive while frames continue arriving
  const snapDuring = session.getSnapshot();
  const l1View = selectIngestForLevelPage(snapDuring, "level1_naive");
  assert.ok(l1View);
  assert.equal(l1View.entry.jobId, "ing_nav000001");
  assert.equal(l1View.entry.isStreaming, true);
  assert.equal(l1View.entry.selectedStage, "embed");
  assert.equal(selectIngestForLevelPage(snapDuring, "level2_hybrid"), null);

  // Job completes while /ingest is still unmounted
  streams[0].cb.onFrame(stageCompleted("ing_nav000001", "level1_naive", "embed"));
  streams[0].cb.onFrame(jobComplete("ing_nav000001", "level1_naive", 128));
  streams[0].cb.onEnd();

  // User navigates back to /ingest: full state, events, and completion seq are intact
  const snapAfter = session.getSnapshot();
  const ingestView = selectIngestForIngestPage(snapAfter);
  assert.ok(ingestView);
  assert.equal(ingestView.jobId, "ing_nav000001");
  assert.equal(ingestView.isStreaming, false);
  assert.equal(ingestView.state.complete, true);
  assert.equal(ingestView.state.events.length, 5);
  assert.equal(snapAfter.completedSeq, 1);
  assert.deepEqual(settled, [{ level: "level1_naive", scope: null }]);
  assert.ok(notifications >= 3);
});

test("Level 4 RAG page observes Level 3 multimodal ingestion via ingestDataLevelFor", async () => {
  assert.equal(ingestDataLevelFor("level4_agentic"), "level3_multimodal");
  assert.equal(ingestDataLevelFor("level1_naive"), "level1_naive");

  const { open, streams } = fakeIngestTransport();
  const settledScopes: Array<string | null> = [];
  const session = new IngestSession(open, (entry) => settledScopes.push(entry.scope));

  await session.start("level3_multimodal", async () => ({ job_id: "ing_mm0000001" }), { scope: "upl_aabbccddeeff" });
  streams[0].cb.onFrame(jobStarted("ing_mm0000001", "level3_multimodal"));
  streams[0].cb.onFrame(stageStarted("ing_mm0000001", "level3_multimodal", "parse"));

  const snap = session.getSnapshot();
  const l3 = selectIngestForLevelPage(snap, "level3_multimodal");
  const l4 = selectIngestForLevelPage(snap, "level4_agentic");
  assert.ok(l3 && l4);
  assert.equal(l4.backingLevelId, "level3_multimodal");
  assert.equal(l4.entry.jobId, "ing_mm0000001");
  assert.equal(l4.entry.scope, "upl_aabbccddeeff");

  streams[0].cb.onFrame(jobComplete("ing_mm0000001", "level3_multimodal", 9));
  streams[0].cb.onEnd();
  assert.deepEqual(settledScopes, ["upl_aabbccddeeff"]);
});

test("selectedStage user override and independent dismiss on level vs /ingest (ignored while streaming)", async () => {
  const { open, streams } = fakeIngestTransport();
  const session = new IngestSession(open);
  await session.start("level1_naive", async () => ({ job_id: "ing_sel000001" }));
  streams[0].cb.onFrame(jobStarted("ing_sel000001", "level1_naive"));
  streams[0].cb.onFrame(stageStarted("ing_sel000001", "level1_naive", "discover_docs"));
  assert.equal(session.getSnapshot().entries.level1_naive.selectedStage, "discover_docs");

  // User explicitly clicks "discover_docs", then "parse" becomes active -> user choice sticks
  session.selectStage("level1_naive", "discover_docs");
  streams[0].cb.onFrame(stageCompleted("ing_sel000001", "level1_naive", "discover_docs"));
  streams[0].cb.onFrame(stageStarted("ing_sel000001", "level1_naive", "parse"));
  assert.equal(session.getSnapshot().entries.level1_naive.selectedStage, "discover_docs");

  // Dismiss while streaming is a no-op
  session.dismissOnLevel("level1_naive");
  session.dismissOnIngest("level1_naive");
  assert.ok(selectIngestForLevelPage(session.getSnapshot(), "level1_naive"));
  assert.ok(selectIngestForIngestPage(session.getSnapshot()));

  // Once finished, dismissing on level page hides it on /levels/level1_naive while keeping /ingest visible
  streams[0].cb.onFrame(jobComplete("ing_sel000001", "level1_naive", 10));
  streams[0].cb.onEnd();
  session.dismissOnLevel("level1_naive");
  assert.equal(selectIngestForLevelPage(session.getSnapshot(), "level1_naive"), null);
  assert.ok(selectIngestForIngestPage(session.getSnapshot()));

  session.dismissOnIngest("level1_naive");
  assert.equal(selectIngestForIngestPage(session.getSnapshot()), null);
});

test("syncActiveJobs re-attaches running backend jobs idempotently and skips completed or user-aborted jobs", () => {
  const { open, streams } = fakeIngestTransport();
  const session = new IngestSession(open);

  const active: ActiveIngestJobSummary[] = [
    { job_id: "ing_reload001", level: "level1_naive", status: "running", scope: null },
    { job_id: "ing_done00001", level: "level2_hybrid", status: "completed", scope: null },
  ];
  session.syncActiveJobs(active);
  assert.equal(streams.length, 1);
  assert.equal(streams[0].jobId, "ing_reload001");

  // Repeated status polls with the same active job must NOT open a second stream
  session.syncActiveJobs(active);
  assert.equal(streams.length, 1);

  // User aborts following ing_reload001 -> subsequent status polls do not re-attach it
  session.abort("level1_naive");
  assert.equal(streams[0].aborted, true);
  session.syncActiveJobs(active);
  assert.equal(streams.length, 1);

  // Bounded aborted-ID set and bounded tracked levels
  for (let i = 0; i < MAX_ABORTED_JOB_IDS + 5; i += 1) {
    session.syncActiveJobs([{ job_id: `ing_b_${i}`, level: `lvl_${i % MAX_TRACKED_INGEST_LEVELS}`, status: "running" }]);
    session.abort(`lvl_${i % MAX_TRACKED_INGEST_LEVELS}`);
  }
  assert.ok(Object.keys(session.getSnapshot().entries).length <= MAX_TRACKED_INGEST_LEVELS);
});

test("createJob failure surfaces transportError cleanly and snapshot reference stays stable when unchanged", async () => {
  const { open, streams } = fakeIngestTransport();
  const session = new IngestSession(open);
  const s0 = session.getSnapshot();
  assert.equal(session.getSnapshot(), s0);

  await session.start("level2_hybrid", async () => {
    throw new Error("409 level2_hybrid is already being ingested");
  });
  assert.equal(streams.length, 0);
  const entry = selectIngestForIngestPage(session.getSnapshot());
  assert.ok(entry);
  assert.equal(entry.isStreaming, false);
  assert.equal(entry.transportError, "409 level2_hybrid is already being ingested");

  const s1 = session.getSnapshot();
  session.syncActiveJobs([]);
  assert.equal(session.getSnapshot(), s1);
});
