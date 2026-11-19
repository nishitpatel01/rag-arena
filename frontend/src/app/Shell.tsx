import React, { useEffect, useRef, useState } from "react";
import { ChevronDown, Lock } from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";
import { useAppData } from "./AppDataProvider";
import { useOptionalIngestSession } from "./IngestSessions";
import { SafeModeToggle, useSafeMode } from "./SafeMode";
import { SHORT_LEVEL_LABEL, architectureMenuLabel, isArchitectureRoute, lockedTitle } from "./navMenuModel";
import { ingestDataLevelFor } from "../features/ingest/ingestSession";
import { GlobalModelPopover } from "../features/models/GlobalModelPopover";
import { MASK_PROJECT_ID } from "../lib/safeMode";
import { Pill } from "../components/ui/Pill";
import { ThemeToggle } from "../theme/ThemeToggle";
import { LEVEL_IDS, type LevelDescriptor } from "../types/arena";

export { SHORT_LEVEL_LABEL, lockedTitle };

const navClass = ({ isActive }: { isActive: boolean }): string =>
  `rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
    isActive ? "bg-surface-2 text-ink-1" : "text-ink-2 hover:bg-surface-2 hover:text-ink-1"
  }`;

/** Compact dropdown menu for all 1..8 architectures so the top header stays on a single row. */
const ArchitectureNavMenu: React.FC = () => {
  const { levels } = useAppData();
  const ingestCtx = useOptionalIngestSession();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const ordered =
    levels.length > 0
      ? levels
      : LEVEL_IDS.map((id, i) => ({ level_id: id, order: i + 1, enabled: false, label: id }) as LevelDescriptor);

  const activeRoute = isArchitectureRoute(location.pathname);
  const buttonLabel = architectureMenuLabel(location.pathname, ordered);
  const anyLevelStreaming = ordered.some((level) => {
    const backing = ingestDataLevelFor(level.level_id);
    return Boolean(ingestCtx?.snapshot.entries[backing]?.isStreaming);
  });

  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!open) return undefined;
    const onDocDown = (e: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="architectures-menu-trigger"
        onClick={() => setOpen((cur) => !cur)}
        className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
          activeRoute || open ? "bg-surface-2 text-ink-1" : "text-ink-2 hover:bg-surface-2 hover:text-ink-1"
        }`}
      >
        <span>{buttonLabel}</span>
        {anyLevelStreaming && (
          <span
            className="h-1.5 w-1.5 rounded-full bg-moss animate-pulse"
            title="Ingestion running for an architecture"
            aria-label="Architecture ingestion running"
            data-testid="nav-ingesting-architectures"
          />
        )}
        <ChevronDown className={`h-3.5 w-3.5 text-ink-3 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>

      {open && (
        <ol
          role="menu"
          aria-label="Level ladder"
          data-testid="architectures-menu-list"
          className="absolute left-0 top-full z-40 mt-1.5 flex w-60 flex-col gap-0.5 rounded-md border border-line bg-surface-1 p-1.5 shadow-lg"
        >
          {ordered.map((level) => {
            const href = `/levels/${level.level_id}`;
            const current = location.pathname === href;
            const backing = ingestDataLevelFor(level.level_id);
            const levelIngesting = Boolean(ingestCtx?.snapshot.entries[backing]?.isStreaming);
            if (!level.enabled) {
              return (
                <li key={level.level_id} role="none">
                  <button
                    type="button"
                    role="menuitem"
                    disabled
                    title={lockedTitle(level, ordered)}
                    aria-disabled
                    className="flex w-full cursor-not-allowed items-center justify-between rounded px-2.5 py-1.5 text-left text-xs text-ink-3"
                  >
                    <span>{SHORT_LEVEL_LABEL[level.level_id] ?? level.label}</span>
                    <Lock className="h-3 w-3 shrink-0" aria-hidden />
                  </button>
                </li>
              );
            }
            return (
              <li key={level.level_id} role="none">
                <NavLink
                  to={href}
                  role="menuitem"
                  aria-current={current ? "page" : undefined}
                  onClick={() => setOpen(false)}
                  className={`flex w-full items-center justify-between rounded px-2.5 py-1.5 text-xs font-medium transition-colors ${
                    current ? "bg-surface-2 text-ink-1" : "text-ink-2 hover:bg-surface-2 hover:text-ink-1"
                  }`}
                >
                  <span>{SHORT_LEVEL_LABEL[level.level_id] ?? level.label}</span>
                  {levelIngesting && (
                    <span
                      className="h-1.5 w-1.5 rounded-full bg-moss animate-pulse"
                      title="Ingestion running for this level"
                      aria-label="Ingestion running"
                      data-testid={`nav-ingesting-${level.level_id}`}
                    />
                  )}
                </NavLink>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
};

const HealthPills: React.FC = () => {
  const { health, error, reload } = useAppData();
  const { enabled: safe, mask } = useSafeMode();
  if (error && !health) {
    return (
      <button type="button" onClick={reload} title={`${mask(error)} — click to retry`} className="cursor-pointer">
        <Pill tone="err">backend unreachable · retry</Pill>
      </button>
    );
  }
  if (!health) return <Pill tone="neutral">connecting…</Pill>;
  const mode = String(health.demo_mode);
  return (
    <>
      {health.app_version && (
        <NavLink
          to="/releases"
          title="View release history & system documentation"
          aria-label={`Open release history (v${health.app_version})`}
          className="cursor-pointer transition-opacity hover:opacity-80"
        >
          <Pill tone="neutral" mono>
            v{health.app_version}
          </Pill>
        </NavLink>
      )}
      <Pill tone={mode === "live" || mode === "record" ? "ok" : "info"} mono title="DEMO_MODE">
        {mode}
      </Pill>
      {health.project_id && (
        <Pill tone="neutral" mono title={safe ? "GCP project (masked by safe mode)" : "GCP project (from /api/v1/health)"}>
          {safe ? MASK_PROJECT_ID : health.project_id}
          {safe && (
            <span className="ml-1 rounded-sm bg-surface-0 px-1 font-sans text-xs uppercase tracking-wide text-ink-3" data-testid="masked-marker">
              masked
            </span>
          )}
        </Pill>
      )}
    </>
  );
};

export interface ShellProps {
  readonly mockBanner: boolean;
  readonly children: React.ReactNode;
}

export const Shell: React.FC<ShellProps> = ({ mockBanner, children }) => {
  const ingestCtx = useOptionalIngestSession();
  const anyIngesting = Boolean(ingestCtx?.snapshot.anyStreaming);
  return (
    <div className="flex min-h-screen flex-col bg-surface-0 text-ink-1">
      {import.meta.env.DEV && mockBanner && (
        <div role="alert" className="border-b border-clay bg-surface-2 px-6 py-1.5 text-center text-xs font-semibold text-clay">
          DEV FIXTURE — not real data. Synthetic events are being replayed because <code className="font-mono">?mock=1</code> is set.
        </div>
      )}
      <header className="border-b border-line bg-surface-1">
        <div className="flex w-full flex-wrap items-center gap-x-3 gap-y-2 px-6 py-2.5 2xl:px-10">
          <NavLink to="/" className="shrink-0 text-sm font-semibold tracking-tight text-ink-1" aria-label="The RAG Arena home">
            The RAG Arena <span className="font-normal text-ink-3">v3</span>
          </NavLink>
          <nav aria-label="Primary" className="flex flex-wrap items-center gap-1">
            <NavLink to="/" end className={navClass}>Overview</NavLink>
            <ArchitectureNavMenu />
            <NavLink to="/workbench" className={navClass}>Workbench</NavLink>
            <NavLink to="/compare" className={navClass}>Compare</NavLink>
            <NavLink to="/leaderboard" className={navClass}>Leaderboard</NavLink>
            <NavLink to="/ingest" className={navClass}>
              <span className="inline-flex items-center gap-1.5">
                Ingest
                {anyIngesting && (
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-moss animate-pulse"
                    title="Ingestion job in progress"
                    aria-label="Ingestion running"
                    data-testid="nav-ingesting-primary"
                  />
                )}
              </span>
            </NavLink>
            <NavLink to="/releases" className={navClass}>Releases</NavLink>
          </nav>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <GlobalModelPopover />
            <HealthPills />
            <SafeModeToggle />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="w-full flex-1 px-6 py-5 2xl:px-10">{children}</main>
    </div>
  );
};
