import React from "react";
import { ArrowRight, GitCompare, Lock, RotateCcw, Sparkles, TrendingUp } from "lucide-react";
import { Link } from "react-router-dom";
import { lockedTitle } from "../../app/Shell";
import { Button } from "../../components/ui/Button";
import { Panel } from "../../components/ui/Panel";
import { Pill, type PillTone } from "../../components/ui/Pill";
import type { LevelDescriptor } from "../../types/arena";
import {
  COMPLIANCE_POSTURE_CHOICES,
  CORPUS_PAGES_BOUNDS,
  CORPUS_QUICK_PICKS,
  DATA_GRAVITY_CHOICES,
  DEFAULT_WORKLOAD,
  DOC_COMPLEXITY_CHOICES,
  ENTERPRISE_SCENARIO_PRESETS,
  LATENCY_SLA_CHOICES,
  MONTHLY_QUERIES_BOUNDS,
  QUERY_QUICK_PICKS,
  SYNTHESIS_MODEL_CHOICES,
  formatCompactInt,
  formatLatencySeconds,
  formatUsdMonthly,
  logSliderToValue,
  valueToLogSlider,
  type ArchitectureTcoRow,
  type CrossoverRegime,
  type EnterpriseTcoEstimate,
  type TcoWorkload,
  type WizardOptionMeta,
} from "./tcoModel";

export interface ArchitectureWizardProps {
  readonly workload: TcoWorkload;
  readonly onChange: (next: TcoWorkload) => void;
  readonly estimate: EnterpriseTcoEstimate;
  readonly levels: readonly LevelDescriptor[];
  readonly leaderboardLoading: boolean;
  readonly onCompareCandidates: (levelIds: readonly string[]) => void;
}

function workloadsEqual(a: TcoWorkload, b: TcoWorkload): boolean {
  return (
    a.data_gravity === b.data_gravity &&
    a.monthly_queries === b.monthly_queries &&
    a.corpus_pages === b.corpus_pages &&
    a.latency_sla === b.latency_sla &&
    a.compliance_posture === b.compliance_posture &&
    a.doc_complexity === b.doc_complexity &&
    a.synthesis_model === b.synthesis_model
  );
}

function regimeBadge(regime: CrossoverRegime): { readonly tone: PillTone; readonly text: string } {
  if (regime === "serverless_favorable") {
    return { tone: "ok", text: "$0/mo Serverless TCO Zone" };
  }
  if (regime === "crossover_zone") {
    return { tone: "warn", text: "Serverless ↔ ScaNN Crossover Zone" };
  }
  return { tone: "info", text: "Dedicated ScaNN Scale Zone" };
}

function idleBadgeText(row: ArchitectureTcoRow): string {
  if (row.idle_mode === "serverless_zero") return "$0/mo idle serverless";
  if (row.idle_mode === "dedicated_cluster") return `24×7 AlloyDB (${row.capacity_units}× 2-vCPU)`;
  return `24×7 VVS2 (minReplica=${row.capacity_units})`;
}

function fmtMetric(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(3) : "—";
}

interface OptionCardsProps<T extends string> {
  readonly label: string;
  readonly value: T;
  readonly options: readonly WizardOptionMeta<T>[];
  readonly columnsClass?: string;
  readonly onSelect: (next: T) => void;
}

