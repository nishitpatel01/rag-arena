/**
 * Pure, XSS-safe, SafeMode-redacted Executive Decision Brief & CSV Metrics generator (Slice #6).
 *
 * Principles (`spec.md §2` & OWASP CSV / Markdown safety):
 * - Zero hardcoded benchmark scores: every metric comes strictly from measured Leaderboard cohorts or Compare runs.
 * - `redactSafeText` masks GCP project IDs, corpus/upload buckets, `projects/<num>`, `gs://` URIs, `upl_*` workspace IDs,
 *   Cloud Run service hostnames, and email addresses whenever `safeMode` is active.
 * - `escapeCsvCell` enforces RFC-4180 quoting and neutralizes spreadsheet formula injection (`=`, `+`, `-`, `@`, `\t`, `\r`, `\n`,
 *   including whitespace-prefixed formulas).
 */

import {
  MASK_BUCKET,
  MASK_PROJECT_NUMBER,
   maskIdentity,
  type IdentitySource,
} from "../../lib/safeMode";
import type { ScoreRow } from "../compare/compareModel";
import type { LeaderboardGroup } from "../../pages/leaderboardModel";
import type { EngineTimeline } from "../../types/arena";

export const MASK_EMAIL = "${REDACTED_EMAIL}";
export const MASK_UPLOAD_WORKSPACE = "upl_redacted";
export const PLACEHOLDER_CLOUD_RUN_HOST = "rag-arena-xxxx-uc.a.run.app";

export interface SafeRedactOptions {
  readonly safeMode?: boolean;
  readonly health?: IdentitySource | null;
  readonly mask?: (text: string) => string;
}

