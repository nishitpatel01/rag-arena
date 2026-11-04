/**
 * Release 1.3.0 Step 4 — Controlled Ablation `/compare` + Retrieval Diff Inspector tests (`npm test`).
 * Covers:
 *   1. `retrievalDiffModel`: rank shift deltas (`same`, `promoted`, `demoted`, `added`, `dropped`),
 *      legacy citation fallback in replay mode, and `"chunk_id"` vs `"doc_page"` cross-store alignment.
 *   2. `ablationModel`: 2..4 slot bounds, clone/remove/update coercion, per-slot synthesis model override
 *      with shared unbiased judge, varying-axes scientific summary, and wire serialization for `/battle/stream`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ABLATION_TEMPLATES,
  MAX_ABLATION_SLOTS,
  MIN_ABLATION_SLOTS,
  ablationSlotChoices,
  buildAblationWire,
  changedAxesSummary,
  cloneAblationSlot,
  defaultAblationSlots,
  removeAblationSlot,
  updateAblationSlot,
} from "../src/features/compare/ablationModel";
import {
  buildRetrievalDiff,
  defaultAlignmentFor,
  extractEngineChunks,
} from "../src/features/compare/retrievalDiffModel";
import { defaultPresetSpec } from "../src/features/workbench/pipelineSpecModel";
import { RAG_ENGINE_GROUNDED_MODEL } from "../src/features/levels/modelSelection";
import type { EngineTimeline, ModelCatalogEntry, ModelsResponse } from "../src/types/arena";

const mkModel = (id: string, provider: string, roles: string[]): ModelCatalogEntry => ({
  id,
  label: id,
  provider,
  location: "us-central1",
  roles,
  input_usd_per_1m: 0.5,
  output_usd_per_1m: 2.0,
  price_source: null,
  price_as_of: null,
  available: true,
  unavailable_reason: null,
  verified_at: null,
});

const CATALOG: ModelsResponse = {
  models: [
    mkModel("gemini-4-argon", "vertex_gemini", ["synthesis", "judge"]),
    mkModel("gemini-3.8-flash", "vertex_gemini", ["synthesis", "judge"]),
    mkModel("claude-opus-5-5", "anthropic_vertex", ["synthesis", "judge"]),
    mkModel(RAG_ENGINE_GROUNDED_MODEL, "vertex_rag_engine", ["synthesis"]),
  ],
  defaults: { synthesis_model: "gemini-4-argon", judge_model: "claude-opus-5-5" },
  demo_mode: "live",
  recorded_pairs: [],
};

const mkTimeline = (
  engine: string,
  chunks: Array<{
    chunk_id: string;
    doc_name: string;
    page_number: number | null;
    score?: number | null;
    content?: string;
  }>,
): EngineTimeline => ({
  engine,
  stages: {},
  stageOrder: ["query_prep", "retrieve", "synthesize", "evaluate"],
  agentTurns: [],
  answer: {
    text: "ok",
    citations: chunks.map((c) => ({
      chunk_id: c.chunk_id,
      doc_name: c.doc_name,
      solicitation_id: "solicitation_01",
      page_number: c.page_number ?? 1,
      section_id: "C.3",
      element_type: "prose",
      bbox: { page: c.page_number ?? 1, x0: 0, y0: 0, x1: 1, y1: 1 },
      excerpt: c.content ?? "",
    })),
    retrievedChunks: chunks.map((c) => ({ chunk_id: c.chunk_id, content: c.content ?? "chunk body" })),
    retrievedChunkDetails: chunks.map((c) => ({
      chunk_id: c.chunk_id,
      content: c.content ?? "chunk body",
      doc_name: c.doc_name,
      solicitation_id: "solicitation_01",
      page_number: c.page_number,
      section_id: "C.3",
      element_type: "prose",
      score: c.score ?? null,
      amendment_number: null,
    })),
    latencyMs: 120,
    costUsd: 0.002,
    tokens: { input: 100, output: 40 },
    flags: {},
    artifacts: {},
  },
  evaluation: null,
  error: null,
  replayed: false,
});

test("retrievalDiffModel computes exact rank shifts (promoted, demoted, same, added, dropped) on chunk_id", () => {
  const timelines: Record<string, EngineTimeline> = {
    slot_1: mkTimeline("slot_1", [
      { chunk_id: "c_base_1", doc_name: "RFP_Base.pdf", page_number: 4, score: 0.88 },
      { chunk_id: "c_noise_2", doc_name: "RFP_Base.pdf", page_number: 19, score: 0.81 },
      { chunk_id: "c_amend_3", doc_name: "Amend_03.pdf", page_number: 2, score: 0.74 },
    ]),
    slot_2: mkTimeline("slot_2", [
      { chunk_id: "c_amend_3", doc_name: "Amend_03.pdf", page_number: 2, score: 0.96 },
      { chunk_id: "c_base_1", doc_name: "RFP_Base.pdf", page_number: 4, score: 0.89 },
      { chunk_id: "c_table_9", doc_name: "Attach_SLA.pdf", page_number: 7, score: 0.83 },
    ]),
  };
  const cols = [
    { engineId: "slot_1", short: "Slot 1", label: "No Rerank" },
    { engineId: "slot_2", short: "Slot 2", label: "Discovery Rerank" },
  ];
  assert.equal(defaultAlignmentFor(cols, timelines), "chunk_id");

  const diff = buildRetrievalDiff(cols, timelines, "slot_1", "chunk_id");
  assert.equal(diff.sharedAcrossAllCount, 2);
  assert.equal(diff.uniqueKeysTotal, 4);

  const byKey = new Map(diff.rows.map((r) => [r.key, r]));
  // c_amend_3 jumped #3 -> #1 (+2 promoted)
  const amendCell = byKey.get("c_amend_3")?.cells.slot_2;
  assert.equal(amendCell?.kind, "promoted");
  assert.equal(amendCell?.deltaVsBaseline, 2);
  assert.match(amendCell?.badgeText ?? "", /\+2/);

  // c_base_1 moved #1 -> #2 (-1 demoted)
  const baseCell = byKey.get("c_base_1")?.cells.slot_2;
  assert.equal(baseCell?.kind, "demoted");
  assert.equal(baseCell?.deltaVsBaseline, -1);

  // c_noise_2 was #2 in baseline, dropped in slot_2
  const droppedCell = byKey.get("c_noise_2")?.cells.slot_2;
  assert.equal(droppedCell?.kind, "dropped");

  // c_table_9 is newly retrieved at #3 in slot_2
  const addedCell = byKey.get("c_table_9")?.cells.slot_2;
  assert.equal(addedCell?.kind, "added");
  assert.equal(addedCell?.rank, 3);
});

test("retrievalDiffModel auto-selects doc_page alignment across heterogeneous stores and supports citation fallback", () => {
  const bqTl = mkTimeline("level2_hybrid", [
    { chunk_id: "bq_chunk_11", doc_name: "gs://bucket/corpus/Solicitation_01.pdf", page_number: 12 },
  ]);
  const ragTl = mkTimeline("rag_engine", [
    { chunk_id: "rag_engine_0_99a1", doc_name: "Solicitation_01.pdf", page_number: 12 },
  ]);
  const cols = [
    { engineId: "level2_hybrid", short: "Level 2", label: "Hybrid" },
    { engineId: "rag_engine", short: "Level 6", label: "RAG Engine" },
  ];
  const timelines = { level2_hybrid: bqTl, rag_engine: ragTl };
  assert.equal(defaultAlignmentFor(cols, timelines), "doc_page");

  const diff = buildRetrievalDiff(cols, timelines, "level2_hybrid", "doc_page");
  assert.equal(diff.sharedAcrossAllCount, 1);
  assert.equal(diff.rows.length, 1);
  assert.equal(diff.rows[0].cells.rag_engine.kind, "same");

  // Replay fallback when retrievedChunkDetails is absent (older recordings)
  const legacyTl: EngineTimeline = {
    ...bqTl,
    answer: bqTl.answer
      ? {
          ...bqTl.answer,
          retrievedChunkDetails: undefined,
          citations: [
            {
              chunk_id: "leg_1",
              doc_name: "Base.pdf",
              solicitation_id: "solicitation_01",
              page_number: 3,
              section_id: null,
              element_type: "prose",
              bbox: { page: 3, x0: 0, y0: 0, x1: 1, y1: 1 },
              excerpt: "quoted text",
            },
          ],
          retrievedChunks: [{ chunk_id: "leg_1", content: "full chunk content" }],
        }
      : null,
  };
  const extracted = extractEngineChunks(legacyTl);
  assert.equal(extracted.length, 1);
  assert.equal(extracted[0].docName, "Base.pdf");
  assert.equal(extracted[0].pageNumber, 3);
});

test("ablationModel enforces 2..4 slot bounds, coercion, per-slot synthesis override, and varying axes", () => {
  const initial = defaultAblationSlots();
  assert.equal(initial.length, MIN_ABLATION_SLOTS);
  assert.ok(ABLATION_TEMPLATES.length >= 3);

  // Cannot remove below MIN_ABLATION_SLOTS (2)
  assert.equal(removeAblationSlot(initial, initial[0].slotId).length, MIN_ABLATION_SLOTS);

  // Clone up to MAX_ABLATION_SLOTS (4), and 5th clone is a bounded no-op
  const three = cloneAblationSlot(initial, initial[1].slotId);
  assert.equal(three.length, 3);
  const four = cloneAblationSlot(three, three[2].slotId);
  assert.equal(four.length, MAX_ABLATION_SLOTS);
  const stillFour = cloneAblationSlot(four, four[3].slotId);
  assert.equal(stillFour.length, MAX_ABLATION_SLOTS);

  // Override synthesis model on slot_3 and switch slot_4 to rag_engine
  const customized = updateAblationSlot(
    updateAblationSlot(four, four[2].slotId, { synthesisModelOverride: "gemini-3.8-flash" }),
    four[3].slotId,
    { spec: defaultPresetSpec("rag_engine") },
  );
  const axes = changedAxesSummary(customized);
  assert.ok(axes.includes("index_profile"));
  assert.ok(axes.includes("reranker"));
  assert.ok(axes.includes("synthesis_model"));

  // Wire serialization preserves shared judge across all 4 slots while honoring slot_3 synthesis override
  const shared = { synthesis: "gemini-4-argon", judge: "claude-opus-5-5" };
  const wire = buildAblationWire(customized, shared);
  assert.equal(wire.length, 4);
  assert.equal(wire[0].synthesis_model, "gemini-4-argon");
  assert.equal(wire[2].synthesis_model, "gemini-3.8-flash");
  assert.ok(wire.every((w) => w.judge_model === "claude-opus-5-5"));

  // Choices surface blockers cleanly when a backing corpus is un-ingested or model conflicts with judge
  const choices = ablationSlotChoices({
    slots: updateAblationSlot(initial, initial[0].slotId, { synthesisModelOverride: "claude-opus-5-5" }),
    enabledLevels: ["level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "rag_engine"],
    catalog: CATALOG,
    sharedSelection: shared,
    questionId: "q_exact_clin_lookup",
    isUploadActive: false,
    activeWorkspace: null,
    ingestStatus: null,
    ingestError: null,
    isMock: true,
  });
  // Slot 1 has synthesis=claude-opus-5-5 == judge=claude-opus-5-5 -> blocked! Slot 2 is runnable!
  assert.match(choices[0].disabledReason ?? "", /differ from synthesis/i);
  assert.equal(choices[1].disabledReason, null);
});
