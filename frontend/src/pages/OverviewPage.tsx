import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowRight, Lock } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useAppData } from "../app/AppDataProvider";
import { useBattleSession } from "../app/BattleSessions";
import { useMasked } from "../app/SafeMode";
import { lockedTitle } from "../app/Shell";
import { Button } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { Panel } from "../components/ui/Panel";
import { Pill } from "../components/ui/Pill";
import { levelName } from "../features/compare/compareModel";
import { STAGE_LABELS } from "../features/levels/StageRail";
import { COMPARE_SESSION } from "../features/levels/useLevelRun";
import { ArchitectureWizard } from "../features/wizard/ArchitectureWizard";
import { TcoCalculatorPanel } from "../features/wizard/TcoCalculatorPanel";
import {
  DEFAULT_WORKLOAD,
  estimateEnterpriseTco,
  uncheckedForCompareCandidates,
  type TcoWorkload,
} from "../features/wizard/tcoModel";
import { api } from "../lib/api";
import type { LeaderboardResponse } from "../types/arena";

const PRINCIPLES: readonly { readonly id: string; readonly text: string }[] = [
  { id: "P1", text: "Measured, not simulated — every stage event originates inside the engine with its measured duration." },
  { id: "P2", text: "Fail loud in live mode — a failed stage is shown as failed, never silently replaced." },
  { id: "P3", text: "No hardcoded scores — metrics come from the LLM judge or from recordings of real runs." },
  { id: "P4", text: "One level at a time — a level unlocks only after the previous one passes its Definition of Done." },
  { id: "P5", text: "Honest labels — a stage is named after the service that actually ran." },
];

const COMPLEXITY_TIER_BY_LEVEL: Readonly<Record<string, string>> = {
  managed_vertex: "Turnkey Managed",
  rag_engine: "Turnkey Managed",
  level1_naive: "Serverless SQL",
  level2_hybrid: "Serverless SQL",
  alloydb_scann: "Sub-Second ScaNN",
  vertex_vector_v2: "Sub-Second ScaNN",
  level3_multimodal: "Multimodal & Agentic",
  level4_agentic: "Multimodal & Agentic",
};

function useRecordedCohorts(): { readonly leaderboard: LeaderboardResponse | null; readonly loading: boolean } {
  const [raw, setRaw] = useState<LeaderboardResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .leaderboard({ source: "recordings" })
      .then(
        (res) => {
          if (!cancelled) setRaw(res);
        },
        () => {
          if (!cancelled) setRaw(null);
        }
      )
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { leaderboard: useMasked(raw), loading };
}

export const OverviewPage: React.FC = () => {
  const { levels, loading, error, reload } = useAppData();
  const { leaderboard, loading: leaderboardLoading } = useRecordedCohorts();
  const { session: compareSession } = useBattleSession(COMPARE_SESSION);
  const navigate = useNavigate();

  const [workload, setWorkload] = useState<TcoWorkload>(DEFAULT_WORKLOAD);
  const [showAdvanced, setShowAdvanced] = useState<boolean>(false);

  const estimate = useMemo(
    () => estimateEnterpriseTco(workload, { levels, leaderboard }),
    [workload, levels, leaderboard]
  );

  const handleCompareCandidates = useCallback(
    (candidateIds: readonly string[]) => {
      const unchecked = uncheckedForCompareCandidates(candidateIds, levels);
      compareSession.setUi("compareMode", undefined);
      compareSession.setUi("unchecked", unchecked.length > 0 ? unchecked : undefined);
      navigate("/compare?mode=ladder");
    },
    [compareSession, levels, navigate]
  );

  return (
    <div className="flex flex-col gap-5">
      <ArchitectureWizard
        workload={workload}
        onChange={setWorkload}
        estimate={estimate}
        levels={levels}
        leaderboardLoading={leaderboardLoading}
        onCompareCandidates={handleCompareCandidates}
        showAdvanced={showAdvanced}
        onToggleAdvanced={() => setShowAdvanced((v) => !v)}
        advancedContent={
          <TcoCalculatorPanel
            estimate={estimate}
            levels={levels}
            leaderboardLoading={leaderboardLoading}
            onCompareCandidates={handleCompareCandidates}
          />
        }
      />

      <section aria-labelledby="ladder-heading" className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h1 id="ladder-heading" className="text-lg font-semibold text-ink-1">
              Architecture Progression (1 → 8)
            </h1>
            <p className="text-sm text-ink-2">
              Simplest Turnkey Managed → Serverless SQL → Sub-Second ScaNN → Multimodal &amp; Agentic. Pick any level to
              watch its real pipeline execute stage by stage.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-3" aria-label="Complexity tiers">
            <span className="rounded border border-line bg-surface-0 px-2 py-0.5">1–2 Turnkey Managed</span>
            <span aria-hidden>→</span>
            <span className="rounded border border-line bg-surface-0 px-2 py-0.5">3–4 Serverless SQL</span>
            <span aria-hidden>→</span>
            <span className="rounded border border-line bg-surface-0 px-2 py-0.5">5–6 Sub-Second ScaNN</span>
            <span aria-hidden>→</span>
            <span className="rounded border border-line bg-surface-0 px-2 py-0.5">7–8 Multimodal &amp; Agentic</span>
          </div>
        </div>

        {loading && <p className="text-xs text-ink-3">Loading levels…</p>}
        {!loading && levels.length === 0 && (
          <EmptyState
            title="No levels returned"
            description={error ?? "GET /api/v1/levels returned an empty list."}
            action={
              <Button size="sm" variant="secondary" onClick={reload}>
                Retry connection
              </Button>
            }
          />
        )}

        <ol className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="architecture-ladder-grid">
          {levels.map((level) => {
            const tierSubtitle = COMPLEXITY_TIER_BY_LEVEL[level.level_id] ?? "Reference Architecture";
            return (
              <li key={level.level_id}>
                <Panel className="h-full" bodyClassName="flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="font-mono text-xs text-ink-3">
                        Level {level.order} · {tierSubtitle}
                      </p>
                      <h2 className="text-sm font-semibold text-ink-1">{levelName(level.label)}</h2>
                    </div>
                    {level.enabled ? <Pill tone="ok">enabled</Pill> : <Pill tone="locked">locked</Pill>}
                  </div>
                  <p className="text-xs text-ink-2">
                    Stages: {level.stages.map((s) => STAGE_LABELS[s] ?? s).join(" → ")}
                  </p>
                  <p className="text-xs text-ink-3">
                    {level.architecture.nodes.length} services · {level.architecture.edges.length} flows
                  </p>
                  <div className="mt-auto pt-1">
                    {level.enabled ? (
                      <Link
                        to={`/levels/${level.level_id}`}
                        className="inline-flex items-center gap-1 text-xs font-medium text-moss hover:underline"
                      >
                        Open level <ArrowRight className="h-3.5 w-3.5" aria-hidden />
                      </Link>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-ink-3">
                        <Lock className="h-3.5 w-3.5" aria-hidden /> {lockedTitle(level, levels)}
                      </span>
                    )}
                  </div>
                </Panel>
              </li>
            );
          })}
        </ol>
      </section>

      <Panel title="Engineering Principles" subtitle="Nothing is shown that was not measured.">
        <ol className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          {PRINCIPLES.map((p) => (
            <li key={p.id} className="flex gap-2 text-xs text-ink-2">
              <span className="font-mono font-semibold text-ink-1">{p.id}</span>
              <span>{p.text}</span>
            </li>
          ))}
        </ol>
      </Panel>
    </div>
  );
};
