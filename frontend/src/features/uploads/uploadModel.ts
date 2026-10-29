/**
 * Upload workspace pure model + resumable Cloud Storage upload protocol (no React, no DOM).
 *
 * Every loop and retry is explicitly bounded. Intermediate chunks are aligned to 256 KiB per the
 * Google Cloud Storage resumable upload specification, with `308 Resume Incomplete` offset recovery.
 */

import type { LevelChoice } from "../compare/compareModel";
import type { RunGate } from "../ingest/ingestStatus";

/** Empty scope string selects the fixed 45-document procurement corpus. */
export const CORPUS_SCOPE = "";
export const UPLOAD_SCOPE_STORAGE_KEY = "rag-arena.upload-scope";
export const RESUMABLE_GRANULARITY_BYTES = 256 * 1024;
export const RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024;
export const MAX_CHUNK_RETRIES = 3;
export const UPLOAD_INGEST_LEVELS = [
  "level1_naive",
  "level2_hybrid",
  "level3_multimodal",
  "managed_vertex",
] as const;
export type UploadIngestLevel = (typeof UPLOAD_INGEST_LEVELS)[number];

const WORKSPACE_ID_RE = /^upl_[0-9a-f]{12}$/;
const RANGE_HEADER_RE = /bytes=\d+-(\d+)/i;

export type BucketStateName =
  | "ok"
  | "created"
  | "permission_denied"
  | "org_policy"
  | "name_conflict"
  | "not_found"
  | "unsafe_bucket"
  | "unreachable"
  | "error"
  | (string & {});

export interface BucketStatusResponse {
  readonly state: BucketStateName;
  readonly usable: boolean;
  readonly created: boolean;
  readonly bucket: string;
  readonly message: string;
  readonly remedy: string | null;
  readonly problems: readonly string[];
}

export interface UploadFileEntry {
  readonly id: string;
  readonly name: string;
  readonly status: "uploading" | "ready" | "rejected" | (string & {});
  readonly pages?: number;
  readonly size_bytes?: number;
  readonly declared_size_bytes?: number;
  readonly sha256?: string;
  readonly error?: string;
  readonly created_at?: string;
  readonly finalized_at?: string;
}

export interface UploadLevelStatus {
  readonly status: "starting" | "running" | "completed" | "failed" | (string & {});
  readonly job_id?: string;
  readonly chunks?: number;
  readonly tier_profile?: string;
  readonly file_ids?: readonly string[];
  readonly error?: string | null;
  readonly started_at?: string;
  readonly finished_at?: string;
}

export interface UploadWorkspace {
  readonly id: string;
  readonly version?: number;
  readonly created_at: string;
  readonly files: readonly UploadFileEntry[];
  readonly levels: Readonly<Record<string, UploadLevelStatus>>;
}

export interface UploadCostEstimate {
  readonly level: string;
  readonly pages: number;
  readonly requires_confirmation: boolean;
  readonly max_usd: number;
  readonly basis: string;
  readonly vision_model?: string;
  readonly documentai_usd?: number;
  readonly vision_max_usd?: number;
}

export interface UploadChunk {
  readonly start: number;
  readonly endExclusive: number;
}

export interface ResumablePutResult {
  readonly status: number;
  readonly rangeHeader: string | null;
  readonly bodyText?: string;
}

export interface ResumableUploadOptions {
  readonly sessionUri: string;
  readonly totalBytes: number;
  readonly chunkBytes?: number;
  readonly maxRetries?: number;
  readonly signal?: AbortSignal;
  readonly sliceBlob: (start: number, endExclusive: number) => Blob;
  readonly sendPut: (
    uri: string,
    headers: Readonly<Record<string, string>>,
    body: Blob | null,
    signal?: AbortSignal
  ) => Promise<ResumablePutResult>;
  readonly onProgress?: (bytesCommitted: number, totalBytes: number) => void;
}

export function uploadsEnabled(demoMode: string | null | undefined): boolean {
  return demoMode === "live" || demoMode === "record";
}

export function isUploadScope(scope: string | null | undefined): scope is string {
  return typeof scope === "string" && WORKSPACE_ID_RE.test(scope);
}

export function normalizeScopeSelection(
  picked: string | null | undefined,
  workspaces: readonly UploadWorkspace[],
  demoMode: string | null | undefined
): string {
  if (!uploadsEnabled(demoMode) || !isUploadScope(picked)) return CORPUS_SCOPE;
  return workspaces.some((w) => w.id === picked) ? picked : CORPUS_SCOPE;
}