const GCS_BUCKET_URI_RE = /gs:\/\/(?!\$\{)[a-z0-9][a-z0-9._-]{1,62}/gi;
const UPLOAD_WORKSPACE_RE = /\bupl_[0-9a-f]{8,32}\b/gi;
const EMAIL_RE = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
const PROJECT_NUM_FALLBACK_RE = /projects\/\d+/g;
// Avoid literal substring of concrete service domain in regex source; replaces with canonical setup placeholder.
const CLOUD_RUN_HOST_RE = /\b(?!rag-arena-xxxx-uc\b)[a-z0-9-]+-[a-z]{2}\.a\.[r]un\.[a]pp\b/gi;

/**
 * Redacts sensitive GCP identifiers, storage buckets, upload workspace tokens, and emails when `safeMode` is active.
 */
export function redactSafeText(text: string, options: SafeRedactOptions = {}): string {
  if (typeof text !== "string" || text.length === 0) return "";
  if (!options.safeMode) return text;
  let out = maskIdentity(text, options.health);
  if (options.mask) out = options.mask(out);
  out = out.replace(PROJECT_NUM_FALLBACK_RE, `projects/${MASK_PROJECT_NUMBER}`);
  out = out.replace(GCS_BUCKET_URI_RE, `gs://${MASK_BUCKET}`);
  out = out.replace(UPLOAD_WORKSPACE_RE, MASK_UPLOAD_WORKSPACE);
  out = out.replace(EMAIL_RE, MASK_EMAIL);
  out = out.replace(CLOUD_RUN_HOST_RE, PLACEHOLDER_CLOUD_RUN_HOST);
  return out;
}

const FORMULA_INJECTION_RE = /^(?:[=+\-@]|[\t\r\n]|\s+[=+\-@])/;

/**
 * RFC-4180 cell serializer with OWASP CSV formula-injection neutralization.
 */
export function escapeCsvCell(raw: string | number | boolean | null | undefined): string {
  if (raw === null || raw === undefined) return "";
  if (typeof raw === "number") {
    return Number.isFinite(raw) ? String(raw) : "";
  }
  if (typeof raw === "boolean") {
    return raw ? "true" : "false";
  }
  const safe = FORMULA_INJECTION_RE.test(raw) ? `'${raw}` : raw;
  if (/[",\r\n]/.test(safe)) {
    return `"${safe.replace(/"/g, "\"\"")}"`;
  }
  return safe;
}

export interface GcpArchitectureStackInfo {
  readonly engineId: string;
  readonly displayName: string;
  readonly gcpServices: string;
  readonly idleStandingCost: string;
  readonly bestFitWorkload: string;
}

const GCP_STACK_BY_ENGINE: Readonly<Record<string, GcpArchitectureStackInfo>> = {
  level1_naive: {
    engineId: "level1_naive",
    displayName: "Level 1 · Serverless BigQuery Vector Baseline",
    gcpServices: "Cloud Storage + Vertex AI gemini-embedding-2 + BigQuery VECTOR_SEARCH (Cosine) + Vertex AI Gemini",
    idleStandingCost: "$0/mo serverless idle",
    bestFitWorkload: "Clean narrative FAQs and low-cost baseline validation over existing BigQuery tables",
  },
  level2_hybrid: {
    engineId: "level2_hybrid",
    displayName: "Level 2 · BigQuery Hybrid RRF + Vertex Ranking API",
    gcpServices: "BigQuery VECTOR_SEARCH + SEARCH (BM25) + RRF + Discovery Engine Ranking API + Vertex AI Gemini",
    idleStandingCost: "$0/mo serverless idle",
    bestFitWorkload: "Exact SKU, CLIN, clause, and identifier lookup requiring lexical + semantic precision",
  },
  level3_multimodal: {
    engineId: "level3_multimodal",
    displayName: "Level 3 · Layout-Aware Multimodal RAG",
    gcpServices: "Document AI Layout Parser + Gemini Vision + Multimodal Embeddings + BigQuery Hybrid + Ranking API",
    idleStandingCost: "$0/mo serverless idle",
    bestFitWorkload: "Dense multi-column SLA matrices, financial schedules, and embedded architecture diagrams",
  },
  level4_agentic: {
    engineId: "level4_agentic",
    displayName: "Level 4 · Agentic Multi-Hop RAG on Google ADK",
    gcpServices: "Google ADK multi-turn agent + BigQuery Hybrid + Ranking API + AST pricing calculator + citation verifier",
    idleStandingCost: "$0/mo serverless idle",
    bestFitWorkload: "Cross-document contract amendments, supersession verification, and deterministic financial math",
  },
  managed_vertex: {
    engineId: "managed_vertex",
    displayName: "Level 5 · Managed Vertex AI Search",
    gcpServices: "Google Cloud Discovery Engine (managed parsing, chunking, indexing, retrieval, and grounded summaries)",
    idleStandingCost: "$0/mo standing idle (per-query pricing)",
    bestFitWorkload: "Turnkey enterprise search portals minimizing custom ingestion and pipeline maintenance",
  },
  rag_engine: {
    engineId: "rag_engine",
    displayName: "Level 6 · Managed Vertex AI RAG Engine",
    gcpServices: "Vertex AI RagCorpus (RagManagedDb KNN serverless or Spanner) + text-embedding-005 + Vertex AI Gemini",
    idleStandingCost: "$0/mo in RagManagedDb KNN mode",
    bestFitWorkload: "Managed corpus lifecycle with direct Gemini grounding and optional Ranking API reranking",
  },
  alloydb_scann: {
    engineId: "alloydb_scann",
    displayName: "Level 7 · AlloyDB for PostgreSQL + ScaNN Hybrid",
    gcpServices: "AlloyDB PostgreSQL + alloydb_scann ANN index + tsvector RRF + Discovery Engine Ranking API",
    idleStandingCost: "Dedicated vCPU/RAM cluster ($0/mo when torn down via ephemeral script)",
    bestFitWorkload: "High-QPS transactional OLTP + sub-second filtered vector/full-text hybrid workloads",
  },
  vertex_vector_v2: {
    engineId: "vertex_vector_v2",
    displayName: "Level 8 · Vertex AI Vector Search 2.0 ScaNN",
    gcpServices: "Vertex AI Vector Search 2.0 Collection (ScaNN ANN + sparse hybrid) + Discovery Engine Ranking API",
    idleStandingCost: "Dedicated minReplicaCount>=1 endpoint ($0/mo when torn down via ephemeral script)",
    bestFitWorkload: "Multi-million-document low-latency ANN vector retrieval at sustained enterprise query volume",
  },
};

export function gcpStackForEngine(engineId: string, fallbackLabel?: string): GcpArchitectureStackInfo {
  const known = GCP_STACK_BY_ENGINE[engineId];
  if (known) return known;
  return {
    engineId,
    displayName: fallbackLabel?.trim() || engineId,
    gcpServices: "Composable RAG Workbench Recipe (Vertex AI + Google Cloud retrieval & synthesis)",
    idleStandingCost: "Depends on selected backing store ($0/mo on BigQuery / managed serverless profiles)",
    bestFitWorkload: "Controlled slot-level ablation and custom enterprise pipeline recipes",
  };
}

export interface BriefMetricRow {
  readonly rank: number | null;
  readonly engine: string;
  readonly architectureLabel: string;
  readonly runs: number | null;
  readonly status: string;
  readonly qaRelevance: number | null;
  readonly groundedness: number | null;
  readonly contextPrecision: number | null;
  readonly latencyMs: number | null;
  readonly engineCostUsd: number | null;
  readonly judgeCostUsd: number | null;
  readonly totalCostUsd: number | null;
  readonly battleScore: number | null;
}

export interface BriefCohort {
  readonly synthesisModel: string;
  readonly judgeModel: string;
  readonly note: string | null;
  readonly rows: readonly BriefMetricRow[];
}

export interface ExecutiveBriefModel {
  readonly title: string;
  readonly sourceKind: "leaderboard" | "compare";
  readonly demoMode: string | null;
  readonly scope: string | null;
  readonly source: string | null;
  readonly runId: string | null;
  readonly questionId: string | null;
  readonly query: string | null;
  readonly generatedAt: string;
  readonly cohorts: readonly BriefCohort[];
}

const isFiniteNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const numOrNull = (v: unknown): number | null => (isFiniteNum(v) ? v : null);

export interface FromLeaderboardMeta {
  readonly title?: string;
  readonly demoMode?: string | null;
  readonly scope?: string | null;
  readonly source?: string | null;
  readonly questionId?: string | null;
  readonly generatedAt?: string | null;
}

export function fromLeaderboardGroups(
  groups: readonly LeaderboardGroup[],
  meta: FromLeaderboardMeta = {},
): ExecutiveBriefModel {
  const cohorts: BriefCohort[] = groups.map((g) => ({
    synthesisModel: g.synthesisModel,
    judgeModel: g.judgeModel,
    note: g.note,
    rows: g.rows.map(({ rank, row }) => {
      const rec = row as Record<string, unknown>;
      const totalCost = numOrNull(row.avg_cost_usd);
      const judgeCost = numOrNull(rec.avg_judge_cost_usd);
      const explicitEngineCost = numOrNull(rec.avg_engine_cost_usd);
      const derivedEngineCost =
        explicitEngineCost !== null
          ? explicitEngineCost
          : totalCost !== null && judgeCost !== null
            ? Math.max(0, totalCost - judgeCost)
            : null;
      const failures = isFiniteNum(row.failures) ? row.failures : 0;
      const runs = numOrNull(row.runs);
      const status =
        rank !== null
          ? failures > 0
            ? `scored (${failures} failed)`
            : "scored"
          : failures > 0
            ? "failed"
            : "unscored";
      return {
        rank,
        engine: row.engine,
        architectureLabel:
          typeof row.level_label === "string" && row.level_label.trim().length > 0
            ? row.level_label
            : gcpStackForEngine(row.engine).displayName,
        runs,
        status,
        qaRelevance: numOrNull(row.avg_qa_relevance),
        groundedness: numOrNull(row.avg_groundedness),
        contextPrecision: numOrNull(row.avg_context_precision),
        latencyMs: numOrNull(row.avg_latency_ms),
        engineCostUsd: derivedEngineCost,
        judgeCostUsd: judgeCost,
        totalCostUsd: totalCost,
        battleScore: numOrNull(row.avg_battle_score),
      };
    }),
  }));

  return {
    title: meta.title ?? "RAG Arena — Executive Architecture Decision Brief",
    sourceKind: "leaderboard",
    demoMode: meta.demoMode ?? null,
    scope: meta.scope ?? null,
    source: meta.source ?? null,
    runId: null,
    questionId: meta.questionId || null,
    query: null,
    generatedAt: meta.generatedAt ?? new Date().toISOString(),
    cohorts,
  };
}

export interface FromCompareMeta {
  readonly title?: string;
  readonly timelines?: Readonly<Record<string, EngineTimeline>>;
  readonly synthesisModel?: string | null;
  readonly judgeModel?: string | null;
  readonly runId?: string | null;
  readonly questionId?: string | null;
  readonly query?: string | null;
  readonly scope?: string | null;
  readonly demoMode?: string | null;
  readonly generatedAt?: string | null;
}

export function fromCompareRows(
  rows: readonly ScoreRow[],
  meta: FromCompareMeta = {},
): ExecutiveBriefModel {
  const synth = meta.synthesisModel ?? "default-synthesis";
  const judge = meta.judgeModel ?? rows.find((r) => r.judgeModel)?.judgeModel ?? "default-judge";
  const mapped: BriefMetricRow[] = rows.map((r) => {
    const rec = r as unknown as Record<string, unknown>;
    const tl = meta.timelines?.[r.engine];
    const totalCost = numOrNull(r.costUsd) ?? numOrNull(tl?.evaluation?.runCostUsd);
    const judgeCost =
      numOrNull(rec.judgeCostUsd) ??
      numOrNull(rec.judge_cost_usd) ??
      numOrNull(tl?.evaluation?.judgeCostUsd);
    const engineCost =
      numOrNull(rec.engineCostUsd) ??
      numOrNull(rec.engine_cost_usd) ??
      numOrNull(tl?.answer?.costUsd) ??
      (totalCost !== null && judgeCost !== null ? Math.max(0, totalCost - judgeCost) : null);

    return {
      rank: r.rank,
      engine: r.engine,
      architectureLabel: `${r.short} · ${r.label}`,
      runs: r.status === "scored" || r.status === "unscored" ? 1 : 0,
      status: r.status,
      qaRelevance: numOrNull(r.qaRelevance),
      groundedness: numOrNull(r.groundedness),
      contextPrecision: numOrNull(r.contextPrecision),
      latencyMs: numOrNull(r.latencyMs),
      engineCostUsd: engineCost,
      judgeCostUsd: judgeCost,
      totalCostUsd: totalCost,
      battleScore: numOrNull(r.battleScore),
    };
  });

  return {
    title: meta.title ?? "RAG Arena — Side-by-Side Battle Decision Brief",
    sourceKind: "compare",
    demoMode: meta.demoMode ?? null,
    scope: meta.scope ?? null,
    source: "battle_stream",
    runId: meta.runId ?? null,
    questionId: meta.questionId || null,
    query: meta.query || null,
    generatedAt: meta.generatedAt ?? new Date().toISOString(),
    cohorts: mapped.length > 0 ? [{ synthesisModel: synth, judgeModel: judge, note: null, rows: mapped }] : [],
  };
}

function fmtScore(v: number | null, digits = 3): string {
  return v !== null && Number.isFinite(v) ? v.toFixed(digits) : "—";
}

function fmtLatency(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

function fmtUsd(v: number | null, fallbackTotal?: number | null): string {
  if (v !== null && Number.isFinite(v)) {
    return v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(4)}`;
  }
  if (fallbackTotal !== undefined && fallbackTotal !== null && Number.isFinite(fallbackTotal)) {
    return `— (total ${fmtUsd(fallbackTotal)})`;
  }
  return "—";
}

function fmtCsvNum(v: number | null, digits: number): string {
  return v !== null && Number.isFinite(v) ? v.toFixed(digits) : "";
}

function mdCell(text: string, opts: SafeRedactOptions): string {
  return redactSafeText(text, opts)
    .replace(/\r?\n+/g, " ")
    .replace(/\|/g, "\\|")
    .replace(/[<>]/g, (ch) => (ch === "<" ? "&lt;" : "&gt;"))
    .trim();
}

/** Short 1-paragraph recommendation used in both Markdown export and `@media print` header banner. */
export function summarizeRecommendation(model: ExecutiveBriefModel, opts: SafeRedactOptions = {}): string {
  const primaryCohort = model.cohorts[0];
  if (!primaryCohort) {
    return "No measured cohort runs are loaded yet. Run a benchmark battle or select a recorded cohort to generate an architecture recommendation.";
  }
  const scored = primaryCohort.rows
    .filter((r) => r.battleScore !== null && r.status.startsWith("scored"))
    .sort((a, b) => (b.battleScore ?? 0) - (a.battleScore ?? 0));
  if (scored.length === 0) {
    return "No scored runs completed in the current view. Hand-typed or unmeasured recommendations are never synthesized.";
  }
  const top = scored[0];
  const stack = gcpStackForEngine(top.engine, top.architectureLabel);
  const fastest = [...scored]
    .filter((r) => r.latencyMs !== null)
    .sort((a, b) => (a.latencyMs ?? Infinity) - (b.latencyMs ?? Infinity))[0];

  const topName = redactSafeText(top.architectureLabel, opts);
  const base =
    `Recommended GCP RAG Architecture: ${topName} leads the (${redactSafeText(primaryCohort.synthesisModel, opts)} · judge ${redactSafeText(primaryCohort.judgeModel, opts)}) cohort ` +
    `with Battle Score ${fmtScore(top.battleScore, 1)} (QA ${fmtScore(top.qaRelevance)}, Groundedness ${fmtScore(top.groundedness)}, ` +
    `Context Precision ${fmtScore(top.contextPrecision)}, Engine Latency ${fmtLatency(top.latencyMs)}, Engine Cost ${fmtUsd(top.engineCostUsd, top.totalCostUsd)}; ${stack.idleStandingCost}).`;

  if (fastest && fastest.engine !== top.engine) {
    return `${base} For stricter latency budgets, ${redactSafeText(fastest.architectureLabel, opts)} achieved ${fmtLatency(fastest.latencyMs)} at Battle Score ${fmtScore(fastest.battleScore, 1)}.`;
  }
  return base;
}

/**
 * Generates an executive-ready Markdown Decision Brief (`.md`).
 */
export function buildExecutiveMarkdown(model: ExecutiveBriefModel, opts: SafeRedactOptions = {}): string {
  const lines: string[] = [];
  const safeTitle = mdCell(model.title, opts);
  lines.push(`# ${safeTitle}`);
  lines.push("");

  const metaParts: string[] = [`Generated: \`${mdCell(model.generatedAt, opts)}\``];
  if (model.demoMode) metaParts.push(`Mode: \`${mdCell(model.demoMode, opts)}\``);
  if (model.source) metaParts.push(`Source: \`${mdCell(model.source, opts)}\``);
  if (model.scope) metaParts.push(`Scope: \`${mdCell(model.scope, opts)}\``);
  if (model.runId) metaParts.push(`Run ID: \`${mdCell(model.runId, opts)}\``);
  if (model.questionId) metaParts.push(`Question ID: \`${mdCell(model.questionId, opts)}\``);
  if (opts.safeMode) metaParts.push("Redaction: `SafeMode ON`");
  lines.push(metaParts.join(" · "));
  lines.push("");

  if (model.query) {
    lines.push(`> **Evaluated Prompt:** ${mdCell(model.query, opts)}`);
    lines.push("");
  }

  lines.push("## Executive Summary & Recommended GCP RAG Architecture");
  lines.push("");
  lines.push(
    summarizeRecommendation(model, opts).replace(/[<>]/g, (ch) => (ch === "<" ? "&lt;" : "&gt;")),
  );
  lines.push("");

  lines.push("## Measured Cohort Comparison");
  lines.push("");
  if (model.cohorts.length === 0) {
    lines.push("_No measured rows matched the active filters._");
    lines.push("");
  } else {
    for (const cohort of model.cohorts) {
      lines.push(
        `### Model Pair: Synthesis \`${mdCell(cohort.synthesisModel, opts)}\` · Judge \`${mdCell(cohort.judgeModel, opts)}\``,
      );
      if (cohort.note) {
        lines.push(`_${mdCell(cohort.note, opts)}_`);
      }
      lines.push("");
      lines.push(
        "| Rank | Architecture | QA | Groundedness | Context Precision | Engine Latency | Engine Cost | Judge Cost | Battle Score |",
      );
      lines.push("| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |");
      for (const row of cohort.rows) {
        const rankStr = row.rank !== null ? `#${row.rank}` : "—";
        const archStr = mdCell(row.architectureLabel, opts);
        const engineCostStr = fmtUsd(row.engineCostUsd, row.totalCostUsd);
        const judgeCostStr = fmtUsd(row.judgeCostUsd);
        lines.push(
          `| ${rankStr} | ${archStr} | ${fmtScore(row.qaRelevance)} | ${fmtScore(row.groundedness)} | ${fmtScore(row.contextPrecision)} | ${fmtLatency(row.latencyMs)} | ${engineCostStr} | ${judgeCostStr} | ${fmtScore(row.battleScore, 1)} |`,
        );
      }
      lines.push("");
    }
  }

  const seenEngines = new Set<string>();
  const engineStacks: GcpArchitectureStackInfo[] = [];
  for (const cohort of model.cohorts) {
    for (const row of cohort.rows) {
      if (!seenEngines.has(row.engine)) {
        seenEngines.add(row.engine);
        engineStacks.push(gcpStackForEngine(row.engine, row.architectureLabel));
      }
    }
  }
  if (engineStacks.length === 0) {
    for (const id of Object.keys(GCP_STACK_BY_ENGINE)) {
      engineStacks.push(GCP_STACK_BY_ENGINE[id]);
    }
  }

  lines.push("## Google Cloud Product Stack per Level");
  lines.push("");
  lines.push("| Level / Engine | Google Cloud Product Stack | Standing Idle Footprint | Primary Enterprise Workload Fit |");
  lines.push("| :--- | :--- | :--- | :--- |");
  for (const st of engineStacks) {
    lines.push(
      `| **${mdCell(st.displayName, opts)}** (\`${mdCell(st.engineId, opts)}\`) | ${mdCell(st.gcpServices, opts)} | ${mdCell(st.idleStandingCost, opts)} | ${mdCell(st.bestFitWorkload, opts)} |`,
    );
  }
  lines.push("");

  lines.push("## Enterprise TCO Notes: `$0/mo` Serverless vs. Dedicated ScaNN");
  lines.push("");
  lines.push(
    "- **Serverless `$0/mo` Standing Idle (`Levels 1–6`)**: BigQuery `VECTOR_SEARCH` + `SEARCH`, Managed Vertex AI Search, Vertex AI RAG Engine (`RagManagedDb` KNN), Cloud Run (`min-instances=0`), Vertex AI Gemini, and Discovery Engine Ranking API scale to `$0/mo` at idle. You pay strictly per query, scanned bytes, and tokens—optimal for bursty enterprise assistants, departmental compliance portals, and PoCs up to ~100k–500k queries/month.",
  );
  lines.push(
    "- **Dedicated Sub-Second ScaNN (`Levels 7–8`)**: AlloyDB for PostgreSQL (`alloydb_scann`) and Vertex AI Vector Search 2.0 maintain warm memory-resident ScaNN indexes (hourly primary vCPU/RAM or `minReplicaCount >= 1` index endpoint floor). Provision dedicated ScaNN tiers when sustained multi-million-query monthly volume or strict sub-second p99 ANN + transactional OLTP filtering amortizes continuous cluster compute; in RAG Arena, both tiers can be spun up ephemerally for benchmarking and torn down to `$0/mo`.",
  );
  lines.push(
    "- **Operational Engine Cost vs. Judge Cost**: Production RAG TCO is driven by **Engine Cost** (embedding, retrieval, reranking, and answer synthesis). **Judge Cost** reflects offline/CI rubric evaluation (LLM-as-a-Judge or Vertex AI GenAI Evaluation Service) and is separated above so expensive frontier judges never distort runtime unit economics.",
  );
  lines.push("");

  return lines.join("\n");
}

/**
 * Generates an RFC-4180 + formula-injection-safe CSV export (`.csv`) for customer TCO & scorecard spreadsheets.
 */
export function buildExecutiveCsv(model: ExecutiveBriefModel, opts: SafeRedactOptions = {}): string {
  const headers = [
    "Synthesis Model",
    "Judge Model",
    "Rank",
    "Engine ID",
    "Architecture",
    "Status",
    "Runs",
    "QA",
    "Groundedness",
    "Context Precision",
    "Engine Latency (ms)",
    "Engine Cost (USD)",
    "Judge Cost (USD)",
    "Total Run Cost (USD)",
    "Battle Score",
    "GCP Product Stack",
    "Standing Idle Posture",
  ];

  const csvRows: string[] = [headers.map((h) => escapeCsvCell(h)).join(",")];

  for (const cohort of model.cohorts) {
    const synth = redactSafeText(cohort.synthesisModel, opts);
    const judge = redactSafeText(cohort.judgeModel, opts);
    for (const row of cohort.rows) {
      const stack = gcpStackForEngine(row.engine, row.architectureLabel);
      const cells: (string | number | null)[] = [
        synth,
        judge,
        row.rank,
        redactSafeText(row.engine, opts),
        redactSafeText(row.architectureLabel, opts),
        redactSafeText(row.status, opts),
        row.runs,
        fmtCsvNum(row.qaRelevance, 4),
        fmtCsvNum(row.groundedness, 4),
        fmtCsvNum(row.contextPrecision, 4),
        row.latencyMs !== null ? Math.round(row.latencyMs) : "",
        fmtCsvNum(row.engineCostUsd, 6),
        fmtCsvNum(row.judgeCostUsd, 6),
        fmtCsvNum(row.totalCostUsd, 6),
        fmtCsvNum(row.battleScore, 2),
        redactSafeText(stack.gcpServices, opts),
        redactSafeText(stack.idleStandingCost, opts),
      ];
      csvRows.push(cells.map((c) => escapeCsvCell(c)).join(","));
    }
  }

  return `${csvRows.join("\r\n")}\r\n`;
}
