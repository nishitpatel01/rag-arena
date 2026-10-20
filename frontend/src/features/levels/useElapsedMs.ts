import { useEffect, useState } from "react";

export const ELAPSED_TICK_MS = 250;

/**
 * Elapsed ms since `startedAt`, re-rendered every 250 ms ONLY while `active` is true.
 * Returns null when not active. This is the single permitted client timer for progress display;
 * once a stage completes the backend `duration_ms` replaces it (callers never show this value then).
 */
export function useElapsedMs(startedAt: number | null, active: boolean): number | null {
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    if (!active || startedAt === null) return undefined;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), ELAPSED_TICK_MS);
    return () => window.clearInterval(id);
  }, [active, startedAt]);

  if (!active || startedAt === null) return null;
  return Math.max(0, now - startedAt);
}
