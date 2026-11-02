/**
 * Bounded retry and single-flight helpers for app bootstrap and cold-start status calls.
 */

import type { HealthResponse, LevelsResponse } from "../types/arena";

const MAX_BOOTSTRAP_ATTEMPTS = 5;
const MAX_BASE_DELAY_MS = 30_000;

export interface BootstrapOptions {
  readonly fetchHealth: () => Promise<HealthResponse>;
  readonly fetchLevels: () => Promise<LevelsResponse>;
  readonly maxAttempts?: number;
  readonly baseDelayMs?: number;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly isCancelled?: () => boolean;
}

export interface BootstrapResult {
  readonly health: HealthResponse | null;
  readonly levels: LevelsResponse | null;
  readonly errors: readonly string[];
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/**
 * Loads `/health` and `/levels` with bounded exponential backoff, retrying only failed endpoints.
 */
export async function loadBootstrapData(opts: BootstrapOptions): Promise<BootstrapResult> {
  const maxAttempts = opts.maxAttempts ?? 3;
  const baseDelayMs = opts.baseDelayMs ?? 1_500;
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > MAX_BOOTSTRAP_ATTEMPTS) {
    throw new RangeError(`maxAttempts must be an integer in [1, ${MAX_BOOTSTRAP_ATTEMPTS}]`);
  }
  if (!Number.isFinite(baseDelayMs) || baseDelayMs < 0 || baseDelayMs > MAX_BASE_DELAY_MS) {
    throw new RangeError(`baseDelayMs must be finite in [0, ${MAX_BASE_DELAY_MS}]`);
  }
  const sleep = opts.sleep ?? defaultSleep;
  const isCancelled = opts.isCancelled ?? (() => false);

  let health: HealthResponse | null = null;
  let levels: LevelsResponse | null = null;
  let healthErr: string | null = null;
  let levelsErr: string | null = null;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (isCancelled()) break;
    const healthTask: Promise<HealthResponse> = health !== null ? Promise.resolve(health) : opts.fetchHealth();
    const levelsTask: Promise<LevelsResponse> = levels !== null ? Promise.resolve(levels) : opts.fetchLevels();
    const [hRes, lRes] = await Promise.allSettled([healthTask, levelsTask]);
    if (hRes.status === "fulfilled") {
      health = hRes.value;
      healthErr = null;
    } else {
      healthErr = `health: ${errorMessage(hRes.reason)}`;
    }
    if (lRes.status === "fulfilled") {
      levels = lRes.value;
      levelsErr = null;
    } else {
      levelsErr = `levels: ${errorMessage(lRes.reason)}`;
    }
    if ((health !== null && levels !== null) || attempt + 1 >= maxAttempts || isCancelled()) {
      break;
    }
    await sleep(baseDelayMs * 2 ** attempt);
  }

  const errors = [healthErr, levelsErr].filter((e): e is string => e !== null);
  return { health, levels, errors };
}

/**
 * Coalesces concurrent calls to a zero-arg async function into one in-flight Promise; resets on settlement.
 */
export function createSingleFlight<T>(fn: () => Promise<T>): () => Promise<T> {
  if (typeof fn !== "function") {
    throw new TypeError("fn must be a function");
  }
  if (fn.length > 0) {
    throw new RangeError("createSingleFlight expects a zero-argument function");
  }
  let inFlight: Promise<T> | null = null;
  return (): Promise<T> => {
    if (inFlight !== null) return inFlight;
    let raw: Promise<T>;
    try {
      raw = fn();
    } catch (err) {
      return Promise.reject(err);
    }
    const current = raw.finally(() => {
      if (inFlight === current) inFlight = null;
    });
    inFlight = current;
    return current;
  };
}
