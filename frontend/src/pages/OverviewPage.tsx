import React from "react";
import { ArrowRight, Lock } from "lucide-react";
import { Link } from "react-router-dom";
import { useAppData } from "../app/AppDataProvider";
import { lockedTitle } from "../app/Shell";
import { Panel } from "../components/ui/Panel";
import { Pill } from "../components/ui/Pill";
import { EmptyState } from "../components/ui/EmptyState";
import { STAGE_LABELS } from "../features/levels/StageRail";

const PRINCIPLES: readonly { readonly id: string; readonly text: string }[] = [
  { id: "P1", text: "Measured, not simulated — every stage event originates inside the engine with its measured duration." },
  { id: "P2", text: "Fail loud in live mode — a failed stage is shown as failed, never silently replaced." },
  { id: "P3", text: "No hardcoded scores — metrics come from the LLM judge or from recordings of real runs." },
  { id: "P4", text: "One level at a time — a level unlocks only after the previous one passes its Definition of Done." },
  { id: "P5", text: "Honest labels — a stage is named after the service that actually ran." },
];

export const OverviewPage: React.FC = () => {
  const { levels, loading, error } = useAppData();
  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(18rem,1fr)]">
      <section aria-labelledby="ladder-heading" className="flex min-w-0 flex-col gap-3">
        <div>
          <h1 id="ladder-heading" className="text-lg font-semibold text-ink-1">Level ladder</h1>
          <p className="text-sm text-ink-2">Pick a level, ask a question, and watch the real pipeline execute stage by stage.</p>
        </div>
        {loading && <p className="text-xs text-ink-3">Loading levels…</p>}
        {!loading && levels.length === 0 && (
          <EmptyState title="No levels returned" description={error ?? "GET /api/v1/levels returned an empty list."} />
        )}
        <ol className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {levels.map((level) => (
            <li key={level.level_id}>
              <Panel className="h-full" bodyClassName="flex flex-col gap-2">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-mono text-xs text-ink-3">Level {level.order}</p>
                    <h2 className="text-sm font-semibold text-ink-1">{level.label}</h2>
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
                    <Link to={`/levels/${level.level_id}`} className="inline-flex items-center gap-1 text-xs font-medium text-moss hover:underline">
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
          ))}
        </ol>
      </section>
      <Panel title="Principles" subtitle="Nothing is shown that was not measured.">
        <ol className="flex flex-col gap-2">
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
