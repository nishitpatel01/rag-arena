/**
 * Pure leaderboard shaping (no React). Rows are grouped by (synthesis_model, judge_model) and shown
 * exactly as the server aggregated them — never averaged across pairs. Tolerates the pre-picker
 * response shape (rows without model fields), which yields `grouped: false`.
 */

import type { LeaderboardResponse, LeaderboardRow } from "../types/arena";

export interface LeaderboardGroup {
  readonly key: string;
  readonly synthesisModel: string;
  readonly judgeModel: string;
  readonly rows: readonly LeaderboardRow[];
}

export interface LeaderboardView {
  /** True only when at least one row carries both model ids (new, pair-grouped shape). */
  readonly grouped: boolean;
  readonly groups: readonly LeaderboardGroup[];
  /** Numeric `avg_*` metric keys present in any row, in first-seen order. */
  readonly metricKeys: readonly string[];
  readonly totalRows: number;
}

const MAX_ROWS = 500;
const isNonEmpty = (v: unknown): v is string => typeof v === "string" && v.trim().length > 0;

export function leaderboardRows(res: LeaderboardResponse | null | undefined): LeaderboardRow[] {
  if (!res || typeof res !== "object") return [];
  const raw = Array.isArray(res.rows) ? res.rows : Array.isArray(res.engines) ? res.engines : [];
  return raw.filter((r): r is LeaderboardRow => typeof r === "object" && r !== null && isNonEmpty(r.engine)).slice(0, MAX_ROWS);
}

export function groupLeaderboard(res: LeaderboardResponse | null | undefined): LeaderboardView {
  const rows = leaderboardRows(res);
  const grouped = rows.some((r) => isNonEmpty(r.synthesis_model) && isNonEmpty(r.judge_model));
  const metricKeys: string[] = [];
  for (const r of rows) {
    for (const [k, v] of Object.entries(r)) {
      if (k.startsWith("avg_") && typeof v === "number" && !metricKeys.includes(k)) metricKeys.push(k);
    }
  }
  if (!grouped) return { grouped: false, groups: [], metricKeys, totalRows: rows.length };
  const byKey = new Map<string, { synthesisModel: string; judgeModel: string; rows: LeaderboardRow[] }>();
  for (const r of rows) {
    const synthesisModel = isNonEmpty(r.synthesis_model) ? r.synthesis_model : "not recorded";
    const judgeModel = isNonEmpty(r.judge_model) ? r.judge_model : "not recorded";
    const key = `${synthesisModel}|${judgeModel}`;
    const g = byKey.get(key) ?? { synthesisModel, judgeModel, rows: [] };
    g.rows.push(r);
    byKey.set(key, g);
  }
  const groups = [...byKey.entries()].map(([key, g]) => ({ key, ...g }));
  return { grouped: true, groups, metricKeys, totalRows: rows.length };
}

/** Latest `recorded_at` among a row's provenance entries (ISO strings compare lexicographically). */
export function latestRecordedAt(row: LeaderboardRow): string | null {
  let latest: string | null = null;
  for (const p of row.provenance ?? []) {
    if (isNonEmpty(p.recorded_at) && (latest === null || p.recorded_at > latest)) latest = p.recorded_at;
  }
  return latest;
}
