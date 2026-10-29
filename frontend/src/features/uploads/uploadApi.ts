/**
 * Typed REST + resumable-PUT client for `/api/v1/uploads/*`.
 *
 * Preserves structured error payloads returned by the backend:
 * - `POST /uploads/bucket` returns a full `BucketStatusResponse` JSON even on HTTP 403/409/502/503.
 * - `POST /uploads/{ws}/files/{fid}/finalize` returns `{ error, file }` on HTTP 422 when PDF validation rejects a file.
 * - `POST /uploads/{ws}/ingest` returns `{ error, needs_confirmation: true, estimate }` on HTTP 409 for Level 3.
 */

import { API_BASE, ApiError, backendErrorText } from "../../lib/api";
import type { IngestJobCreated } from "../../types/arena";
import {
  uploadViaResumableSession,
  type BucketStatusResponse,
  type ResumablePutResult,
  type UploadCostEstimate,
  type UploadFileEntry,
  type UploadWorkspace,
} from "./uploadModel";

const BUCKET_TIMEOUT_MS = 35_000;
const WORKSPACE_TIMEOUT_MS = 30_000;
const FINALIZE_TIMEOUT_MS = 120_000;
const DELETE_TIMEOUT_MS = 90_000;
const PUT_CHUNK_TIMEOUT_MS = 120_000;

export interface CreateWorkspaceResponse {
  readonly workspace: UploadWorkspace;
  readonly bucket: BucketStatusResponse;
}

export interface CreateFileSessionResponse {
  readonly file: UploadFileEntry;
  readonly object: string;
  readonly session_uri: string;
}

export interface FinalizeFileOutcome {
  readonly accepted: boolean;
  readonly file: UploadFileEntry;
  readonly error: string | null;
}

export interface StartUploadIngestOutcome {
  readonly started: boolean;
  readonly job: IngestJobCreated | null;
  readonly needsConfirmation: boolean;
  readonly estimate: UploadCostEstimate | null;
  readonly error: string | null;
}

export interface DeleteWorkspaceResponse {
  readonly deleted: string;
  readonly deleted_objects: number;
}

