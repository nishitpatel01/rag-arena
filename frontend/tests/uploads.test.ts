/**
 * Upload workspace pure model + resumable upload protocol tests — run with `npm test`.
 * Covers: replay-mode disabling, zero app size cap, 256 KiB-aligned resumable chunking + 308 resume,
 * per-level readiness gating (incl. L4 -> L3), and Compare level choices under an upload scope.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { formatIngestRunCount } from "../src/features/ingest/ingestStatus";
import {
  CORPUS_SCOPE,
  RESUMABLE_CHUNK_BYTES,
  applyUploadScopeToChoices,
  contentRangeHeader,
  dataLevelFor,
  isUploadScope,
  isWorkspaceLevelReady,
  mergeWorkspaceList,
  normalizeScopeSelection,
  parseCommittedOffset,
  planUploadChunks,
  uploadRunGateFor,
  uploadViaResumableSession,
  uploadsEnabled,
  validateUploadPdfCandidate,
  type UploadWorkspace,
} from "../src/features/uploads/uploadModel";
import type { LevelChoice } from "../src/features/compare/compareModel";

const WS: UploadWorkspace = {
  id: "upl_0123456789ab",
  created_at: "2026-10-28T15:00:00Z",
  files: [
    { id: "f_001122334455", name: "Pricing.pdf", status: "ready", pages: 12, size_bytes: 250_000_000 },
  ],
  levels: {
    level1_naive: { status: "completed", chunks: 48, tier_profile: "naive" },
    level3_multimodal: { status: "completed", chunks: 19, tier_profile: "multimodal" },
  },
};

test("uploadsEnabled is false in replay mode and true in live/record", () => {
  assert.equal(uploadsEnabled("replay"), false);
  assert.equal(uploadsEnabled(null), false);
  assert.equal(uploadsEnabled("live"), true);
  assert.equal(uploadsEnabled("record"), true);
});

test("validateUploadPdfCandidate enforces .pdf and non-empty bytes with NO size cap (D3)", () => {
  assert.match(validateUploadPdfCandidate("notes.txt", 1024) ?? "", /\.pdf/i);
  assert.match(validateUploadPdfCandidate("empty.pdf", 0) ?? "", /empty/i);
  // Multi-hundred-MB and multi-GB files are accepted without any app size limit.
  assert.equal(validateUploadPdfCandidate("huge_solicitation.pdf", 650 * 1024 * 1024), null);
  assert.equal(validateUploadPdfCandidate("archive.PDF", 3 * 1024 * 1024 * 1024), null);
});

test("planUploadChunks aligns intermediate chunks to 256 KiB and builds Content-Range headers", () => {
  assert.equal(RESUMABLE_CHUNK_BYTES % (256 * 1024), 0);
  const total = 20 * 1024 * 1024 + 123;
  const chunks = planUploadChunks(total, 0, 8 * 1024 * 1024);
  assert.deepEqual(chunks, [
    { start: 0, endExclusive: 8 * 1024 * 1024 },
    { start: 8 * 1024 * 1024, endExclusive: 16 * 1024 * 1024 },
    { start: 16 * 1024 * 1024, endExclusive: total },
  ]);
  assert.equal(contentRangeHeader(0, 8 * 1024 * 1024, total), `bytes 0-${8 * 1024 * 1024 - 1}/${total}`);

  // Resume from an arbitrary committed byte offset reported by GCS 308 Range header
  assert.equal(parseCommittedOffset("bytes=0-8388607", total), 8_388_608);
  assert.equal(parseCommittedOffset(null, total), 0);
});

test("uploadViaResumableSession streams slices, reports progress, and resumes from GCS 308 offset on failure", async () => {
  const total = 1024 * 1024; // 1 MiB
  const chunkBytes = 512 * 1024; // 512 KiB (multiple of 256 KiB)
  const calls: string[] = [];
  const progress: number[] = [];
  let failSecondChunkOnce = true;

  await uploadViaResumableSession({
    sessionUri: "https://storage.googleapis.com/upload/session/abc",
    totalBytes: total,
    chunkBytes,
    sliceBlob: (start, end) => ({ size: end - start }) as unknown as Blob,
    sendPut: async (_uri, headers) => {
      const range = headers["Content-Range"];
      calls.push(range);
      if (range === `bytes */${total}`) {
        return { status: 308, rangeHeader: `bytes=0-${chunkBytes - 1}` };
      }
      if (range.startsWith(`bytes ${chunkBytes}-`) && failSecondChunkOnce) {
        failSecondChunkOnce = false;
        throw new Error("Simulated transient network drop");
      }
      if (range.startsWith("bytes 0-")) {
        return { status: 308, rangeHeader: `bytes=0-${chunkBytes - 1}` };
      }
      return { status: 200, rangeHeader: null };
    },
    onProgress: (sent) => progress.push(sent),
  });

  // Chunk 1 succeeded -> Chunk 2 dropped -> Offset probe (`bytes */total`) -> Chunk 2 retried (Chunk 1 NOT resent!)
  assert.deepEqual(calls, [
    `bytes 0-${chunkBytes - 1}/${total}`,
    `bytes ${chunkBytes}-${total - 1}/${total}`,
    `bytes */${total}`,
    `bytes ${chunkBytes}-${total - 1}/${total}`,
  ]);
  assert.equal(progress.at(-1), total);
});

test("workspace readiness maps Level 4 to Level 3 data and gates un-ingested levels clearly", () => {
  assert.equal(dataLevelFor("level4_agentic"), "level3_multimodal");
  assert.equal(dataLevelFor("managed_vertex"), "managed_vertex");
  assert.equal(isWorkspaceLevelReady(WS, "level1_naive"), true);
  assert.equal(isWorkspaceLevelReady(WS, "level2_hybrid"), false);
  assert.equal(isWorkspaceLevelReady(WS, "level3_multimodal"), true);
  assert.equal(isWorkspaceLevelReady(WS, "level4_agentic"), true);
  assert.equal(isWorkspaceLevelReady(WS, "managed_vertex"), false);

  const l1Gate = uploadRunGateFor("level1_naive", WS);
  assert.equal(l1Gate.blocked, false);
  assert.equal(l1Gate.count, 48);

  const l4Gate = uploadRunGateFor("level4_agentic", WS);
  assert.equal(l4Gate.blocked, false);

  const l2Gate = uploadRunGateFor("level2_hybrid", WS);
  assert.equal(l2Gate.blocked, true);
  assert.match(l2Gate.note ?? "", /level2_hybrid/);

  const baseChoices: LevelChoice[] = [
    { levelId: "level1_naive", label: "Naive", short: "Level 1", order: 1, disabledReason: null },
    { levelId: "level2_hybrid", label: "Hybrid", short: "Level 2", order: 2, disabledReason: null },
    { levelId: "level4_agentic", label: "Agentic", short: "Level 4", order: 4, disabledReason: null },
    { levelId: "managed_vertex", label: "Managed", short: "Level 5", order: 5, disabledReason: null },
  ];
  const scoped = applyUploadScopeToChoices(baseChoices, WS);
  assert.equal(scoped.find((c) => c.levelId === "level1_naive")?.disabledReason, null);
  assert.equal(scoped.find((c) => c.levelId === "level4_agentic")?.disabledReason, null);
  assert.match(scoped.find((c) => c.levelId === "level2_hybrid")?.disabledReason ?? "", /not ingested/i);
  assert.match(scoped.find((c) => c.levelId === "managed_vertex")?.disabledReason ?? "", /not ingested/i);

  assert.equal(isUploadScope(CORPUS_SCOPE), false);
  assert.equal(isUploadScope(WS.id), true);
  assert.equal(normalizeScopeSelection(WS.id, [WS], "live"), WS.id);
  assert.equal(normalizeScopeSelection("upl_missing000000", [WS], "live"), CORPUS_SCOPE);
  assert.equal(normalizeScopeSelection(WS.id, [WS], "replay"), CORPUS_SCOPE);

  const fresh: UploadWorkspace = {
    id: "upl_999999999999",
    created_at: "2026-10-28T16:00:00Z",
    files: [],
    levels: {},
  };
  assert.deepEqual(
    mergeWorkspaceList([], [fresh]).map((w) => w.id),
    ["upl_999999999999"],
    "preserves newly created workspace if immediate list lags"
  );
  const updatedFresh: UploadWorkspace = {
    ...fresh,
    files: [{ id: "f_111111111111", name: "doc.pdf", status: "ready", pages: 2, size_bytes: 500 }],
  };
  assert.equal(
    mergeWorkspaceList([updatedFresh], [fresh])[0].files.length,
    1,
    "server list entry supersedes cached optimistic entry once visible"
  );

  assert.equal(formatIngestRunCount(null), "—", "managed_vertex ingest_runs rows have null chunks");
  assert.equal(formatIngestRunCount(undefined), "—");
  assert.equal(formatIngestRunCount(5067), (5067).toLocaleString());
});