function OptionCards<T extends string>({
  label,
  value,
  options,
  columnsClass = "grid-cols-1 sm:grid-cols-3",
  onSelect,
}: OptionCardsProps<T>): React.ReactElement {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-semibold text-ink-1">{label}</span>
      <div role="group" aria-label={label} className={`grid gap-1.5 ${columnsClass}`}>
        {options.map((opt) => {
          const active = opt.value === value;
          return (
            <button
              key={opt.value}
              type="button"
              aria-pressed={active}
              onClick={() => onSelect(opt.value)}
              className={`flex flex-col items-start rounded-md border px-2.5 py-2 text-left transition-colors ${
                active
                  ? "border-moss bg-winner text-ink-1"
                  : "border-line bg-surface-0 text-ink-2 hover:border-ink-3 hover:text-ink-1"
              }`}
            >
              <span className="text-xs font-semibold">{opt.label}</span>
              <span className="text-xs text-ink-3">{opt.hint}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface CohortBadgesProps {
  readonly row: ArchitectureTcoRow;
  readonly loading: boolean;
}

const CohortBadges: React.FC<CohortBadgesProps> = ({ row, loading }) => {
  const cohort = row.recorded_cohort ?? row.alternate_cohort;
  const isAlternate = row.recorded_cohort === null && row.alternate_cohort !== null;

  if (loading && !cohort) {
    return <p className="text-xs text-ink-3">Loading recorded benchmark cohorts…</p>;
  }
  if (!cohort) {
    return (
      <p className="text-xs text-ink-3">
        Recorded benchmark metrics load automatically from <span className="font-mono">GET /api/v1/leaderboard</span>.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone="ok" mono data-testid={`wizard-qa-${row.level_id}`}>
          QA {fmtMetric(cohort.avg_qa_relevance)}
        </Pill>
        <Pill tone="info" mono>
          GR {fmtMetric(cohort.avg_groundedness)}
        </Pill>
        <Pill tone="neutral" mono>
          CP {fmtMetric(cohort.avg_context_precision)}
        </Pill>
        <Pill tone="neutral" mono>
          Lat {formatLatencySeconds(cohort.avg_latency_ms)}
        </Pill>
        {cohort.avg_battle_score !== null && (
          <Pill tone="active" mono>
            Battle {cohort.avg_battle_score.toFixed(1)}
          </Pill>
        )}
        {isAlternate && (
          <Pill tone="warn" title="No Flash recording exists for this engine; showing recorded Gemini 4 Argon cohort">
            Recorded w/ {cohort.synthesis_model}
          </Pill>
        )}
      </div>
      <p className="text-xs text-ink-3">
        Measured across {cohort.runs} golden run{cohort.runs === 1 ? "" : "s"} ({cohort.synthesis_model} +{" "}
        {cohort.judge_model})
      </p>
    </div>
  );
};

export const ArchitectureWizard: React.FC<ArchitectureWizardProps> = ({
  workload,
  onChange,
  estimate,
  levels,
  leaderboardLoading,
  onCompareCandidates,
}) => {
  const byId = new Map<string, ArchitectureTcoRow>(estimate.architectures.map((r) => [r.level_id, r]));
  const primary = byId.get(estimate.recommended_level_id) ?? estimate.architectures[2];
  const runnerUp = byId.get(estimate.runner_up_level_id) ?? estimate.architectures[1];

  const levelMap = new Map<string, LevelDescriptor>(levels.map((l) => [l.level_id, l]));
  const primaryLevel = levelMap.get(primary.level_id);
  const runnerUpLevel = levelMap.get(runnerUp.level_id);
  const primaryEnabled = primaryLevel ? primaryLevel.enabled : true;
  const runnerUpEnabled = runnerUpLevel ? runnerUpLevel.enabled : true;

  const isDefault = workloadsEqual(workload, DEFAULT_WORKLOAD);
  const querySlider = valueToLogSlider(workload.monthly_queries, MONTHLY_QUERIES_BOUNDS);
  const corpusSlider = valueToLogSlider(workload.corpus_pages, CORPUS_PAGES_BOUNDS);
  const avgQps = workload.monthly_queries / (730 * 3600);
  const rawPdfMib = (workload.corpus_pages * 120) / 1024;
  const runnerUpDeltaUsd = runnerUp.total_monthly_usd - primary.total_monthly_usd;
  const crossoverPill = regimeBadge(estimate.crossover.regime);

  return (
    <Panel
      title="Enterprise Use-Case Architecture Advisor"
      subtitle="Map your Google Cloud data gravity, document complexity, latency SLA, and monthly scale to a measured reference architecture."
      actions={
        <Button
          size="sm"
          variant="ghost"
          disabled={isDefault}
          leadingIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
          onClick={() => onChange(DEFAULT_WORKLOAD)}
          data-testid="wizard-reset"
        >
          Reset defaults
        </Button>
      }
    >
      <div className="flex flex-col gap-4" data-testid="architecture-wizard">
        {/* 1-Click Enterprise Scenario Quick-Picks */}
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-2">
              <Sparkles className="h-3.5 w-3.5 text-ochre" aria-hidden />
              Executive scenario presets
            </span>
            <span className="text-xs text-ink-3">1-click workload archetypes for live architecture reviews</span>
          </div>
          <div
            role="group"
            aria-label="Enterprise scenario presets"
            className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5"
          >
            {ENTERPRISE_SCENARIO_PRESETS.map((preset) => {
              const selected = workloadsEqual(workload, preset.workload);
              return (
                <button
                  key={preset.preset_id}
                  type="button"
                  aria-pressed={selected}
                  data-testid={`wizard-preset-${preset.preset_id}`}
                  onClick={() => onChange(preset.workload)}
                  className={`flex flex-col items-start gap-0.5 rounded-md border px-2.5 py-2 text-left transition-colors ${
                    selected
                      ? "border-moss bg-winner text-ink-1"
                      : "border-line bg-surface-0 text-ink-2 hover:border-ink-3 hover:text-ink-1"
                  }`}
                >
                  <span className="text-xs font-semibold">{preset.title}</span>
                  <span className="line-clamp-2 text-xs text-ink-3">{preset.subtitle}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Main 2-column advisor workspace */}
        <div className="grid gap-4 xl:grid-cols-[minmax(0,11fr)_minmax(0,13fr)]">
          {/* Left column: 7 deterministic workload controls */}
          <div className="flex flex-col gap-3 rounded-lg border border-line bg-surface-0 p-3.5">
            <OptionCards
              label="1. Data Gravity & Store Affinity"
              value={workload.data_gravity}
              options={DATA_GRAVITY_CHOICES}
              columnsClass="grid-cols-1 sm:grid-cols-2"
              onSelect={(data_gravity) => onChange({ ...workload, data_gravity })}
            />

            <OptionCards
              label="2. Document Structure & Reasoning Complexity"
              value={workload.doc_complexity}
              options={DOC_COMPLEXITY_CHOICES}
              onSelect={(doc_complexity) => onChange({ ...workload, doc_complexity })}
            />

            <OptionCards
              label="3. End-to-End Latency SLA"
              value={workload.latency_sla}
              options={LATENCY_SLA_CHOICES}
              onSelect={(latency_sla) => onChange({ ...workload, latency_sla })}
            />

            <OptionCards
              label="4. Security, Compliance & Ops Posture"
              value={workload.compliance_posture}
              options={COMPLIANCE_POSTURE_CHOICES}
              onSelect={(compliance_posture) => onChange({ ...workload, compliance_posture })}
            />

            {/* Scale sliders: Monthly Queries & Corpus Pages */}
            <div className="grid gap-3 pt-1 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5 rounded-md border border-line bg-surface-1 p-2.5">
                <div className="flex items-baseline justify-between gap-2">
                  <label htmlFor="wizard-monthly-queries" className="text-xs font-semibold text-ink-1">
                    5. Monthly Queries
                  </label>
                  <span className="font-mono text-xs font-semibold text-moss" data-testid="wizard-queries-readout">
                    {workload.monthly_queries.toLocaleString("en-US")}/mo
                  </span>
                </div>
                <input
                  id="wizard-monthly-queries"
                  type="range"
                  min={0}
                  max={1000}
                  step={5}
                  value={querySlider}
                  aria-label="Monthly query volume"
                  onChange={(e) =>
                    onChange({
                      ...workload,
                      monthly_queries: logSliderToValue(Number(e.target.value), MONTHLY_QUERIES_BOUNDS),
                    })
                  }
                  className="w-full accent-[var(--accent-moss)]"
                />
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <div className="flex flex-wrap gap-1">
                    {QUERY_QUICK_PICKS.map((q) => (
                      <button
                        key={q}
                        type="button"
                        onClick={() => onChange({ ...workload, monthly_queries: q })}
                        className={`rounded border px-1.5 py-0.5 font-mono text-xs ${
                          workload.monthly_queries === q
                            ? "border-moss bg-winner text-ink-1 font-semibold"
                            : "border-line bg-surface-0 text-ink-3 hover:text-ink-1"
                        }`}
                      >
                        {formatCompactInt(q)}
                      </button>
                    ))}
                  </div>
                  <span className="font-mono text-xs text-ink-3">~{avgQps < 0.1 ? avgQps.toFixed(3) : avgQps.toFixed(2)} QPS</span>
                </div>
              </div>

              <div className="flex flex-col gap-1.5 rounded-md border border-line bg-surface-1 p-2.5">
                <div className="flex items-baseline justify-between gap-2">
                  <label htmlFor="wizard-corpus-pages" className="text-xs font-semibold text-ink-1">
                    6. Corpus Scale (PDF Pages)
                  </label>
                  <span className="font-mono text-xs font-semibold text-moss" data-testid="wizard-pages-readout">
                    {workload.corpus_pages.toLocaleString("en-US")} pages
                  </span>
                </div>
                <input
                  id="wizard-corpus-pages"
                  type="range"
                  min={0}
                  max={1000}
                  step={5}
                  value={corpusSlider}
                  aria-label="Corpus size in PDF pages"
                  onChange={(e) =>
                    onChange({
                      ...workload,
                      corpus_pages: logSliderToValue(Number(e.target.value), CORPUS_PAGES_BOUNDS),
                    })
                  }
                  className="w-full accent-[var(--accent-moss)]"
                />
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <div className="flex flex-wrap gap-1">
                    {CORPUS_QUICK_PICKS.map((p) => (
                      <button
                        key={p}
                        type="button"
                        onClick={() => onChange({ ...workload, corpus_pages: p })}
                        className={`rounded border px-1.5 py-0.5 font-mono text-xs ${
                          workload.corpus_pages === p
                            ? "border-moss bg-winner text-ink-1 font-semibold"
                            : "border-line bg-surface-0 text-ink-3 hover:text-ink-1"
                        }`}
                      >
                        {formatCompactInt(p)}
                      </button>
                    ))}
                  </div>
                  <span className="font-mono text-xs text-ink-3">
                    ~{rawPdfMib >= 1024 ? `${(rawPdfMib / 1024).toFixed(1)} GiB` : `${Math.round(rawPdfMib)} MiB`}
                  </span>
                </div>
              </div>
            </div>

            <OptionCards
              label="7. Answer Synthesis Model (Vertex AI)"
              value={workload.synthesis_model}
              options={SYNTHESIS_MODEL_CHOICES}
              columnsClass="grid-cols-1 sm:grid-cols-2"
              onSelect={(synthesis_model) => onChange({ ...workload, synthesis_model })}
            />
          </div>

          {/* Right column: Primary + Runner-up recommendations + Crossover insight */}
          <div className="flex flex-col gap-3">
            {/* Primary Recommended Architecture */}
            <div
              className="flex flex-col gap-3 rounded-lg border-2 border-moss bg-winner p-4"
              data-testid="wizard-primary-card"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Pill tone="ok">Primary recommendation · Fit {primary.fit_score}/100</Pill>
                    <Pill tone="neutral" mono>
                      {idleBadgeText(primary)}
                    </Pill>
                  </div>
                  <h3 className="text-base font-semibold text-ink-1" data-testid="wizard-primary-title">
                    {/^level\s*\d/i.test(primary.label) ? primary.label : `Level ${primary.order} · ${primary.label}`}
                  </h3>
                </div>
                <div className="text-right">
                  <p className="font-mono text-base font-semibold text-ink-1" data-testid="wizard-primary-tco">
                    {formatUsdMonthly(primary.total_monthly_usd)}
                    <span className="text-xs font-normal text-ink-2">/mo</span>
                  </p>
                  <p className="font-mono text-xs text-ink-3">
                    ${primary.cost_per_1k_queries_usd.toFixed(2)} / 1k queries · {formatUsdMonthly(primary.one_time_ingestion_usd)} ingest
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap gap-1.5">
                {primary.gcp_services.map((svc) => (
                  <span
                    key={svc}
                    className="rounded border border-line bg-surface-0 px-2 py-0.5 font-mono text-xs text-ink-2"
                  >
                    {svc}
                  </span>
                ))}
              </div>

              <CohortBadges row={primary} loading={leaderboardLoading} />

              <div className={`grid gap-2 border-t border-line pt-2.5 ${primary.tradeoffs.length > 0 ? "sm:grid-cols-2" : "grid-cols-1"}`}>
                <div>
                  <p className="text-xs font-semibold text-ink-1">Why this GCP architecture fits</p>
                  <ul className="mt-1 flex list-disc flex-col gap-1 pl-4 text-xs text-ink-2">
                    {primary.rationale.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                </div>
                {primary.tradeoffs.length > 0 && (
                  <div>
                    <p className="text-xs font-semibold text-ink-1">Architectural watch-outs</p>
                    <ul className="mt-1 flex list-disc flex-col gap-1 pl-4 text-xs text-ink-2">
                      {primary.tradeoffs.map((t) => (
                        <li key={t}>{t}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2.5">
                {primaryEnabled ? (
                  <Link
                    to={`/levels/${primary.level_id}`}
                    data-testid="wizard-open-primary"
                    className="inline-flex items-center gap-1.5 rounded-md border border-moss bg-moss px-3 py-1.5 text-xs font-medium text-surface-0 transition-opacity hover:opacity-90"
                  >
                    Open Level {primary.order} live stage view <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                  </Link>
                ) : (
                  <span className="inline-flex items-center gap-1 text-xs text-ink-3">
                    <Lock className="h-3.5 w-3.5" aria-hidden />
                    {primaryLevel ? lockedTitle(primaryLevel, levels) : "Locked"}
                  </span>
                )}
                <Button
                  size="sm"
                  variant="secondary"
                  leadingIcon={<GitCompare className="h-3.5 w-3.5" aria-hidden />}
                  onClick={() => onCompareCandidates([primary.level_id, runnerUp.level_id])}
                  data-testid="wizard-compare-top"
                >
                  Compare L{primary.order} vs L{runnerUp.order} on /compare
                </Button>
                <Link
                  to="/workbench"
                  className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-moss hover:underline"
                >
                  Customize slots in Workbench <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                </Link>
              </div>
            </div>

            {/* Runner-Up Architecture */}
            <div
              className="flex flex-col gap-2 rounded-lg border border-line bg-surface-0 p-3.5"
              data-testid="wizard-runner-up-card"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex flex-col gap-0.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Pill tone="info">Runner-up · Fit {runnerUp.fit_score}/100</Pill>
                    <Pill tone="neutral" mono>
                      {idleBadgeText(runnerUp)}
                    </Pill>
                  </div>
                  <h4 className="text-sm font-semibold text-ink-1">
                    {/^level\s*\d/i.test(runnerUp.label) ? runnerUp.label : `Level ${runnerUp.order} · ${runnerUp.label}`}
                  </h4>
                </div>
                <div className="text-right">
                  <p className="font-mono text-sm font-semibold text-ink-1">
                    {formatUsdMonthly(runnerUp.total_monthly_usd)}/mo
                  </p>
                  <p className="font-mono text-xs text-ink-3">
                    {runnerUpDeltaUsd >= 0 ? "+" : "-"}
                    {formatUsdMonthly(Math.abs(runnerUpDeltaUsd))} vs primary
                  </p>
                </div>
              </div>

              <CohortBadges row={runnerUp} loading={leaderboardLoading} />

              {runnerUp.rationale[0] && <p className="text-xs text-ink-2">{runnerUp.rationale[0]}</p>}

              <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
                <span className="text-xs text-ink-3">{runnerUp.tradeoffs[0] ?? ""}</span>
                {runnerUpEnabled ? (
                  <Link
                    to={`/levels/${runnerUp.level_id}`}
                    className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-moss hover:underline"
                  >
                    Open Level {runnerUp.order} <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                  </Link>
                ) : (
                  <span className="inline-flex items-center gap-1 text-xs text-ink-3">
                    <Lock className="h-3.5 w-3.5" aria-hidden />
                    {runnerUpLevel ? lockedTitle(runnerUpLevel, levels) : "Locked"}
                  </span>
                )}
              </div>
            </div>

            {/* Serverless $0/mo Idle vs Dedicated Cluster Crossover Banner */}
            <div
              className="flex flex-col gap-1.5 rounded-lg border border-line bg-surface-0 p-3.5"
              data-testid="wizard-crossover-banner"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-1">
                  <TrendingUp className="h-3.5 w-3.5 text-moss" aria-hidden />
                  $0/mo Serverless Idle vs. Dedicated ScaNN Crossover
                </span>
                <Pill tone={crossoverPill.tone}>{crossoverPill.text}</Pill>
              </div>
              <p className="text-xs text-ink-2">{estimate.crossover.summary}</p>
              <div className="flex flex-wrap gap-x-4 gap-y-1 pt-0.5 font-mono text-xs text-ink-3">
                <span>
                  L8 VVS2 &lt; L5 VAIS: ~{estimate.crossover.vvs2_vs_vais_queries.toLocaleString("en-US")} q/mo
                </span>
                <span>
                  L7 AlloyDB &lt; L5 VAIS: ~{estimate.crossover.alloydb_vs_vais_queries.toLocaleString("en-US")} q/mo
                </span>
                <span>
                  L8 VVS2 &lt; L3 BQ Scan ({formatCompactInt(workload.corpus_pages)} p): ~
                  {estimate.crossover.vvs2_vs_bq_multimodal_queries.toLocaleString("en-US")} q/mo
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Panel>
  );
};
