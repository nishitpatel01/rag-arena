import React, { useEffect, useState } from "react";
import { ArrowRight, GitCompare, Lock } from "lucide-react";
import { Link } from "react-router-dom";
import { lockedTitle } from "../../app/Shell";
import { Button } from "../../components/ui/Button";
import { Panel } from "../../components/ui/Panel";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { SegmentedControl } from "../../components/ui/SegmentedControl";
import type { LevelDescriptor } from "../../types/arena";
import {
  ALLOYDB_BASE_MONTHLY_USD,
  RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD,
  VVS2_BASE_REPLICA_MONTHLY_USD,
  formatLatencySeconds,
  formatUsdMonthly,
  sortArchitectures,
  type ArchitectureTcoRow,
  type EnterpriseTcoEstimate,
  type FitTier,
  type TcoSortKey,
} from "./tcoModel";

export interface TcoCalculatorPanelProps {
  readonly estimate: EnterpriseTcoEstimate;
  readonly levels: readonly LevelDescriptor[];
  readonly leaderboardLoading: boolean;
  readonly onCompareCandidates: (levelIds: readonly string[]) => void;
}

const SORT_OPTIONS: readonly { readonly value: TcoSortKey; readonly label: string }[] = [
  { value: "fit_score", label: "Workload Fit" },
  { value: "monthly_tco", label: "Lowest Monthly TCO" },
  { value: "recorded_qa", label: "Recorded QA" },
  { value: "level_order", label: "Levels 1–8" },
];

function fitTierBadge(tier: FitTier): { readonly tone: PillTone; readonly label: string } {
  if (tier === "recommended") return { tone: "ok", label: "Primary" };
  if (tier === "runner_up") return { tone: "info", label: "Runner-up" };
  if (tier === "viable") return { tone: "neutral", label: "Viable" };
  if (tier === "baseline") return { tone: "locked", label: "Baseline" };
  return { tone: "warn", label: "Low fit" };
}

function fmtScore(val: number | null | undefined): string {
  return typeof val === "number" && Number.isFinite(val) ? val.toFixed(3) : "—";
}

