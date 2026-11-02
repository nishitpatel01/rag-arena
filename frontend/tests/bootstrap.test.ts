import assert from "node:assert/strict";
import { test } from "node:test";
import { BOOTSTRAP_TIMEOUT_MS } from "../src/lib/api";
import { createSingleFlight, loadBootstrapData } from "../src/lib/bootstrap";
import type { HealthResponse, LevelsResponse } from "../src/types/arena";

const HEALTH: HealthResponse = {
  status: "ok",
  demo_mode: "live",
  project_id: "proj",
  gcs_bucket: "corp",
  upload_bucket: "upl",
  enabled_levels: ["level1_naive"],
};

const LEVELS: LevelsResponse = {
  demo_mode: "live",
  enabled_levels: ["level1_naive"],
  levels: [
    {
      level_id: "level1_naive",
      label: "Level 1",
      order: 1,
      enabled: true,
      stages: ["query_prep", "retrieve", "synthesize", "evaluate"],
      architecture: { level_id: "level1_naive", nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] },
    },
  ],
};

test("BOOTSTRAP_TIMEOUT_MS gives Cloud Run cold starts 30 s", () => {
  assert.equal(BOOTSTRAP_TIMEOUT_MS, 30_000);
});

test("loadBootstrapData retries only the failed endpoint and preserves partial success", async () => {
  let healthCalls = 0;
  let levelsCalls = 0;
  const delays: number[] = [];

  const res = await loadBootstrapData({
    fetchHealth: async () => {
      healthCalls += 1;
      return HEALTH;
    },
    fetchLevels: async () => {
      levelsCalls += 1;
      if (levelsCalls < 3) throw new Error("timed out after 30 s");
      return LEVELS;
    },
    maxAttempts: 3,
    baseDelayMs: 100,
    sleep: async (ms) => {
      delays.push(ms);
    },
  });

  assert.equal(healthCalls, 1);
  assert.equal(levelsCalls, 3);
  assert.deepEqual(delays, [100, 200]);
  assert.deepEqual(res.errors, []);
  assert.equal(res.health?.demo_mode, "live");
  assert.equal(res.levels?.levels.length, 1);
});

test("loadBootstrapData surfaces verbatim errors after bounded retries exhaust", async () => {
  let calls = 0;
  const res = await loadBootstrapData({
    fetchHealth: async () => {
      calls += 1;
      throw new Error("timed out after 30 s (0 /api/v1/health)");
    },
    fetchLevels: async () => {
      throw new Error("502 Bad Gateway");
    },
    maxAttempts: 2,
    baseDelayMs: 10,
    sleep: async () => {},
  });

  assert.equal(calls, 2);
  assert.equal(res.health, null);
  assert.equal(res.levels, null);
  assert.deepEqual(res.errors, [
    "health: timed out after 30 s (0 /api/v1/health)",
    "levels: 502 Bad Gateway",
  ]);
});

test("createSingleFlight coalesces concurrent calls and resets after settlement", async () => {
  let underlyingCalls = 0;
  const gate: { resolve?: (v: number) => void } = {};
  const wrapped = createSingleFlight(
    () =>
      new Promise<number>((resolve) => {
        underlyingCalls += 1;
        gate.resolve = resolve;
      })
  );

  const p1 = wrapped();
  const p2 = wrapped();
  assert.equal(underlyingCalls, 1);
  gate.resolve!(42);
  assert.deepEqual(await Promise.all([p1, p2]), [42, 42]);

  // Subsequent call after settlement triggers a fresh underlying call
  const p3 = wrapped();
  assert.equal(underlyingCalls, 2);
  gate.resolve!(99);
  assert.equal(await p3, 99);
});
