/**
 * Thin typed REST client. Every call carries an abort timeout and fails loud on non-2xx.
 */

import type {
  BenchmarksResponse,
  HealthResponse,
  IngestJobCreated,
  IngestStatusResponse,
  LevelsResponse,
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

async function requestJson<T>(path: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const url = `${API_BASE}${path}`;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const text = await response.text();
    if (!response.ok) {
      throw new ApiError(url, response.status, text.slice(0, 300) || response.statusText);
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

export const api = {
  health: (): Promise<HealthResponse> => requestJson<HealthResponse>("/health"),
  levels: (): Promise<LevelsResponse> => requestJson<LevelsResponse>("/levels"),
  benchmarks: (): Promise<BenchmarksResponse> => requestJson<BenchmarksResponse>("/benchmarks"),
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