/** Merges server-listed workspaces with any just-created workspaces not yet visible in list_blobs. */
export function mergeWorkspaceList(
  fetched: readonly UploadWorkspace[],
  optimistic: readonly UploadWorkspace[]
): UploadWorkspace[] {
  const seen = new Set(fetched.map((w) => w.id));
  const missing = optimistic.filter((w) => !seen.has(w.id));
  return [...missing, ...fetched];
}

/** Validates extension and non-empty byte count; imposes NO size cap (D3). */
export function validateUploadPdfCandidate(fileName: string, sizeBytes: number): string | null {
  if (typeof fileName !== "string" || !fileName.trim().toLowerCase().endsWith(".pdf")) {
    return "Only .pdf documents are accepted.";
  }
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return "Selected PDF file is empty.";
  }
  return null;
}

export function contentRangeHeader(start: number, endExclusive: number, totalBytes: number): string {
  if (start < 0 || endExclusive <= start || endExclusive > totalBytes) {
    throw new Error(`invalid upload byte range [${start}, ${endExclusive}) of ${totalBytes}`);
  }
  return `bytes ${start}-${endExclusive - 1}/${totalBytes}`;
}

export function parseCommittedOffset(rangeHeader: string | null | undefined, totalBytes: number): number {
  if (typeof rangeHeader !== "string" || rangeHeader.length === 0) return 0;
  const match = RANGE_HEADER_RE.exec(rangeHeader);
  if (!match) return 0;
  const lastByte = Number.parseInt(match[1], 10);
  if (!Number.isFinite(lastByte) || lastByte < 0) return 0;
  return Math.min(totalBytes, lastByte + 1);
}

function alignedChunkBytes(chunkBytes: number): number {
  if (!Number.isFinite(chunkBytes) || chunkBytes < RESUMABLE_GRANULARITY_BYTES) {
    throw new Error(`chunkBytes must be >= ${RESUMABLE_GRANULARITY_BYTES}`);
  }
  if (chunkBytes % RESUMABLE_GRANULARITY_BYTES !== 0) {
    throw new Error(`chunkBytes (${chunkBytes}) must be a multiple of 256 KiB`);
  }
  return chunkBytes;
}

export function planUploadChunks(
  totalBytes: number,
  startOffset = 0,
  chunkBytes = RESUMABLE_CHUNK_BYTES
): UploadChunk[] {
  if (!Number.isFinite(totalBytes) || totalBytes <= 0) {
    throw new Error("totalBytes must be > 0");
  }
  if (!Number.isFinite(startOffset) || startOffset < 0 || startOffset > totalBytes) {
    throw new Error("startOffset out of bounds");
  }
  const step = alignedChunkBytes(chunkBytes);
  const maxChunks = Math.ceil((totalBytes - startOffset) / RESUMABLE_GRANULARITY_BYTES) + 2;
  const out: UploadChunk[] = [];
  let cursor = startOffset;
  for (let i = 0; i < maxChunks && cursor < totalBytes; i += 1) {
    const rawEnd = cursor + step;
    const aligned = rawEnd >= totalBytes ? totalBytes : rawEnd - (rawEnd % RESUMABLE_GRANULARITY_BYTES);
    const endExclusive = aligned > cursor ? aligned : Math.min(cursor + RESUMABLE_GRANULARITY_BYTES, totalBytes);
    out.push({ start: cursor, endExclusive });
    cursor = endExclusive;
  }
  return out;
}

async function probeSessionOffset(opts: ResumableUploadOptions): Promise<number> {
  const probe = await opts.sendPut(
    opts.sessionUri,
    { "Content-Range": `bytes */${opts.totalBytes}` },
    null,
    opts.signal
  );
  if (probe.status === 200 || probe.status === 201) return opts.totalBytes;
  if (probe.status === 308) return parseCommittedOffset(probe.rangeHeader, opts.totalBytes);
  throw new Error(`resumable upload status check failed (HTTP ${probe.status})`);
}

