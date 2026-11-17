/**
 * Slice #6 TDD suite — Industry Benchmark Packs, Executive Decision Brief Export, and Replay Speed Toggle.
 * Run with `cd frontend && npm test`.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server.browser";

import {
  GOLDEN_BADGE_LABEL,
  INDUSTRY_PACKS,
  INDUSTRY_PACK_IDS,
  LIVE_PROMPT_TEMPLATES,
  LIVE_TEMPLATE_BADGE_LABEL,
  REPLAY_LIVE_TEMPLATE_REASON,
  classifyGoldenQuestionPack,
  filterPackItems,
  resolvePackSelection,
} from "../src/features/packs/industryPacks";
import {
  buildExecutiveCsv,
  buildExecutiveMarkdown,
  escapeCsvCell,
  fromCompareRows,
  fromLeaderboardGroups,
  gcpStackForEngine,
  redactSafeText,
} from "../src/features/export/executiveBrief";
import { ExportBriefMenu } from "../src/features/export/ExportBriefMenu";
import {
  DEFAULT_REPLAY_SPEED,
  REPLAY_SPEED_MULTIPLIER,
  REPLAY_SPEED_OPTIONS,
  REPLAY_SPEED_STORAGE_KEY,
  ReplaySpeedSelect,
  parseReplaySpeed,
  replaySpeedMultiplier,
  replaySpeedWireValue,
} from "../src/features/levels/ReplaySpeedSelect";
import { CompareQuestionFields } from "../src/features/compare/CompareQuestionFields";
import type { ScoreRow } from "../src/features/compare/compareModel";
import type { LeaderboardGroup } from "../src/pages/leaderboardModel";
import type { BenchmarkQuestion, EngineTimeline } from "../src/types/arena";

const REPO_ROOT = fs.existsSync(path.join(process.cwd(), "data"))
  ? process.cwd()
  : path.resolve(process.cwd(), "..");

// Synthetic identity fixture (never real GCP identifiers; project number uses 5 digits).
const SYNTHETIC_HEALTH = {
  project_id: "acme-rag-demo",
  gcs_bucket: "acme-rag-demo-corpus",
  upload_bucket: "acme-rag-demo-uploads",
};

test("Industry Packs: defines all 5 canonical packs and maps all 8 golden benchmarks without altering IDs", () => {
  assert.deepEqual(INDUSTRY_PACK_IDS, [
    "all",
    "federal_procurement",
    "sla_financial",
    "security_soc",
    "legal_cuad",
  ]);
  assert.equal(INDUSTRY_PACKS.length, 5);
  assert.equal(GOLDEN_BADGE_LABEL, "Recorded (Replay + Live)");
  assert.equal(LIVE_TEMPLATE_BADGE_LABEL, "Live Template");

  const benchmarkPath = path.join(REPO_ROOT, "data", "golden_benchmarks", "procurement_benchmark.json");
  const parsed = JSON.parse(fs.readFileSync(benchmarkPath, "utf-8")) as {
    questions: readonly BenchmarkQuestion[];
  };
  assert.equal(parsed.questions.length, 8, "8 canonical golden questions remain intact");

  const packCounts: Record<string, number> = {};
  for (const q of parsed.questions) {
    const packId = classifyGoldenQuestionPack(q);
    assert.notEqual(packId, "all");
    packCounts[packId] = (packCounts[packId] ?? 0) + 1;
  }
  assert.equal(packCounts.federal_procurement, 3);
  assert.equal(packCounts.sla_financial, 3);
  assert.equal(packCounts.security_soc, 2);

  const allFiltered = filterPackItems("all", parsed.questions);
  assert.equal(allFiltered.recordedQuestions.length, 8);
  assert.ok(allFiltered.liveTemplates.length >= 5);

  const legalFiltered = filterPackItems("legal_cuad", parsed.questions);
  assert.equal(legalFiltered.recordedQuestions.length, 0);
  assert.ok(legalFiltered.liveTemplates.length >= 5);
  const verticals = new Set(legalFiltered.liveTemplates.map((t) => t.vertical));
  assert.ok(verticals.has("Legal M&A"), "includes Legal M&A CUAD templates");
  assert.ok(verticals.has("Healthcare / Biopharma"), "includes Healthcare CUAD templates");
  assert.ok(verticals.has("FSI / Banking"), "includes FSI CUAD templates");
});

test("Industry Packs: every curated Live Template references a real CUAD PDF present in data/sample_docs", () => {
  const corpusRoot = path.join(REPO_ROOT, "data", "sample_docs", "procurement_corpus");
  assert.ok(LIVE_PROMPT_TEMPLATES.length >= 5);
  for (const tpl of LIVE_PROMPT_TEMPLATES) {
    const pdfPath = path.join(corpusRoot, tpl.solicitationDir, tpl.sourceDoc);
    assert.equal(
      fs.existsSync(pdfPath),
      true,
      `Live Template ${tpl.id} must reference real corpus file: ${tpl.solicitationDir}/${tpl.sourceDoc}`,
    );
    assert.ok(tpl.prompt.length >= 40, `Live Template ${tpl.id} must have a substantive grounded prompt`);
  }
});

test("Industry Packs: Live Templates populate custom query in live mode and block cleanly in replay mode", () => {
  const benchmarkPath = path.join(REPO_ROOT, "data", "golden_benchmarks", "procurement_benchmark.json");
  const { questions } = JSON.parse(fs.readFileSync(benchmarkPath, "utf-8")) as {
    questions: readonly BenchmarkQuestion[];
  };
  const golden = questions[0];
  const tpl = LIVE_PROMPT_TEMPLATES[0];

  const pickedGoldenReplay = resolvePackSelection(`golden:${golden.question_id}`, questions, "replay");
  assert.ok(pickedGoldenReplay);
  assert.equal(pickedGoldenReplay.kind, "recorded");
  assert.equal(pickedGoldenReplay.blockedInReplay, false);
  assert.equal(pickedGoldenReplay.questionId, golden.question_id);
  assert.equal(pickedGoldenReplay.query, golden.question);

  const pickedLiveInLive = resolvePackSelection(`tpl:${tpl.id}`, questions, "live");
  assert.ok(pickedLiveInLive);
  assert.equal(pickedLiveInLive.kind, "live_template");
  assert.equal(pickedLiveInLive.blockedInReplay, false);
  assert.equal(pickedLiveInLive.questionId, "");
  assert.equal(pickedLiveInLive.query, tpl.prompt);

  const pickedLiveInReplay = resolvePackSelection(`tpl:${tpl.id}`, questions, "replay");
  assert.ok(pickedLiveInReplay);
  assert.equal(pickedLiveInReplay.kind, "live_template");
  assert.equal(pickedLiveInReplay.blockedInReplay, true);
  assert.equal(pickedLiveInReplay.reason, REPLAY_LIVE_TEMPLATE_REASON);
  assert.match(REPLAY_LIVE_TEMPLATE_REASON, /DEMO_MODE=live/i);
});

test("redactSafeText masks project IDs, buckets, project numbers, upload IDs, gs:// URIs, and emails in SafeMode", () => {
  const raw =
    "Querying `acme-rag-demo.rag_arena.document_chunks_v3` from gs://acme-rag-demo-corpus/docs and " +
    "gs://acme-rag-demo-uploads/upl_0123456789ab/file.pdf via projects/76543/locations/us-central1 by ops@example.org";

  assert.equal(redactSafeText(raw, { safeMode: false, health: SYNTHETIC_HEALTH }), raw);

  const redacted = redactSafeText(raw, { safeMode: true, health: SYNTHETIC_HEALTH });
  assert.equal(redacted.includes("acme-rag-demo"), false);
  assert.equal(redacted.includes("76543"), false);
  assert.equal(redacted.includes("upl_0123456789ab"), false);
  assert.equal(redacted.includes("ops@example.org"), false);
  assert.ok(redacted.includes("${GCP_PROJECT_ID}"));
  assert.ok(redacted.includes("${GCS_CORPUS_BUCKET}"));
  assert.ok(redacted.includes("${UPLOAD_BUCKET}"));
  assert.ok(redacted.includes("projects/${GCP_PROJECT_NUMBER}"));
});

test("escapeCsvCell & buildExecutiveCsv: RFC-4180 compliant and blocks =, +, -, @, tab, CR, and whitespace-prefixed formula injection", () => {
  assert.equal(escapeCsvCell("=SUM(A1:A5)"), "'=SUM(A1:A5)");
  assert.equal(escapeCsvCell("+1+2"), "'+1+2");
  assert.equal(escapeCsvCell("-cmd|' /C calc'!A0"), "'-cmd|' /C calc'!A0");
  assert.equal(escapeCsvCell("@SUM(A1:A2)"), "'@SUM(A1:A2)");
  assert.equal(escapeCsvCell("@IMPORTXML(A1,A2)"), "\"'@IMPORTXML(A1,A2)\"");
  assert.equal(escapeCsvCell("\t=1+1"), "'\t=1+1");
  assert.equal(escapeCsvCell("\r=1+1"), "\"'\r=1+1\"");
  assert.equal(escapeCsvCell("  =HYPERLINK(\"http://bad\", \"x\")"), "\"'  =HYPERLINK(\"\"http://bad\"\", \"\"x\"\")\"");
  assert.equal(escapeCsvCell("Level 2, \"Hybrid\" RRF"), "\"Level 2, \"\"Hybrid\"\" RRF\"");
  assert.equal(escapeCsvCell("86.4"), "86.4");

  const groups: readonly LeaderboardGroup[] = [
    {
      key: "gemini-4-argon|claude-opus-5-5",
      synthesisModel: "gemini-4-argon",
      judgeModel: "claude-opus-5-5",
      pairKnown: true,
      note: "=UNTRUSTED_NOTE_FROM_QUERY",
      rows: [
        {
          rank: 1,
          row: {
            engine: "level2_hybrid",
            level_label: "Level 2: BigQuery Hybrid + Reranker (acme-rag-demo)",
            runs: 8,
            failures: 0,
            avg_qa_relevance: 0.912,
            avg_groundedness: 0.955,
            avg_context_precision: 0.875,
            avg_latency_ms: 3420,
            avg_engine_cost_usd: 0.0095,
            avg_judge_cost_usd: 0.031,
            avg_cost_usd: 0.0405,
            avg_battle_score: 89.4,
          },
        },
      ],
    },
  ];

  const model = fromLeaderboardGroups(groups, {
    title: "Leaderboard Cohort Brief",
    demoMode: "replay",
    scope: "golden",
    generatedAt: "2026-11-17T17:00:00Z",
  });
  const csv = buildExecutiveCsv(model, { safeMode: true, health: SYNTHETIC_HEALTH });
  const lines = csv.trim().split("\r\n");
  assert.equal(lines.length, 2);
  assert.match(lines[0], /QA/);
  assert.match(lines[0], /Groundedness/);
  assert.match(lines[0], /Context Precision/);
  assert.match(lines[0], /Engine Latency/);
  assert.match(lines[0], /Engine Cost/);
  assert.match(lines[0], /Judge Cost/);
  assert.match(lines[0], /Battle Score/);
  assert.equal(csv.includes("acme-rag-demo"), false, "SafeMode masks project id in CSV");
  assert.ok(lines[1].includes("0.0095"), "exports measured engine cost");
  assert.ok(lines[1].includes("0.0310"), "exports measured judge cost");
  assert.ok(lines[1].includes("89.4"), "exports measured battle score");
});

test("buildExecutiveMarkdown: generates executive summary, recommended GCP architecture, split costs, stack & $0/mo vs ScaNN TCO notes", () => {
  const rows: readonly ScoreRow[] = [
    {
      engine: "level4_agentic",
      short: "Level 4",
      label: "Agentic RAG (Google ADK)",
      status: "scored",
      battleScore: 92.1,
      breakdown: null,
      groundedness: 0.96,
      qaRelevance: 0.95,
      contextPrecision: 0.89,
      latencyMs: 11200,
      costUsd: 0.052,
      judgeModel: "claude-opus-5-5",
      error: null,
      rank: 1,
    },
    {
      engine: "alloydb_scann",
      short: "Level 7",
      label: "AlloyDB + ScaNN Hybrid",
      status: "scored",
      battleScore: 86.3,
      breakdown: null,
      groundedness: 0.91,
      qaRelevance: 0.88,
      contextPrecision: 0.84,
      latencyMs: 1450,
      costUsd: 0.038,
      judgeModel: "claude-opus-5-5",
      error: null,
      rank: 2,
    },
  ];

  const timelines: Readonly<Record<string, EngineTimeline>> = {
    level4_agentic: {
      engine: "level4_agentic",
      stages: {},
      stageOrder: [],
      agentTurns: [],
      answer: {
        answer: "ok",
        citations: [],
        retrievedChunks: [],
        latencyMs: 11200,
        costUsd: 0.021,
        usage: { prompt_tokens: 100, output_tokens: 50, embedding_tokens: 10 },
        flags: { reranker_used: true, adk_runner_used: true, layout_source: null },
      },
      evaluation: {
        metrics: { groundedness: 0.96, qa_relevance: 0.95, context_precision: 0.89 },
        judgeModel: "claude-opus-5-5",
        referenceAnswerUsed: true,
        durationMs: 900,
        judgeCostUsd: 0.031,
        runCostUsd: 0.052,
        battleScore: 92.1,
      },
      error: null,
    },
    alloydb_scann: {
      engine: "alloydb_scann",
      stages: {},
      stageOrder: [],
      agentTurns: [],
      answer: {
        answer: "ok",
        citations: [],
        retrievedChunks: [],
        latencyMs: 1450,
        costUsd: 0.008,
        usage: { prompt_tokens: 80, output_tokens: 40, embedding_tokens: 10 },
        flags: { reranker_used: true, adk_runner_used: false, layout_source: null },
      },
      evaluation: {
        metrics: { groundedness: 0.91, qa_relevance: 0.88, context_precision: 0.84 },
        judgeModel: "claude-opus-5-5",
        referenceAnswerUsed: true,
        durationMs: 850,
        judgeCostUsd: 0.030,
        runCostUsd: 0.038,
        battleScore: 86.3,
      },
      error: null,
    },
  };

  const model = fromCompareRows(rows, {
    timelines,
    synthesisModel: "gemini-4-argon",
    judgeModel: "claude-opus-5-5",
    questionId: "q_amendment_rate_comparison",
    query: "Compare amended CLIN rates in project acme-rag-demo (projects/76543)",
    runId: "run_123",
    demoMode: "replay",
    generatedAt: "2026-11-17T17:30:00Z",
  });

  const md = buildExecutiveMarkdown(model, { safeMode: true, health: SYNTHETIC_HEALTH });
  assert.equal(md.includes("acme-rag-demo"), false, "SafeMode redacts project IDs in Markdown");
  assert.equal(md.includes("projects/76543"), false, "SafeMode redacts project numbers in Markdown");
  assert.ok(md.includes("${GCP_PROJECT_ID}"));
  assert.match(md, /## Executive Summary/);
  assert.match(md, /Recommended GCP RAG Architecture[\s\S]*Agentic/i);
  assert.match(md, /\|\s*Rank\s*\|\s*Architecture\s*\|\s*QA\s*\|\s*Groundedness\s*\|\s*Context Precision\s*\|\s*Engine Latency\s*\|\s*Engine Cost\s*\|\s*Judge Cost\s*\|\s*Battle Score\s*\|/);
  assert.match(md, /\$0\.0210/, "shows split Engine Cost from timeline");
  assert.match(md, /\$0\.0310/, "shows split Judge Cost from timeline");
  assert.match(md, /\$0\/mo/, "includes $0/mo serverless idle vs dedicated ScaNN TCO notes");
  assert.match(md, /ScaNN/, "includes dedicated ScaNN TCO notes");
  assert.ok(gcpStackForEngine("vertex_vector_v2").gcpServices.includes("Vector Search 2.0"));
});

test("ReplaySpeedSelect: parses 1x / 4x / Instant multipliers and omits wire param outside replay mode", () => {
  assert.equal(REPLAY_SPEED_STORAGE_KEY, "rag-arena.replay-speed");
  assert.equal(DEFAULT_REPLAY_SPEED, "1");
  assert.deepEqual(REPLAY_SPEED_MULTIPLIER, { "1": 1.0, "4": 4.0, instant: 100.0 });
  assert.equal(REPLAY_SPEED_OPTIONS.length, 3);

  assert.equal(parseReplaySpeed("1"), "1");
  assert.equal(parseReplaySpeed("4"), "4");
  assert.equal(parseReplaySpeed("instant"), "instant");
  assert.equal(parseReplaySpeed(100), "instant");
  assert.equal(parseReplaySpeed("bogus"), "1");

  assert.equal(replaySpeedMultiplier("1"), 1.0);
  assert.equal(replaySpeedMultiplier("4"), 4.0);
  assert.equal(replaySpeedMultiplier("instant"), 100.0);

  assert.equal(replaySpeedWireValue("replay", "1"), 1.0);
  assert.equal(replaySpeedWireValue("replay", "4"), 4.0);
  assert.equal(replaySpeedWireValue("replay", "instant"), 100.0);
  assert.equal(replaySpeedWireValue("live", "instant"), undefined);
  assert.equal(replaySpeedWireValue("record", "4"), undefined);

  const replayHtml = renderToStaticMarkup(React.createElement(ReplaySpeedSelect, { demoMode: "replay", value: "4" }));
  assert.match(replayHtml, /1×/);
  assert.match(replayHtml, /4×/);
  assert.match(replayHtml, /Instant/);
  assert.match(replayHtml, /aria-pressed="true"/);

  const liveHtml = renderToStaticMarkup(React.createElement(ReplaySpeedSelect, { demoMode: "live", value: "4" }));
  assert.equal(liveHtml, "", "ReplaySpeedSelect hides cleanly in DEMO_MODE=live by default");
});

test("SSR & CSS hygiene: ExportBriefMenu, CompareQuestionFields pack bar, @media print rules, and zero raw hex outside tokens.css", () => {
  const briefModel = fromLeaderboardGroups([], { title: "Empty Brief", demoMode: "replay" });
  const exportHtml = renderToStaticMarkup(React.createElement(ExportBriefMenu, { brief: briefModel }));
  assert.match(exportHtml, /Export Brief/);
  assert.match(exportHtml, /aria-haspopup="menu"/);
  assert.match(exportHtml, /data-print-only="true"/);

  const replayFieldsHtml = renderToStaticMarkup(
    React.createElement(CompareQuestionFields, {
      value: { query: "", questionId: "" },
      onChange: () => {},
      disabled: false,
      demoMode: "replay",
    }),
  );
  assert.match(replayFieldsHtml, /Industry benchmark pack/);
  assert.match(replayFieldsHtml, /Legal &amp; CUAD/);
  assert.match(replayFieldsHtml, /requires DEMO_MODE=live/);

  const liveFieldsHtml = renderToStaticMarkup(
    React.createElement(CompareQuestionFields, {
      value: { query: LIVE_PROMPT_TEMPLATES[0].prompt, questionId: "" },
      onChange: () => {},
      disabled: false,
      demoMode: "live",
    }),
  );
  assert.match(liveFieldsHtml, /Live Template · Legal M&amp;A/);
  assert.match(liveFieldsHtml, /ArconicRolledProductsCorp/);

  const cssPath = path.join(REPO_ROOT, "frontend", "src", "index.css");
  const cssText = fs.readFileSync(cssPath, "utf-8");
  assert.match(cssText, /@media print/, "index.css must define @media print executive report styles");
  assert.equal(/#[0-9a-fA-F]{3,8}\b/.test(cssText), false, "index.css must not contain raw hex colors");
});

test("Markdown XSS/table-injection neutralization and empty-cohort honesty invariants", () => {
  const emptyModel = fromLeaderboardGroups([], { title: "Honest Empty Brief", demoMode: "replay" });
  const emptyMd = buildExecutiveMarkdown(emptyModel, { safeMode: true, health: SYNTHETIC_HEALTH });
  assert.match(emptyMd, /No measured cohort runs are loaded yet/);
  assert.match(emptyMd, /_No measured rows matched the active filters\._/);

  const untrustedModel = fromCompareRows(
    [
      {
        engine: "level1_naive",
        short: "Level 1",
        label: "Naive | Injected <img src=x>",
        status: "scored",
        battleScore: 71.2,
        breakdown: null,
        groundedness: 0.8,
        qaRelevance: 0.75,
        contextPrecision: 0.7,
        latencyMs: 1200,
        costUsd: 0.01,
        judgeModel: "claude-opus-5-5",
        error: null,
        rank: 1,
      },
    ],
    {
      query: "Line 1\nLine 2 | <svg onload=bad()> in upl_abcdef012345",
      demoMode: "live",
      generatedAt: "2026-11-17T18:00:00Z",
    },
  );
  const safeMd = buildExecutiveMarkdown(untrustedModel, { safeMode: true, health: SYNTHETIC_HEALTH });
  assert.equal(safeMd.includes("<svg"), false, "HTML angle brackets are escaped in Markdown output");
  assert.equal(safeMd.includes("<img"), false, "HTML angle brackets in row labels are escaped");
  assert.equal(safeMd.includes("upl_abcdef012345"), false, "Upload workspace IDs are masked in SafeMode");
  assert.ok(safeMd.includes("upl_redacted"));
  assert.ok(safeMd.includes("Naive \\| Injected &lt;img src=x&gt;"));
});

test("Owned Slice #6 source files uphold strict security, typography (>=12px), and zero-leak rules", () => {
  const ownedFiles = [
    "frontend/src/features/packs/industryPacks.ts",
    "frontend/src/features/export/executiveBrief.ts",
    "frontend/src/features/export/ExportBriefMenu.tsx",
    "frontend/src/features/levels/ReplaySpeedSelect.tsx",
    "frontend/src/features/levels/QuestionPicker.tsx",
    "frontend/src/features/compare/CompareQuestionFields.tsx",
    "frontend/src/index.css",
  ];
  const bannedSubstrings = [
    "dangerously" + "SetInnerHTML",
    ".inner" + "HTML",
    "ev" + "al(",
    "al" + "ert(",
    "text-[10" + "px]",
    "text-[11" + "px]",
    "/usr/local/" + "google/home",
  ];
  for (const rel of ownedFiles) {
    const text = fs.readFileSync(path.join(REPO_ROOT, rel), "utf-8");
    for (const banned of bannedSubstrings) {
      assert.equal(text.includes(banned), false, `Forbidden pattern ${banned} in ${rel}`);
    }
    assert.equal(/projects\/\d{6,}/.test(text), false, `6+ digit project number in ${rel}`);
  }
});
