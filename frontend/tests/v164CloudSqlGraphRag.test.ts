/**
 * v1.6.4 TDD suite: Gemini 4 Neon, Cloud SQL pgvector (Level 5), BigQuery GraphRAG (Level 9),
 * Workbench ADK-on-all-stores & dense-only ANN/PG ablations, and GraphExpandDetail ISO GQL renderer.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server.browser";

import { SHORT_LEVEL_LABEL } from "../src/app/navMenuModel";
import { DEFAULT_ACL_CAPABILITIES } from "../src/features/acl/aclModel";
import { STAGE_LABELS } from "../src/features/levels/StageRail";
import { dedicatedStageView } from "../src/features/levels/stageViews";
import {
  CLOUDSQL_BASE_MONTHLY_USD,
  estimateEnterpriseTco,
  parseTcoWorkload,
} from "../src/features/wizard/tcoModel";
import {
  INDEX_PROFILES,
  SCANN_RETRIEVAL_MODES,
  activeQueryStages,
  buildPipelineDescriptor,
  coerceSpec,
  defaultPresetSpec,
  validatePipelineSpec,
} from "../src/features/workbench/pipelineSpecModel";
import { LEVEL_IDS } from "../src/types/arena";

test("v1.6.4 Order A 10-level ladder, ACL capabilities, and Gemini 4 Neon TCO math", () => {
  assert.equal(LEVEL_IDS.length, 10);
  assert.equal(SHORT_LEVEL_LABEL.cloudsql_pgvector, "5 · Cloud SQL pgvector");
  assert.equal(SHORT_LEVEL_LABEL.bigquery_graphrag, "9 · BQ GraphRAG (GQL)");
  assert.equal(SHORT_LEVEL_LABEL.level4_agentic, "10 · Agentic ADK");
  assert.equal(STAGE_LABELS.graph_expand, "Graph GQL");
  assert.equal(DEFAULT_ACL_CAPABILITIES.cloudsql_pgvector.enforcement_mode, "postgres_rls_policy");
  assert.equal(DEFAULT_ACL_CAPABILITIES.bigquery_graphrag.enforcement_mode, "bigquery_parameterized_rls");

  const neonWl = parseTcoWorkload({
    synthesis_model: "gemini-4-neon",
    monthly_queries: 50_000,
    corpus_pages: 10_000,
  });
  const est = estimateEnterpriseTco(neonWl);
  assert.equal(est.architectures.length, 10);
  const byId = Object.fromEntries(est.architectures.map((a) => [a.level_id, a]));
  assert.ok(byId.cloudsql_pgvector.standing_monthly_usd >= CLOUDSQL_BASE_MONTHLY_USD - 0.05);
  assert.ok(byId.cloudsql_pgvector.standing_monthly_usd < byId.alloydb_scann.standing_monthly_usd);
  assert.equal(byId.bigquery_graphrag.standing_monthly_usd, 0);
  assert.equal(byId.bigquery_graphrag.idle_mode, "serverless_zero");
});

test("Workbench unlocks adk_agent across all 9 stores and dense-only vector_cosine across PG/ANN stores", () => {
  assert.deepEqual(SCANN_RETRIEVAL_MODES, ["hybrid_rrf", "vector_cosine"]);
  assert.equal(INDEX_PROFILES.length, 9);

  for (const profile of ["cloudsql_pgvector", "alloydb_scann", "vertex_vector_v2"] as const) {
    const dense = coerceSpec({
      ...defaultPresetSpec(profile),
      slots: { ...defaultPresetSpec(profile).slots, retrieval_mode: "vector_cosine", reranker: "none" },
    });
    assert.equal(validatePipelineSpec(dense), null);
    assert.equal(dense.slots.retrieval_mode, "vector_cosine");
    const desc = buildPipelineDescriptor(dense);
    assert.equal(desc.edges.some((e) => e.label === "tsquery terms" || e.label === "sparse tokens"), false);

    const adk = coerceSpec({
      ...defaultPresetSpec(profile),
      slots: { ...defaultPresetSpec(profile).slots, reasoning_mode: "adk_agent", agent_tools: [] },
    });
    assert.equal(validatePipelineSpec(adk), null);
    assert.equal(adk.slots.reasoning_mode, "adk_agent");
    assert.equal(adk.slots.agent_tools.includes("check_amendments"), false);
    const adkDesc = buildPipelineDescriptor(adk);
    const nodeIds = new Set(adkDesc.nodes.map((n) => n.id));
    assert.ok(nodeIds.has("agent"));
    assert.ok(nodeIds.has(profile === "cloudsql_pgvector" ? "cloudsql" : profile === "alloydb_scann" ? "alloydb" : "vvs2"));
  }

  const graphPreset = defaultPresetSpec("bigquery_graphrag");
  assert.equal(validatePipelineSpec(graphPreset), null);
  assert.ok(activeQueryStages(graphPreset).includes("graph_expand"));
  const graphDesc = buildPipelineDescriptor(graphPreset);
  assert.ok(graphDesc.nodes.some((n) => n.id === "graph"));
});

test("dedicatedStageView renders GraphExpandDetail ISO GQL edges and counts", () => {
  const view = dedicatedStageView("graph_expand", {
    graph_name: "rag_arena.procurement_graph",
    query_language: "ISO GQL (GRAPH_TABLE)",
    seed_docs: ["solicitation_01::04_Pricing_Schedule_CLINs.pdf"],
    seed_sections: ["solicitation_01::CLIN-0004-AB"],
    edges_traversed: 1,
    traversed_edges: [
      {
        edge_type: "AMENDS_SECTION",
        source_doc: "06_Amendment_0001_Scope_Update.pdf",
        target_label: "CLIN-0004-AB",
        hop_count: 1,
        amendment_number: 1,
        chunk_id: "sol01_amd1_c1",
      },
    ],
    graph_chunks_returned: 2,
    graph_chunks_added: 1,
    merged_candidates: 16,
    bytes_processed: 10485760,
    slot_millis: 412,
  });
  assert.ok(view);
  const html = renderToStaticMarkup(view);
  assert.ok(html.includes("graph-expand-detail"));
  assert.ok(html.includes("ISO GQL (GRAPH_TABLE)"));
  assert.ok(html.includes("06_Amendment_0001_Scope_Update.pdf"));
  assert.ok(html.includes("AMENDS_SECTION"));
  assert.ok(html.includes("+1 amendment chunks merged"));
});
