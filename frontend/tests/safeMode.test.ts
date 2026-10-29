/**
 * Screen-share safe mode tests — run with `npm test` (esbuild bundles to JS, then plain `node`).
 * Covers: maskIdentity exact replacement of project id / bucket / project number, maskDeep over
 * nested server payloads, and the toggle defaulting to off unless `?safe=1` or localStorage say on.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MASK_BUCKET,
  MASK_PROJECT_ID,
  MASK_PROJECT_NUMBER,
  MASK_UPLOAD_BUCKET,
  SAFE_MODE_STORAGE_KEY,
  initialSafeMode,
  maskDeep,
  maskIdentity,
} from "../src/lib/safeMode";

// Synthetic identities only (never real values in a committed test).
const health = {
  project_id: "acme-proj-42",
  gcs_bucket: "acme-proj-42-rag-arena-corpus",
  upload_bucket: "acme-proj-42-rag-arena-uploads",
};

test("maskIdentity replaces the project id with the placeholder", () => {
  assert.equal(maskIdentity("FROM `acme-proj-42.rag_arena.document_chunks_v3`", health), `FROM \`${MASK_PROJECT_ID}.rag_arena.document_chunks_v3\``);
});

test("maskIdentity replaces the bucket before the project id it contains", () => {
  assert.equal(maskIdentity("gs://acme-proj-42-rag-arena-corpus/sample_docs/a.pdf", health), `gs://${MASK_BUCKET}/sample_docs/a.pdf`);
  assert.equal(
    maskIdentity("gs://acme-proj-42-rag-arena-uploads/uploads/upl_0123456789ab/a.pdf", health),
    `gs://${MASK_UPLOAD_BUCKET}/uploads/upl_0123456789ab/a.pdf`
  );
});

test("maskIdentity replaces projects/<digits> with the project-number placeholder", () => {
  assert.equal(
    maskIdentity("projects/123456789012/locations/global/publishers/google", health),
    `projects/${MASK_PROJECT_NUMBER}/locations/global/publishers/google`
  );
  assert.equal(maskIdentity("projects/acme-proj-42/locations/global", health), `projects/${MASK_PROJECT_ID}/locations/global`);
});

test("maskIdentity replaces every occurrence and leaves other text untouched", () => {
  assert.equal(maskIdentity("a acme-proj-42 b acme-proj-42 c", health), `a ${MASK_PROJECT_ID} b ${MASK_PROJECT_ID} c`);
  assert.equal(maskIdentity("nothing to see", health), "nothing to see");
});

test("maskIdentity without health still masks project numbers and tolerates empty identities", () => {
  assert.equal(maskIdentity("projects/987654/x", null), `projects/${MASK_PROJECT_NUMBER}/x`);
  assert.equal(maskIdentity("acme-proj-42", { project_id: "", gcs_bucket: null }), "acme-proj-42");
});

test("maskIdentity treats regex metacharacters in identities literally", () => {
  assert.equal(maskIdentity("a.b.c and axbxc", { project_id: "a.b.c", gcs_bucket: null }), `${MASK_PROJECT_ID} and axbxc`);
});

test("maskDeep masks strings in nested objects/arrays and keys, preserving other types", () => {
  const input = {
    sql: "SELECT * FROM `acme-proj-42.ds.t`",
    rows: 5,
    ok: true,
    none: null,
    nested: [{ uri: "gs://acme-proj-42-rag-arena-corpus/x" }, "projects/1234567/y"],
    "acme-proj-42": "key",
  };
  const out = maskDeep(input, health);
  assert.deepEqual(out, {
    sql: `SELECT * FROM \`${MASK_PROJECT_ID}.ds.t\``,
    rows: 5,
    ok: true,
    none: null,
    nested: [{ uri: `gs://${MASK_BUCKET}/x` }, `projects/${MASK_PROJECT_NUMBER}/y`],
    [MASK_PROJECT_ID]: "key",
  });
  assert.equal(input.sql, "SELECT * FROM `acme-proj-42.ds.t`", "input is not mutated");
});

test("maskDeep is bounded on very deep structures (fails loud instead of overflowing)", () => {
  let deep: unknown = "acme-proj-42";
  for (let i = 0; i < 200; i += 1) deep = [deep];
  assert.throws(() => maskDeep(deep, health), /depth/);
});

test("safe mode defaults to off", () => {
  assert.equal(initialSafeMode("", null), false);
  assert.equal(initialSafeMode("?mock=1", "0"), false);
});

test("safe mode is on with ?safe=1 or a persisted '1'", () => {
  assert.equal(initialSafeMode("?safe=1", null), true);
  assert.equal(initialSafeMode("?foo=bar&safe=1", "0"), true);
  assert.equal(initialSafeMode("", "1"), true);
  assert.equal(SAFE_MODE_STORAGE_KEY, "rag-arena.safe-mode");
});