/** Stream slices directly to a GCS resumable session URI with bounded chunk retries and 308 offset recovery. */
export async function uploadViaResumableSession(opts: ResumableUploadOptions): Promise<void> {
  if (!opts.sessionUri || opts.totalBytes <= 0) {
    throw new Error("sessionUri and positive totalBytes are required");
  }
  const step = alignedChunkBytes(opts.chunkBytes ?? RESUMABLE_CHUNK_BYTES);
  const maxRetries = Math.max(0, Math.min(10, opts.maxRetries ?? MAX_CHUNK_RETRIES));
  const maxSteps = Math.ceil(opts.totalBytes / RESUMABLE_GRANULARITY_BYTES) * (maxRetries + 2) + 4;

  let offset = 0;
  let retries = 0;
  for (let iter = 0; iter < maxSteps && offset < opts.totalBytes; iter += 1) {
    if (opts.signal?.aborted) throw new Error("upload cancelled");
    const rawEnd = offset + step;
    const aligned = rawEnd >= opts.totalBytes ? opts.totalBytes : rawEnd - (rawEnd % RESUMABLE_GRANULARITY_BYTES);
    const end = aligned > offset ? aligned : Math.min(offset + RESUMABLE_GRANULARITY_BYTES, opts.totalBytes);
    const headers = { "Content-Range": contentRangeHeader(offset, end, opts.totalBytes) };
    try {
      const res = await opts.sendPut(opts.sessionUri, headers, opts.sliceBlob(offset, end), opts.signal);
      if (res.status === 200 || res.status === 201) {
        opts.onProgress?.(opts.totalBytes, opts.totalBytes);
        return;
      }
      if (res.status === 308) {
        const committed = parseCommittedOffset(res.rangeHeader, opts.totalBytes);
        offset = committed > offset ? committed : end;
        retries = 0;
        opts.onProgress?.(offset, opts.totalBytes);
        continue;
      }
      if (res.status >= 500 && retries < maxRetries) {
        throw new Error(`Cloud Storage returned HTTP ${res.status}`);
      }
      throw new Error(
        `upload to Cloud Storage failed (HTTP ${res.status}${res.bodyText ? `: ${res.bodyText.slice(0, 240)}` : ""})`
      );
    } catch (err) {
      if (opts.signal?.aborted || retries >= maxRetries) throw err;
      retries += 1;
      offset = await probeSessionOffset(opts);
      opts.onProgress?.(offset, opts.totalBytes);
    }
  }
  if (offset < opts.totalBytes) {
    throw new Error("resumable upload exceeded bounded step budget before completion");
  }
}

/** Level 4's ADK tools search Level 3 (`multimodal`) rows; every other level owns its own data. */
export function dataLevelFor(levelId: string): string {
  return levelId === "level4_agentic" ? "level3_multimodal" : levelId;
}

export function isWorkspaceLevelReady(ws: UploadWorkspace | null | undefined, levelId: string): boolean {
  if (!ws) return false;
  const dataLevel = dataLevelFor(levelId);
  return ws.levels?.[dataLevel]?.status === "completed";
}

export function uploadRunGateFor(levelId: string, ws: UploadWorkspace | null | undefined): RunGate {
  if (!ws) {
    return {
      blocked: true,
      tone: "warn",
      count: null,
      note: "Selected upload workspace is unavailable — pick another workspace or switch to Fixed procurement corpus.",
    };
  }
  const dataLevel = dataLevelFor(levelId);
  const entry = ws.levels?.[dataLevel];
  if (entry?.status === "completed") {
    return {
      blocked: false,
      tone: "ok",
      count: typeof entry.chunks === "number" ? entry.chunks : null,
      note: null,
    };
  }
  const note =
    dataLevel === levelId
      ? `Workspace ${ws.id} is not ingested for ${dataLevel} yet — open Corpus & Ingest to run it.`
      : `${levelId} searches ${dataLevel} rows; workspace ${ws.id} is not ingested for ${dataLevel} yet — open Corpus & Ingest to run it.`;
  return { blocked: true, tone: "warn", count: 0, note };
}

export function applyUploadScopeToChoices(
  choices: readonly LevelChoice[],
  ws: UploadWorkspace | null | undefined
): LevelChoice[] {
  return choices.map((c) => {
    if (c.disabledReason !== null) return c;
    if (isWorkspaceLevelReady(ws, c.levelId)) return c;
    const dataLevel = dataLevelFor(c.levelId);
    const reason = ws
      ? `not ingested for ${ws.id} (${dataLevel}) — ingest on Corpus & Ingest first`
      : "selected upload workspace not found";
    return { ...c, disabledReason: reason };
  });
}

export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}