export const TcoCalculatorPanel: React.FC<TcoCalculatorPanelProps> = ({
  estimate,
  levels,
  leaderboardLoading,
  onCompareCandidates,
}) => {
  const [sortBy, setSortBy] = useState<TcoSortKey>("fit_score");
  const [customCompareIds, setCustomCompareIds] = useState<readonly string[] | null>(null);

  useEffect(() => {
    setCustomCompareIds(null);
  }, [estimate.recommended_level_id, estimate.runner_up_level_id]);

  const compareIds = customCompareIds ?? [estimate.recommended_level_id, estimate.runner_up_level_id];
  const compareSet = new Set(compareIds);
  const sorted = sortArchitectures(estimate.architectures, sortBy);
  const levelMap = new Map<string, LevelDescriptor>(levels.map((l) => [l.level_id, l]));

  const toggleCompare = (levelId: string): void => {
    const next = compareSet.has(levelId)
      ? compareIds.filter((id) => id !== levelId)
      : [...compareIds, levelId];
    setCustomCompareIds(next);
  };

  return (
    <Panel
      title="Monthly Enterprise GCP TCO & Recorded Benchmark Matrix"
      subtitle={`Estimated for ${estimate.workload.monthly_queries.toLocaleString("en-US")} queries/mo · ${estimate.workload.corpus_pages.toLocaleString("en-US")} PDF pages · ${estimate.workload.synthesis_model}`}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl<TcoSortKey>
            label="Sort TCO matrix by"
            value={sortBy}
            options={SORT_OPTIONS}
            onChange={setSortBy}
          />
          <Button
            size="sm"
            variant="secondary"
            disabled={compareIds.length === 0}
            leadingIcon={<GitCompare className="h-3.5 w-3.5" aria-hidden />}
            onClick={() => onCompareCandidates(compareIds)}
            data-testid="tco-compare-checked"
          >
            Compare selected ({compareIds.length})
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3" data-testid="tco-calculator-panel">
        <div className="overflow-x-auto rounded-md border border-line">
          <table className="w-full min-w-[64rem] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-line bg-surface-2 text-ink-2">
                <th scope="col" className="w-8 px-2.5 py-2 font-semibold">
                  Cmp
                </th>
                <th scope="col" className="px-3 py-2 font-semibold">
                  Architecture (L1–L8)
                </th>
                <th scope="col" className="px-2.5 py-2 text-right font-semibold">
                  Fit
                </th>
                <th scope="col" className="px-2.5 py-2 text-right font-semibold">
                  Standing / mo
                </th>
                <th scope="col" className="px-2.5 py-2 text-right font-semibold">
                  Storage / mo
                </th>
                <th scope="col" className="px-3 py-2 text-right font-semibold">
                  Query-Variable / mo
                </th>
                <th scope="col" className="px-3 py-2 text-right font-semibold">
                  Total Monthly TCO
                </th>
                <th scope="col" className="px-2.5 py-2 text-right font-semibold">
                  1-Time Ingest
                </th>
                <th scope="col" className="px-3 py-2 font-semibold">
                  Recorded Golden Cohort (QA · GR · CP · Lat)
                </th>
                <th scope="col" className="px-2.5 py-2 text-right font-semibold">
                  Inspect
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line bg-surface-0">
              {sorted.map((row: ArchitectureTcoRow) => {
                const badge = fitTierBadge(row.fit_tier);
                const isWinner = row.fit_tier === "recommended";
                const lvl = levelMap.get(row.level_id);
                const enabled = lvl ? lvl.enabled : true;
                const cohort = row.recorded_cohort ?? row.alternate_cohort;
                const isAltCohort = row.recorded_cohort === null && row.alternate_cohort !== null;
                const checked = compareSet.has(row.level_id);

                return (
                  <tr
                    key={row.level_id}
                    data-testid={`tco-row-${row.level_id}`}
                    className={isWinner ? "bg-winner" : "hover:bg-surface-1"}
                  >
                    <td className="px-2.5 py-2.5 align-top">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={!enabled}
                        aria-label={`Select ${row.label} for comparison`}
                        onChange={() => toggleCompare(row.level_id)}
                        className="mt-0.5 h-3.5 w-3.5 rounded border-line accent-[var(--accent-moss)]"
                      />
                    </td>

                    <td className="px-3 py-2.5 align-top">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-semibold text-ink-1">{row.short_label}</span>
                        <Pill tone={badge.tone}>{badge.label}</Pill>
                        {row.idle_mode === "serverless_zero" ? (
                          <span className="rounded border border-line bg-surface-1 px-1.5 py-0.5 font-mono text-xs text-ok">
                            $0 idle
                          </span>
                        ) : (
                          <span className="rounded border border-line bg-surface-1 px-1.5 py-0.5 font-mono text-xs text-ink-2">
                            24×7 ({row.capacity_units}u)
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 text-xs text-ink-3" title={row.standing_note}>
                        {row.label}
                        {row.level_id === "rag_engine" ? " · Serverless default (+Spanner Basic opt.)" : ""}
                      </p>
                    </td>

                    <td className="px-2.5 py-2.5 text-right align-top font-mono tabular-nums font-semibold text-ink-1">
                      {row.fit_score}
                    </td>

                    <td
                      className="px-2.5 py-2.5 text-right align-top font-mono tabular-nums text-ink-1"
                      title={row.standing_note}
                    >
                      {formatUsdMonthly(row.standing_monthly_usd)}
                    </td>

                    <td className="px-2.5 py-2.5 text-right align-top font-mono tabular-nums text-ink-2">
                      {formatUsdMonthly(row.storage_monthly_usd)}
                    </td>

                    <td className="px-3 py-2.5 text-right align-top">
                      <div className="font-mono tabular-nums text-ink-1">
                        {formatUsdMonthly(row.query_variable_monthly_usd)}
                      </div>
                      <div className="font-mono text-xs text-ink-3">
                        LLM {formatUsdMonthly(row.synthesis_monthly_usd)}
                        {row.retrieval_scan_monthly_usd > 0
                          ? ` · Ret ${formatUsdMonthly(row.retrieval_scan_monthly_usd)}`
                          : ""}
                        {row.rerank_monthly_usd > 0 ? ` · Rank ${formatUsdMonthly(row.rerank_monthly_usd)}` : ""}
                      </div>
                    </td>

                    <td className="px-3 py-2.5 text-right align-top">
                      <div className="font-mono tabular-nums font-semibold text-ink-1">
                        {formatUsdMonthly(row.total_monthly_usd)}/mo
                      </div>
                      <div className="font-mono text-xs text-ink-3">
                        ${row.cost_per_1k_queries_usd.toFixed(2)} / 1k q
                      </div>
                    </td>

                    <td
                      className="px-2.5 py-2.5 text-right align-top font-mono tabular-nums text-ink-2"
                      title={row.ingestion_note}
                    >
                      {formatUsdMonthly(row.one_time_ingestion_usd)}
                    </td>

                    <td className="px-3 py-2.5 align-top">
                      {cohort ? (
                        <div className="flex flex-col gap-0.5">
                          <span className="font-mono tabular-nums text-ink-1">
                            QA {fmtScore(cohort.avg_qa_relevance)} · GR {fmtScore(cohort.avg_groundedness)} · CP{" "}
                            {fmtScore(cohort.avg_context_precision)} · {formatLatencySeconds(cohort.avg_latency_ms)}
                          </span>
                          <span className="text-xs text-ink-3">
                            {cohort.runs} runs ·{" "}
                            {isAltCohort
                              ? `Reference cohort: ${cohort.synthesis_model} + ${cohort.judge_model}`
                              : cohort.synthesis_model}
                          </span>
                        </div>
                      ) : (
                        <span className="text-xs text-ink-3">
                          {leaderboardLoading ? "Loading recordings…" : "—"}
                        </span>
                      )}
                    </td>

                    <td className="px-2.5 py-2.5 text-right align-top">
                      {enabled ? (
                        <Link
                          to={`/levels/${row.level_id}`}
                          className="inline-flex items-center gap-1 font-medium text-moss hover:underline"
                        >
                          Open <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                        </Link>
                      ) : (
                        <span
                          className="inline-flex items-center gap-1 text-ink-3"
                          title={lvl ? lockedTitle(lvl, levels) : "Locked"}
                        >
                          <Lock className="h-3.5 w-3.5" aria-hidden />
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Transparent published GCP list-pricing disclosure */}
        <details className="rounded-md border border-line bg-surface-0 px-3 py-2 text-xs text-ink-2">
          <summary className="cursor-pointer font-semibold text-ink-1">
            Published Google Cloud pricing basis &amp; architecture notes (2026-11, pay-as-you-go list rates)
          </summary>
          <div className="mt-2 grid gap-2 border-t border-line pt-2 md:grid-cols-2">
            <ul className="flex list-disc flex-col gap-1 pl-4">
              <li>
                <span className="font-semibold text-ink-1">Vertex AI Models &amp; APIs</span>: Gemini 4 Hydrogen ($1.50
                / $7.50 per 1M in/out), Gemini 4 Carbon &amp; Argon ($2.00 / $10.00 per 1M), Claude Sonnet 4.6 ($3.00 /
                $15.00, L1–L7), Claude Opus 5.5 ($4.00 / $20.00, L1–L7), legacy Gemini 3.8 Flash ($0.75 / $3.75),
                gemini-embedding-2 ($0.20 / 1M), text-embedding-005 ($0.10 / 1M), Discovery Engine Ranking API ($1.00 /
                1k calls), Vertex AI Search Enterprise ($4.00 / 1k queries; first 10 GiB indexed free, $2.50/GiB-mo
                above 10 GiB). Level 8 ADK requires a Gemini 4 worker.
              </li>
              <li>
                <span className="font-semibold text-ink-1">Document AI &amp; Multimodal Ingest (L7/L8)</span>: Layout
                Parser ($10.00 / 1k pages) + Gemini Vision diagram transcription (~5%–25% of pages by complexity). L8
                shares the L7 multimodal BigQuery table with zero duplicate ingestion.
              </li>
            </ul>
            <ul className="flex list-disc flex-col gap-1 pl-4">
              <li>
                <span className="font-semibold text-ink-1">Serverless Idle ($0/mo) vs. Dedicated Clusters</span>: L1–L4
                and L7–L8 (with L2 default <span className="font-mono">RagManagedDb Serverless</span>, $0/query
                retrieveContexts fee; optional Spanner Basic tier adds ~${RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD.toFixed(2)}/mo
                for 100 PU) carry $0.00/mo standing compute.
              </li>
              <li>
                <span className="font-semibold text-ink-1">Dedicated ScaNN (L5 &amp; L6)</span>: L5 AlloyDB Zonal
                2-vCPU / 16 GiB primary = ${ALLOYDB_BASE_MONTHLY_USD.toFixed(2)}/mo per 2.5M queries/mo (+$0.30/GiB-mo,
                min 10 GiB; ~2× for Regional HA). L6 Vector Search 2.0{" "}
                <span className="font-mono">PERFORMANCE_OPTIMIZED</span>{" "}
                <span className="font-mono">minReplicaCount=1</span> = ${VVS2_BASE_REPLICA_MONTHLY_USD.toFixed(2)}/mo
                per 3.5M queries/mo. Benchmark scripts tear both down to $0/mo idle after recording.
              </li>
            </ul>
          </div>
        </details>
      </div>
    </Panel>
  );
};
