import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { isMockRequested } from "../features/levels/useBattleStream";
import { LEVEL_IDS, QUERY_STAGE_IDS, type ArchitectureDescriptor, type HealthResponse, type LevelDescriptor } from "../types/arena";

/** DEV fixture descriptors: Level 1 enabled with the fixture architecture; the rest locked. */
function devFixtureLevels(arch: ArchitectureDescriptor): LevelDescriptor[] {
  const empty: ArchitectureDescriptor = { level_id: "level1_naive", nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] };
  return LEVEL_IDS.map((id, i) => ({
    level_id: id,
    label: id === "level1_naive" ? "Naive Vector RAG (dev fixture)" : id,
    order: i + 1,
    enabled: id === "level1_naive",
    stages: QUERY_STAGE_IDS.filter((s) => s !== "rerank" && s !== "agent_turn"),
    architecture: id === "level1_naive" ? arch : { ...empty, level_id: id },
  }));
}

export interface AppData {
  readonly health: HealthResponse | null;
  readonly levels: readonly LevelDescriptor[];
  readonly loading: boolean;
  readonly error: string | null;
  readonly reload: () => void;
}

const AppDataContext = createContext<AppData | null>(null);

const BY_ORDER = (a: LevelDescriptor, b: LevelDescriptor): number => a.order - b.order;

/** Loads `/health` and `/levels` once; everything else (benchmarks, ingest status) is per page. */
export const AppDataProvider: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [levels, setLevels] = useState<readonly LevelDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void Promise.allSettled([api.health(), api.levels()]).then(async ([h, l]) => {
      if (cancelled) return;
      const problems: string[] = [];
      if (h.status === "fulfilled") setHealth(h.value);
      else problems.push(`health: ${h.reason instanceof Error ? h.reason.message : String(h.reason)}`);
      if (l.status === "fulfilled") setLevels([...l.value.levels].sort(BY_ORDER));
      else problems.push(`levels: ${l.reason instanceof Error ? l.reason.message : String(l.reason)}`);

      // DEV + ?mock=1 only: when the backend is unreachable, use the fixture descriptor so the
      // level page can be exercised. Eliminated from production bundles (import.meta.env.DEV is false).
      if (import.meta.env.DEV && isMockRequested() && l.status === "rejected") {
        const { LEVEL1_DEV_ARCHITECTURE } = await import("../dev/fixtures/level1_run");
        if (cancelled) return;
        setLevels(devFixtureLevels(LEVEL1_DEV_ARCHITECTURE));
        setHealth((prev) => prev ?? { status: "dev_fixture", demo_mode: "dev_fixture", project_id: null, gcs_bucket: null });
        problems.length = 0;
      }
      setError(problems.length ? problems.join(" · ") : null);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const value = useMemo<AppData>(() => ({ health, levels, loading, error, reload }), [health, levels, loading, error, reload]);
  return <AppDataContext.Provider value={value}>{children}</AppDataContext.Provider>;
};

export function useAppData(): AppData {
  const ctx = useContext(AppDataContext);
  if (!ctx) throw new Error("useAppData must be used inside <AppDataProvider>");
  return ctx;
}