async function rawRequest(
  path: string,
  init: RequestInit = {},
  timeoutMs = WORKSPACE_TIMEOUT_MS
): Promise<{ readonly status: number; readonly ok: boolean; readonly text: string; readonly url: string }> {
  const url = `${API_BASE}${path}`;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal });
    const text = await res.text();
    return { status: res.status, ok: res.ok, text, url };
  } catch (err) {
    if (controller.signal.aborted) {
      throw new ApiError(url, 0, `timed out after ${Math.round(timeoutMs / 1000)} s`);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

function parseJsonOrThrow<T>(raw: { status: number; ok: boolean; text: string; url: string }): T {
  if (!raw.ok) {
    throw new ApiError(raw.url, raw.status, backendErrorText(raw.text, `HTTP ${raw.status}`));
  }
  return JSON.parse(raw.text) as T;
}

function tryParseObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

async function sendChunkPut(
  uri: string,
  headers: Readonly<Record<string, string>>,
  body: Blob | null,
  outerSignal?: AbortSignal
): Promise<ResumablePutResult> {
  const controller = new AbortController();
  const onOuterAbort = () => controller.abort();
  outerSignal?.addEventListener("abort", onOuterAbort, { once: true });
  const timer = window.setTimeout(() => controller.abort(), PUT_CHUNK_TIMEOUT_MS);
  try {
    const reqHeaders: Record<string, string> = { ...headers };
    if (body !== null) reqHeaders["Content-Type"] = "application/pdf";
    const res = await fetch(uri, {
      method: "PUT",
      headers: reqHeaders,
      body,
      signal: controller.signal,
    });
    const bodyText = res.status >= 400 ? await res.text() : undefined;
    return { status: res.status, rangeHeader: res.headers.get("Range"), bodyText };
  } finally {
    window.clearTimeout(timer);
    outerSignal?.removeEventListener("abort", onOuterAbort);
  }
}

export const uploadApi = {
  bucketStatus: async (): Promise<BucketStatusResponse> => {
    const raw = await rawRequest("/uploads/bucket", {}, BUCKET_TIMEOUT_MS);
    return parseJsonOrThrow<BucketStatusResponse>(raw);
  },

  ensureBucket: async (): Promise<BucketStatusResponse> => {
    const raw = await rawRequest("/uploads/bucket", { method: "POST" }, BUCKET_TIMEOUT_MS);
    const parsed = tryParseObject(raw.text);
    if (parsed && typeof parsed.state === "string" && typeof parsed.usable === "boolean") {
      return parsed as unknown as BucketStatusResponse;
    }
    return parseJsonOrThrow<BucketStatusResponse>(raw);
  },

  listWorkspaces: async (): Promise<UploadWorkspace[]> => {
    const raw = await rawRequest("/uploads");
    return parseJsonOrThrow<{ workspaces: UploadWorkspace[] }>(raw).workspaces;
  },

  getWorkspace: async (workspaceId: string): Promise<UploadWorkspace> => {
    const raw = await rawRequest(`/uploads/${encodeURIComponent(workspaceId)}`);
    return parseJsonOrThrow<{ workspace: UploadWorkspace }>(raw).workspace;
  },

  createWorkspace: async (): Promise<CreateWorkspaceResponse> => {
    const raw = await rawRequest("/uploads", { method: "POST" }, BUCKET_TIMEOUT_MS);
    const parsed = tryParseObject(raw.text);
    if (!raw.ok && parsed && typeof parsed.error === "string") {
      throw new ApiError(raw.url, raw.status, parsed.error);
    }
    return parseJsonOrThrow<CreateWorkspaceResponse>(raw);
  },

  deleteWorkspace: async (workspaceId: string): Promise<DeleteWorkspaceResponse> => {
    const raw = await rawRequest(`/uploads/${encodeURIComponent(workspaceId)}`, { method: "DELETE" }, DELETE_TIMEOUT_MS);
    return parseJsonOrThrow<DeleteWorkspaceResponse>(raw);
  },

  createFileSession: async (workspaceId: string, filename: string, sizeBytes: number): Promise<CreateFileSessionResponse> => {
    const raw = await rawRequest(`/uploads/${encodeURIComponent(workspaceId)}/files`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, size_bytes: sizeBytes }),
    });
    return parseJsonOrThrow<CreateFileSessionResponse>(raw);
  },

  uploadFileToSession: async (
    sessionUri: string,
    file: File,
    onProgress?: (committed: number, total: number) => void,
    signal?: AbortSignal
  ): Promise<void> =>
    uploadViaResumableSession({
      sessionUri,
      totalBytes: file.size,
      signal,
      sliceBlob: (start, end) => file.slice(start, end, "application/pdf"),
      sendPut: sendChunkPut,
      onProgress,
    }),

  finalizeFile: async (workspaceId: string, fileId: string): Promise<FinalizeFileOutcome> => {
    const raw = await rawRequest(
      `/uploads/${encodeURIComponent(workspaceId)}/files/${encodeURIComponent(fileId)}/finalize`,
      { method: "POST" },
      FINALIZE_TIMEOUT_MS
    );
    const parsed = tryParseObject(raw.text);
    if (raw.status === 422 && parsed && typeof parsed.file === "object" && parsed.file !== null) {
      return {
        accepted: false,
        file: parsed.file as UploadFileEntry,
        error: typeof parsed.error === "string" ? parsed.error : "PDF rejected by validation",
      };
    }
    const ok = parseJsonOrThrow<{ file: UploadFileEntry }>(raw);
    return { accepted: true, file: ok.file, error: null };
  },

  estimateIngest: async (workspaceId: string, level: string): Promise<UploadCostEstimate> => {
    const q = new URLSearchParams({ level }).toString();
    const raw = await rawRequest(`/uploads/${encodeURIComponent(workspaceId)}/estimate?${q}`);
    return parseJsonOrThrow<{ estimate: UploadCostEstimate }>(raw).estimate;
  },

  startIngest: async (
    workspaceId: string,
    level: string,
    confirmCostUsd?: number
  ): Promise<StartUploadIngestOutcome> => {
    const payload: Record<string, unknown> = { level };
    if (typeof confirmCostUsd === "number" && Number.isFinite(confirmCostUsd)) {
      payload.confirm_cost_usd = confirmCostUsd;
    }
    const raw = await rawRequest(`/uploads/${encodeURIComponent(workspaceId)}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const parsed = tryParseObject(raw.text);
    if (raw.status === 409 && parsed?.needs_confirmation === true && parsed.estimate) {
      return {
        started: false,
        job: null,
        needsConfirmation: true,
        estimate: parsed.estimate as UploadCostEstimate,
        error: typeof parsed.error === "string" ? parsed.error : null,
      };
    }
    const ok = parseJsonOrThrow<{ job: IngestJobCreated }>(raw);
    return { started: true, job: ok.job, needsConfirmation: false, estimate: null, error: null };
  },
};
