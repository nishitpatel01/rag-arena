/**
 * TDD suite for v1.6.3 Identity-Aware Document ACLs & Row-Level Security (Frontend):
 * - Allow-list validation for persona IDs & storage parsing
 * - Per-architecture capability & replay ACL demo gating (including honest RagCorpus fail-closed)
 * - Document viewer `?persona=` propagation
 * - SSE `run_started` ACL reducer + `RunPills` / `RunStatusPills` rendering
 * - `AccessPersonaSelect` SSR rendering (restricted group pills, doc counts, RagCorpus warning, replay hint)
 * - `tcoModel` `strict_document_acls` parity & 6th `enterprise_document_acls` preset
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server.browser";
import { MemoryRouter } from "react-router-dom";
import { AccessPersonaSelect } from "../src/features/acl/AccessPersonaSelect";
import {
  ACL_PERSONA_IDS,
  ALL_ACCESS_PERSONA_ID,
  DEFAULT_ACL_CAPABILITIES,
  DEFAULT_ACL_PERSONAS,
  DEFAULT_CORPUS_ACL_SUMMARY,
  aclReplayBlockedReason,
  findRecordedAclDemo,
  isPersonaRestricted,
  isValidAclPersonaId,
  parseStoredPersona,
  personaById,
  type AccessPersonaState,
} from "../src/features/acl/aclModel";
import { RunStatusPills } from "../src/features/compare/RunStatusPills";
import { INITIAL_STREAM_STATE, reduceStreamEvent } from "../src/features/levels/model";
import { RunPills } from "../src/features/levels/RunPills";
import { ArchitectureWizard } from "../src/features/wizard/ArchitectureWizard";
import {
  COMPLIANCE_POSTURE_OPTIONS,
  ENTERPRISE_SCENARIO_PRESETS,
  estimateEnterpriseTco,
  parseTcoWorkload,
} from "../src/features/wizard/tcoModel";
import { highlightsUrl, pageImageUrl } from "../src/lib/documents";
import type { RecordedAclDemo } from "../src/types/arena";

const SAMPLE_DEMOS: readonly RecordedAclDemo[] = [
  {
    engine: "level4_agentic",
    synthesis_model: "gemini-4-hydrogen",
    judge_model: "claude-opus-5-5",
    question_id: "q_amendment_rate_comparison",
    access_persona: "engineering_analyst",
    caller_groups: ["corp_all"],
  },
];

function makeAclState(personaId: AccessPersonaState["personaId"]): AccessPersonaState {
  return {
    personaId,
    persona: personaById(DEFAULT_ACL_PERSONAS, personaId),
    isRestricted: isPersonaRestricted(personaId),
    personas: DEFAULT_ACL_PERSONAS,
    capabilities: DEFAULT_ACL_CAPABILITIES,
    corpusSummary: DEFAULT_CORPUS_ACL_SUMMARY,
    recordedDemos: SAMPLE_DEMOS,
    setPersonaId: () => undefined,
  };
}

test("persona allow-list validation fails closed to all_access on untrusted storage input", () => {
  assert.equal(ACL_PERSONA_IDS.length, 5);
  assert.equal(isValidAclPersonaId("engineering_analyst"), true);
  assert.equal(isValidAclPersonaId("super_admin"), false);
  assert.equal(parseStoredPersona(null), ALL_ACCESS_PERSONA_ID);
  assert.equal(parseStoredPersona("'; DROP TABLE --"), ALL_ACCESS_PERSONA_ID);
  assert.equal(parseStoredPersona("security_soc2"), "security_soc2");
  assert.equal(isPersonaRestricted("all_access"), false);
  assert.equal(isPersonaRestricted("engineering_analyst"), true);
});

test("aclReplayBlockedReason enforces honest RagCorpus fail-closed and exact replay ACL demo matching", () => {
  // Unrestricted always passes
  assert.equal(
    aclReplayBlockedReason({
      demoMode: "replay",
      levelId: "rag_engine",
      personaId: "all_access",
      questionId: "q_amendment_rate_comparison",
      synthesisModel: "gemini-4-hydrogen",
      judgeModel: "claude-opus-5-5",
      demos: SAMPLE_DEMOS,
    }),
    null
  );

  // RagCorpus fails closed on restricted persona in BOTH live and replay modes
  for (const mode of ["live", "replay"]) {
    const reason = aclReplayBlockedReason({
      demoMode: mode,
      levelId: "rag_engine",
      personaId: "engineering_analyst",
      questionId: "q_amendment_rate_comparison",
      synthesisModel: "gemini-4-hydrogen",
      judgeModel: "claude-opus-5-5",
      demos: SAMPLE_DEMOS,
    });
    assert.ok(reason && reason.includes("RagCorpus"), `expected RagCorpus warning in mode=${mode}, got ${reason}`);
  }

  // Exact recorded ACL demo unlocks Replay
  assert.ok(
    findRecordedAclDemo(SAMPLE_DEMOS, {
      engine: "level4_agentic",
      synthesisModel: "gemini-4-hydrogen",
      judgeModel: "claude-opus-5-5",
      questionId: "q_amendment_rate_comparison",
      personaId: "engineering_analyst",
    })
  );
  assert.equal(
    aclReplayBlockedReason({
      demoMode: "replay",
      levelId: "level4_agentic",
      personaId: "engineering_analyst",
      questionId: "q_amendment_rate_comparison",
      synthesisModel: "gemini-4-hydrogen",
      judgeModel: "claude-opus-5-5",
      demos: SAMPLE_DEMOS,
    }),
    null
  );

  // Unrecorded question under replay returns helpful hint naming recorded demos
  const missing = aclReplayBlockedReason({
    demoMode: "replay",
    levelId: "level4_agentic",
    personaId: "engineering_analyst",
    questionId: "q_exact_clin_lookup",
    synthesisModel: "gemini-4-hydrogen",
    judgeModel: "claude-opus-5-5",
    demos: SAMPLE_DEMOS,
  });
  assert.ok(missing && missing.includes("q_amendment_rate_comparison"));
});

test("document viewer URL builders append ?persona= only for restricted personas", () => {
  const citation = {
    solicitation_id: "SOL-2026-001",
    doc_name: "05_Pricing_Schedule_CLINs.pdf",
    excerpt: "CLIN 0001 Senior Cloud Architect",
  };
  assert.equal(pageImageUrl(citation, 1, 110).includes("persona="), false);
  assert.equal(pageImageUrl(citation, 1, 110, "all_access").includes("persona="), false);
  assert.equal(pageImageUrl(citation, 1, 110, "engineering_analyst").includes("persona=engineering_analyst"), true);
  assert.equal(highlightsUrl(citation, 1, "all_access").includes("persona="), false);
  assert.equal(highlightsUrl(citation, 1, "engineering_analyst").includes("persona=engineering_analyst"), true);
});

test("reduceStreamEvent captures run_started ACL metadata and RunPills/RunStatusPills render ACL badge", () => {
  const next = reduceStreamEvent(INITIAL_STREAM_STATE, {
    event: "run_started",
    run_id: "run_acl123",
    engines: ["level4_agentic"],
    demo_mode: "replay",
    question_id: "q_amendment_rate_comparison",
    synthesis_model: "gemini-4-hydrogen",
    judge_model: "claude-opus-5-5",
    access_persona: "engineering_analyst",
    caller_groups: ["corp_all"],
    acl_restricted: true,
    ts: "2026-11-20T00:00:00Z",
  });
  assert.equal(next.accessPersona, "engineering_analyst");
  assert.deepEqual([...next.callerGroups], ["corp_all"]);
  assert.equal(next.aclRestricted, true);

  const levelHtml = renderToStaticMarkup(
    React.createElement(RunPills, {
      state: next,
      timeline: next.timelines.level4_agentic,
      isStreaming: false,
      synthLabel: "Gemini 4 Hydrogen",
      judgeLabel: "Claude Opus 5.5",
    })
  );
  assert.ok(levelHtml.includes("acl-run-pill"));
  assert.ok(levelHtml.includes("ACL: Engineering"));
  assert.ok(levelHtml.includes("corp_all"));

  const compareHtml = renderToStaticMarkup(
    React.createElement(RunStatusPills, {
      state: next,
      isStreaming: false,
      labelFor: (id) => id,
    })
  );
  assert.ok(compareHtml.includes("acl-run-pill"));
  assert.ok(compareHtml.includes("corp_all"));
});

test("AccessPersonaSelect renders honest doc counts, mechanism pills, replay hints, and RagCorpus warning", () => {
  const unrestrictedHtml = renderToStaticMarkup(
    React.createElement(AccessPersonaSelect, {
      acl: makeAclState("all_access"),
      levelId: "level4_agentic",
      demoMode: "replay",
    })
  );
  assert.ok(unrestrictedHtml.includes("45/45 docs"));
  assert.ok(unrestrictedHtml.includes("ADK closure-pinned BigQuery RLS"));
  assert.equal(unrestrictedHtml.includes("acl-rag-engine-warning"), false);

  const restrictedAdkHtml = renderToStaticMarkup(
    React.createElement(AccessPersonaSelect, {
      acl: makeAclState("engineering_analyst"),
      levelId: "level4_agentic",
      demoMode: "replay",
    })
  );
  assert.ok(restrictedAdkHtml.includes("10/45 docs"));
  assert.ok(restrictedAdkHtml.includes("acl-replay-demo-hint"));
  assert.ok(restrictedAdkHtml.includes("q_amendment_rate_comparison"));

  const restrictedRagEngineHtml = renderToStaticMarkup(
    React.createElement(AccessPersonaSelect, {
      acl: makeAclState("engineering_analyst"),
      levelId: "rag_engine",
      demoMode: "replay",
    })
  );
  assert.ok(restrictedRagEngineHtml.includes("acl-rag-engine-warning"));
  assert.ok(restrictedRagEngineHtml.includes("Corpus-boundary IAM only"));
});

test("tcoModel strict_document_acls posture and 6th preset mirror backend parity and render in ArchitectureWizard", () => {
  assert.ok(COMPLIANCE_POSTURE_OPTIONS.includes("strict_document_acls"));
  const aclPreset = ENTERPRISE_SCENARIO_PRESETS.find((p) => p.preset_id === "enterprise_document_acls");
  assert.ok(aclPreset, "6th enterprise_document_acls preset must exist");

  const wl = parseTcoWorkload(aclPreset.workload as unknown as Record<string, unknown>);
  const est = estimateEnterpriseTco(wl);
  const byId = Object.fromEntries(est.architectures.map((a) => [a.level_id, a]));
  assert.ok(byId.level3_multimodal.rationale.some((r) => r.includes("UNNEST(@caller_groups)")));
  assert.ok(byId.alloydb_scann.rationale.some((r) => r.includes("Row-Level Security")));
  assert.ok(byId.rag_engine.tradeoffs.some((t) => t.includes("RagCorpus boundary")));
  assert.ok(byId.level3_multimodal.fit_score > byId.rag_engine.fit_score);

  const wizardHtml = renderToStaticMarkup(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(ArchitectureWizard, {
        workload: wl,
        onChange: () => undefined,
        estimate: est,
        levels: [],
        leaderboardLoading: false,
        onCompareCandidates: () => undefined,
        showAdvanced: true,
      })
    )
  );
  assert.ok(wizardHtml.includes("wizard-preset-enterprise_document_acls"));
  assert.ok(wizardHtml.includes("Strict Document ACLs"));
});
