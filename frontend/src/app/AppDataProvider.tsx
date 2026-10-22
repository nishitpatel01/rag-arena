import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { isMockRequested } from "../features/levels/useBattleStream";
import { LEVEL_IDS, QUERY_STAGE_IDS, type ArchitectureDescriptor, type HealthResponse, type LevelDescriptor } from "../types/arena";

/** Levels that have a DEV `?mock=1` battle fixture. */
const DEV_FIXTURE_LEVELS: readonly string[] = ["level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic"];

/** DEV fixture descriptors: Levels 1–4 enabled with the fixture architectures; the rest locked. */
function devFixtureLevels(arch1: ArchitectureDescriptor, arch2: ArchitectureDescriptor, arch3: ArchitectureDescriptor, arch4: ArchitectureDescriptor): LevelDescriptor[] {
  const empty: ArchitectureDescriptor = { level_id: "level1_naive", nodes: [], edges: [], ingest_nodes: [], ingest_edges: [] };
  const labels: Record<string, string> = {
    level1_naive: "Naive Vector RAG (dev fixture)",
    level2_hybrid: "BigQuery Hybrid + Reranker (dev fixture)",
    level3_multimodal: "Layout-Aware Multimodal RAG (dev fixture)",
    level4_agentic: "Agentic RAG with Google ADK (dev fixture)",
  };
  const arch: Record<string, ArchitectureDescriptor> = { level1_naive: arch1, level2_hybrid: arch2, level3_multimodal: arch3, level4_agentic: arch4 };
  return LEVEL_IDS.map((id, i) => ({
    level_id: id,
    label: labels[id] ?? id,
    order: i + 1,
    enabled: DEV_FIXTURE_LEVELS.includes(id),
    dev_fixture: DEV_FIXTURE_LEVELS.includes(id),
    stages:
      id === "level4_agentic"
        ? ["query_prep", "agent_turn", "synthesize", "evaluate"]
        : QUERY_STAGE_IDS.filter((s) => s !== "agent_turn" && (s !== "rerank" || id === "level2_hybrid" || id === "level3_multimodal")),
    architecture: arch[id] ?? { ...empty, level_id: id },
  }));
}

/** DEV `?mock=1` with a reachable backend: a disabled level that has a fixture is unlocked and flagged. */
function unlockForDevFixture(level: LevelDescriptor): LevelDescriptor {
  if (level.enabled || !DEV_FIXTURE_LEVELS.includes(level.level_id)) return level;
  return { ...level, enabled: true, dev_fixture: true, label: `${level.label} (dev fixture)` };
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
        const { LEVEL2_DEV_ARCHITECTURE } = await import("../dev/fixtures/level2_run");
        const { LEVEL3_DEV_ARCHITECTURE } = await import("../dev/fixtures/level3_run");
        const { LEVEL4_DEV_ARCHITECTURE } = await import("../dev/fixtures/level4_run");
        if (cancelled) return;
        setLevels(devFixtureLevels(LEVEL1_DEV_ARCHITECTURE, LEVEL2_DEV_ARCHITECTURE, LEVEL3_DEV_ARCHITECTURE, LEVEL4_DEV_ARCHITECTURE));
        setHealth((prev) => prev ?? { status: "dev_fixture", demo_mode: "dev_fixture", project_id: null, gcs_bucket: null });
        problems.length = 0;
      } else if (import.meta.env.DEV && isMockRequested() && l.status === "fulfilled") {
        // Backend reachable but a fixture level (e.g. Level 3) still disabled there: unlock it client-side with `dev_fixture`
        // set, so the page can play the Level 2 fixture. Ingestion stays disabled for such levels.
        setLevels([...l.value.levels].map(unlockForDevFixture).sort(BY_ORDER));
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
