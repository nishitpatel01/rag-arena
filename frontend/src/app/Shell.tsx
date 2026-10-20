import React from "react";
import { Lock } from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";
import { useAppData } from "./AppDataProvider";
import { Pill } from "../components/ui/Pill";
import { ThemeToggle } from "../theme/ThemeToggle";
import { LEVEL_IDS, type LevelDescriptor } from "../types/arena";

export const SHORT_LEVEL_LABEL: Record<string, string> = {
  level1_naive: "L1 Naive",
  level2_hybrid: "L2 Hybrid",
  level3_multimodal: "L3 Multimodal",
  level4_agentic: "L4 Agentic",
  managed_vertex: "Managed Vertex",
};

export function lockedTitle(level: LevelDescriptor, all: readonly LevelDescriptor[]): string {
  const prev = all.find((l) => l.order === level.order - 1);
  return prev ? `Locked — pending Level ${prev.order} sign-off` : "Locked";
}

const navClass = ({ isActive }: { isActive: boolean }): string =>
  `rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
    isActive ? "bg-surface-2 text-ink-1" : "text-ink-2 hover:bg-surface-2 hover:text-ink-1"
  }`;

/** Level ladder: enabled levels link to their page; locked levels are disabled buttons with the reason. */
const LevelLadderNav: React.FC = () => {
  const { levels } = useAppData();
  const location = useLocation();
  const ordered = levels.length > 0 ? levels : LEVEL_IDS.map((id, i) => ({ level_id: id, order: i + 1, enabled: false, label: id }) as LevelDescriptor);
  return (
    <ol className="flex items-center gap-1" aria-label="Level ladder">
      {ordered.map((level) => {
        const href = `/levels/${level.level_id}`;
        const current = location.pathname === href;
        if (!level.enabled) {
          return (
            <li key={level.level_id}>
              <button type="button" disabled title={lockedTitle(level, ordered)} aria-disabled className="inline-flex cursor-not-allowed items-center gap-1 rounded-md px-2.5 py-1 text-xs text-ink-3">
                <Lock className="h-3 w-3" aria-hidden />
                {SHORT_LEVEL_LABEL[level.level_id] ?? level.label}
              </button>
            </li>
          );
        }
        return (
          <li key={level.level_id}>
            <NavLink to={href} className={navClass} aria-current={current ? "page" : undefined}>
              {SHORT_LEVEL_LABEL[level.level_id] ?? level.label}
            </NavLink>
          </li>
        );
      })}
    </ol>
  );
};

const HealthPills: React.FC = () => {
  const { health, error } = useAppData();
  if (error && !health) return <Pill tone="err" title={error}>backend unreachable</Pill>;
  if (!health) return <Pill tone="neutral">connecting…</Pill>;
  const mode = String(health.demo_mode);
  return (
    <>
      <Pill tone={mode === "live" || mode === "record" ? "ok" : "info"} mono title="DEMO_MODE">
        {mode}
      </Pill>
      {health.project_id && (
        <Pill tone="neutral" mono title="GCP project (from /api/v1/health)">
          {health.project_id}
        </Pill>
      )}
    </>
  );
};

export interface ShellProps {
  readonly mockBanner: boolean;
  readonly children: React.ReactNode;
}

export const Shell: React.FC<ShellProps> = ({ mockBanner, children }) => (
  <div className="flex min-h-screen flex-col bg-surface-0 text-ink-1">
    {import.meta.env.DEV && mockBanner && (
      <div role="alert" className="border-b border-clay bg-surface-2 px-6 py-1.5 text-center text-xs font-semibold text-clay">
        DEV FIXTURE — not real data. Synthetic events are being replayed because <code className="font-mono">?mock=1</code> is set.
      </div>
    )}
    <header className="border-b border-line bg-surface-1">
      <div className="flex w-full flex-wrap items-center gap-x-4 gap-y-2 px-6 py-2.5 2xl:px-10">
        <NavLink to="/" className="text-sm font-semibold tracking-tight text-ink-1" aria-label="The RAG Arena home">
          The RAG Arena <span className="font-normal text-ink-3">v3</span>
        </NavLink>
        <nav aria-label="Primary" className="flex items-center gap-1">
          <NavLink to="/" end className={navClass}>Overview</NavLink>
          <NavLink to="/ingest" className={navClass}>Ingest</NavLink>
          <NavLink to="/compare" className={navClass}>Compare</NavLink>
          <NavLink to="/leaderboard" className={navClass}>Leaderboard</NavLink>
        </nav>
        <div className="hidden h-5 w-px bg-line md:block" aria-hidden />
        <LevelLadderNav />
        <div className="ml-auto flex items-center gap-2">
          <HealthPills />
          <ThemeToggle />
        </div>
      </div>
    </header>
    <main className="w-full flex-1 px-6 py-5 2xl:px-10">{children}</main>
  </div>
);
