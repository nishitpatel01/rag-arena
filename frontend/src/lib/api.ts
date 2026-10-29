/**
 * Thin typed REST client. Every call carries an abort timeout and fails loud on non-2xx.
 */

import type {
  BenchmarksResponse,
  HealthResponse,
  IngestJobCreated,
  IngestStatusResponse,
  LeaderboardResponse,
  LeaderboardScope,
  LeaderboardSource,
  LevelsResponse,
  ModelsResponse,
} from "../types/arena";

export const API_BASE = "/api/v1";
const DEFAULT_TIMEOUT_MS = 10_000;

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  constructor(url: string, status: number, message: string) {
    super(`${message} (${status} ${url})`);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
  }
}

/**
 * The backend's verbatim error text: `{"error": "..."}` bodies yield the `error` string unchanged;
 * anything else yields the (bounded) raw body. Never rewrites or paraphrases the message.
 */
export function backendErrorText(body: string, fallback: string): string {
  const trimmed = body.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (typeof parsed === "object" && parsed !== null && typeof (parsed as { error?: unknown }).error === "string") {
        return (parsed as { error: string }).error;
      }
    } catch {
      // Not JSON after all: fall through to the raw body.
    }
  }
  return trimmed.slice(0, 300) || fallback;
}

async function requestJson<T>(path: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const url = `${API_BASE}${path}`;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const text = await response.text();
    if (!response.ok) {
      throw new ApiError(url, response.status, backendErrorText(text, response.statusText));
    }
    return JSON.parse(text) as T;
  } catch (err) {
    if (controller.signal.aborted) {
      throw new ApiError(url, 0, `timed out after ${Math.round(timeoutMs / 1000)} s`);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

/** BigQuery-backed status (COUNT + INFORMATION_SCHEMA) can take >10 s on a cold cache. */
const INGEST_STATUS_TIMEOUT_MS = 30_000;

/** Model catalog (availability + prices are server-provided; never hardcoded client-side). */
const MODELS_TIMEOUT_MS = 15_000;

export function fetchModels(): Promise<ModelsResponse> {
  return requestJson<ModelsResponse>("/models", {}, MODELS_TIMEOUT_MS);
}

/** The live source reads BigQuery `query_runs` (server bound 45 s); allow it to answer or fail first. */
const LEADERBOARD_TIMEOUT_MS = 60_000;

export interface LeaderboardQuery {
  readonly source?: LeaderboardSource;
  readonly scope?: LeaderboardScope;
  readonly questionId?: string;
  readonly engine?: string;
}

/** `?source=&scope=&question_id=&engine=` with empty values omitted (server applies its mode default). */
export function leaderboardSearch(query: LeaderboardQuery): string {
  const params = new URLSearchParams();
  if (query.source) params.set("source", query.source);
  if (query.scope) params.set("scope", query.scope);
  if (query.questionId) params.set("question_id", query.questionId);
  if (query.engine) params.set("engine", query.engine);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: (): Promise<HealthResponse> => requestJson<HealthResponse>("/health"),
  levels: (): Promise<LevelsResponse> => requestJson<LevelsResponse>("/levels"),
  benchmarks: (): Promise<BenchmarksResponse> => requestJson<BenchmarksResponse>("/benchmarks"),
  models: fetchModels,
  leaderboard: (query: LeaderboardQuery = {}): Promise<LeaderboardResponse> =>
    requestJson<LeaderboardResponse>(`/leaderboard${leaderboardSearch(query)}`, {}, LEADERBOARD_TIMEOUT_MS),
  ingestStatus: (): Promise<IngestStatusResponse> => requestJson<IngestStatusResponse>("/ingest/status", {}, INGEST_STATUS_TIMEOUT_MS),
  createIngestJob: (level: string): Promise<IngestJobCreated> =>
    requestJson<IngestJobCreated>("/ingest/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level }),
    }),
  ingestJobStreamUrl: (jobId: string): string => `${API_BASE}/ingest/jobs/${encodeURIComponent(jobId)}/stream`,
  battleStreamUrl: (): string => `${API_BASE}/battle/stream`,
};
