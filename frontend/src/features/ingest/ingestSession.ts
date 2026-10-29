/**
 * App-wide ingestion session (pure TypeScript, no React). Lives above routes so unsubscribing
 * (unmounting `/ingest` or a RAG level page) NEVER aborts an in-flight SSE stream or drops progress.
 * Exposes per-level views for `/levels/<levelId>` (including L4 reading L3 multimodal ingestion) and
 * idempotently re-attaches active backend jobs reported by `GET /api/v1/ingest/status`.
 */
import type { IngestEvent } from "../../types/arena";
import {
  INITIAL_STREAM_STATE,
  activeStageId,
  reduceStreamEvent,
  streamEndReason,
  streamInterruptedEvent,
  type StreamState,
} from "../levels/model";

export const MAX_TRACKED_INGEST_LEVELS = 8;
export const MAX_ABORTED_JOB_IDS = 32;

export interface IngestTransportCallbacks {
  readonly onFrame: (frame: unknown) => void;
  readonly onError: (err: Error) => void;
  readonly onEnd: () => void;
}

export interface IngestTransportHandle {
  readonly abort: () => void;
}

export type OpenIngestTransport = (
  jobId: string,
  level: string,
  cb: IngestTransportCallbacks
) => IngestTransportHandle;

export interface ActiveIngestJobSummary {
  readonly job_id: string;
  readonly level: string;
  readonly status: string;
  readonly scope?: string | null;
  readonly tier_profile?: string | null;
}

export interface IngestJobEntry {
  readonly level: string;
  readonly jobId: string | null;
  readonly scope: string | null;
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly selectedStage: string | null;
  readonly userPickedStage: boolean;
  readonly dismissedOnIngest: boolean;
  readonly dismissedOnLevel: boolean;
  readonly startedSeq: number;
}

export interface IngestSessionSnapshot {
  readonly entries: Readonly<Record<string, IngestJobEntry>>;
  readonly currentLevel: string | null;
  readonly anyStreaming: boolean;
  readonly completedSeq: number;
}

/** Level 4's ADK agent queries Level 3's multimodal rows, so L4 tracks L3 ingestion. */
export function ingestDataLevelFor(levelId: string): string {
  if (typeof levelId !== "string" || levelId.trim().length === 0) {
    throw new Error("ingestDataLevelFor: levelId must be a non-empty string");
  }
  return levelId === "level4_agentic" ? "level3_multimodal" : levelId;
}

/** Job shown on `/ingest`: current level (unless dismissed once finished), else newest streaming entry. */
export function selectIngestForIngestPage(snap: IngestSessionSnapshot): IngestJobEntry | null {
  const cur = snap.currentLevel ? snap.entries[snap.currentLevel] : undefined;
  if (cur && (cur.isStreaming || !cur.dismissedOnIngest)) return cur;
  let newest: IngestJobEntry | null = null;
  for (const entry of Object.values(snap.entries)) {
    if (!entry.isStreaming) continue;
    if (!newest || entry.startedSeq > newest.startedSeq) newest = entry;
  }
  return newest;
}

/** Job shown on `/levels/<levelId>`: live job or undismissed completed/errored job for its data level. */
export function selectIngestForLevelPage(
  snap: IngestSessionSnapshot,
  levelId: string
): { readonly entry: IngestJobEntry; readonly backingLevelId: string } | null {
  const backingLevelId = ingestDataLevelFor(levelId);
  const entry = snap.entries[backingLevelId];
  if (!entry) return null;
  if (entry.isStreaming) return { entry, backingLevelId };
  const hasPayload = entry.state.events.length > 0 || entry.transportError !== null;
  if (!entry.dismissedOnLevel && hasPayload) return { entry, backingLevelId };
  return null;
}

export interface StartIngestOptions {
  readonly scope?: string | null;
  readonly onComplete?: () => void;
}

const EMPTY_ENTRIES: Readonly<Record<string, IngestJobEntry>> = Object.freeze({});

export class IngestSession {
  private entries: Record<string, IngestJobEntry> = { ...EMPTY_ENTRIES };
  private handles = new Map<string, IngestTransportHandle>();
  private gens = new Map<string, number>();
  private abortedIds: string[] = [];
  private currentLevel: string | null = null;
  private seq = 0;
  private completedSeq = 0;
  private readonly listeners = new Set<() => void>();
  private snapshot: IngestSessionSnapshot;

  constructor(
    private readonly openTransport: OpenIngestTransport,
    private readonly onJobSettled?: (entry: IngestJobEntry) => void
  ) {
    if (typeof openTransport !== "function") throw new Error("IngestSession: openTransport must be callable");
    this.snapshot = this.buildSnapshot();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly getSnapshot = (): IngestSessionSnapshot => this.snapshot;

  readonly start = async (
    level: string,
    createJob: () => Promise<{ job_id: string }>,
    options: StartIngestOptions = {}
  ): Promise<void> => {
    if (typeof level !== "string" || level.trim().length === 0) throw new Error("IngestSession.start: invalid level");
    if (typeof createJob !== "function") throw new Error("IngestSession.start: createJob must be callable");
    const gen = this.stopLevel(level);
    this.ensureCapacityFor(level);
    this.currentLevel = level;
    this.seq += 1;
    this.entries = {
      ...this.entries,
      [level]: {
        level,
        jobId: null,
        scope: options.scope ?? null,
        state: INITIAL_STREAM_STATE,
        isStreaming: true,
        transportError: null,
        selectedStage: null,
        userPickedStage: false,
        dismissedOnIngest: false,
        dismissedOnLevel: false,
        startedSeq: this.seq,
      },
    };
    this.emit();
    let created: { job_id: string };
    try {
      created = await createJob();
    } catch (err) {
      if (this.gens.get(level) !== gen) return;
      const msg = err instanceof Error ? err.message : String(err);
      this.patchEntry(level, { isStreaming: false, transportError: msg });
      return;
    }
    if (this.gens.get(level) !== gen) return;
    this.openForLevel(level, created.job_id, gen, options.onComplete);
  };

  /** Attach to in-flight backend jobs (e.g. after F5 browser reload) without duplicating active streams. */
  readonly syncActiveJobs = (activeJobs: readonly ActiveIngestJobSummary[] | null | undefined): void => {
    if (!Array.isArray(activeJobs) || activeJobs.length === 0) return;
    for (const raw of activeJobs.slice(0, MAX_TRACKED_INGEST_LEVELS)) {
      if (!raw || typeof raw.job_id !== "string" || !raw.job_id || typeof raw.level !== "string" || !raw.level) continue;
      if (raw.status === "completed" || raw.status === "failed") continue;
      if (this.abortedIds.includes(raw.job_id)) continue;
      const existing = this.entries[raw.level];
      if (existing && (existing.isStreaming || existing.jobId === raw.job_id)) continue;
      const gen = this.stopLevel(raw.level);
      this.ensureCapacityFor(raw.level);
      if (this.currentLevel === null || !this.entries[this.currentLevel]?.isStreaming) {
        this.currentLevel = raw.level;
      }
      this.seq += 1;
      this.entries = {
        ...this.entries,
        [raw.level]: {
          level: raw.level,
          jobId: raw.job_id,
          scope: typeof raw.scope === "string" && raw.scope ? raw.scope : null,
          state: INITIAL_STREAM_STATE,
          isStreaming: true,
          transportError: null,
          selectedStage: null,
          userPickedStage: false,
          dismissedOnIngest: false,
          dismissedOnLevel: false,
          startedSeq: this.seq,
        },
      };
      this.emit();
      this.openForLevel(raw.level, raw.job_id, gen);
    }
  };

  readonly selectStage = (level: string, stageId: string | null): void => {
    const entry = this.entries[level];
    if (!entry) return;
    this.patchEntry(level, { selectedStage: stageId, userPickedStage: stageId !== null });
  };

  readonly dismissOnIngest = (level: string): void => {
    const entry = this.entries[level];
    if (!entry || entry.isStreaming || entry.dismissedOnIngest) return;
    this.patchEntry(level, { dismissedOnIngest: true });
  };

  readonly dismissOnLevel = (level: string): void => {
    const backing = ingestDataLevelFor(level);
    const entry = this.entries[backing];
    if (!entry || entry.isStreaming || entry.dismissedOnLevel) return;
    this.patchEntry(backing, { dismissedOnLevel: true });
  };

  readonly abort = (level?: string): void => {
    const target = level ?? this.currentLevel;
    if (!target) return;
    const entry = this.entries[target];
    if (!entry) return;
    if (entry.jobId) this.recordAbortedId(entry.jobId);
    this.stopLevel(target);
    const reason = streamEndReason({ userAborted: true, error: null });
    const nextState = reduceStreamEvent(entry.state, streamInterruptedEvent(entry.state.runId, reason));
    this.patchEntry(target, { isStreaming: false, state: nextState });
  };

  private openForLevel(level: string, jobId: string, gen: number, onComplete?: () => void): void {
    let lastError: string | null = null;
    this.patchEntry(level, { jobId });
    const handle = this.openTransport(jobId, level, {
      onFrame: (frame) => {
        if (this.gens.get(level) !== gen) return;
        const cur = this.entries[level];
        if (!cur) return;
        const nextState = reduceStreamEvent(cur.state, frame as IngestEvent);
        const active = activeStageId(nextState.timelines[level]);
        const selectedStage = cur.userPickedStage ? cur.selectedStage : active ?? cur.selectedStage;
        const frameScope =
          typeof frame === "object" && frame !== null && typeof (frame as { scope?: unknown }).scope === "string"
            ? ((frame as { scope: string }).scope || null)
            : cur.scope;
        this.patchEntry(level, { state: nextState, selectedStage, scope: frameScope });
      },
      onError: (err) => {
        if (this.gens.get(level) !== gen) return;
        lastError = err.message;
        this.patchEntry(level, { transportError: err.message });
      },
      onEnd: () => {
        if (this.gens.get(level) !== gen) return;
        this.handles.delete(level);
        const cur = this.entries[level];
        if (!cur) return;
        const reason = streamEndReason({ userAborted: false, error: lastError });
        const nextState = reduceStreamEvent(cur.state, streamInterruptedEvent(cur.state.runId, reason));
        this.completedSeq += 1;
        this.patchEntry(level, { isStreaming: false, state: nextState });
        const finished = this.entries[level];
        if (finished) this.onJobSettled?.(finished);
        onComplete?.();
      },
    });
    this.handles.set(level, handle);
  }

  private stopLevel(level: string): number {
    const nextGen = (this.gens.get(level) ?? 0) + 1;
    this.gens.set(level, nextGen);
    const h = this.handles.get(level);
    this.handles.delete(level);
    h?.abort();
    return nextGen;
  }

  private ensureCapacityFor(level: string): void {
    if (level in this.entries) return;
    const keys = Object.keys(this.entries);
    if (keys.length < MAX_TRACKED_INGEST_LEVELS) return;
    let victim: IngestJobEntry | null = null;
    for (const entry of Object.values(this.entries)) {
      if (entry.isStreaming) continue;
      if (!victim || entry.startedSeq < victim.startedSeq) victim = entry;
    }
    if (!victim) return;
    const copy = { ...this.entries };
    delete copy[victim.level];
    this.entries = copy;
  }

  private recordAbortedId(jobId: string): void {
    if (this.abortedIds.includes(jobId)) return;
    this.abortedIds = [...this.abortedIds.slice(-(MAX_ABORTED_JOB_IDS - 1)), jobId];
  }

  private patchEntry(level: string, patch: Partial<IngestJobEntry>): void {
    const cur = this.entries[level];
    if (!cur) return;
    const next: IngestJobEntry = { ...cur, ...patch };
    if (
      cur.jobId === next.jobId &&
      cur.scope === next.scope &&
      cur.state === next.state &&
      cur.isStreaming === next.isStreaming &&
      cur.transportError === next.transportError &&
      cur.selectedStage === next.selectedStage &&
      cur.userPickedStage === next.userPickedStage &&
      cur.dismissedOnIngest === next.dismissedOnIngest &&
      cur.dismissedOnLevel === next.dismissedOnLevel
    ) {
      return;
    }
    this.entries = { ...this.entries, [level]: next };
    this.emit();
  }

  private buildSnapshot(): IngestSessionSnapshot {
    const anyStreaming = Object.values(this.entries).some((e) => e.isStreaming);
    return {
      entries: this.entries,
      currentLevel: this.currentLevel,
      anyStreaming,
      completedSeq: this.completedSeq,
    };
  }

  private emit(): void {
    const prev = this.snapshot;
    const next = this.buildSnapshot();
    if (
      prev.entries === next.entries &&
      prev.currentLevel === next.currentLevel &&
      prev.anyStreaming === next.anyStreaming &&
      prev.completedSeq === next.completedSeq
    ) {
      return;
    }
    this.snapshot = next;
    for (const listener of [...this.listeners]) listener();
  }
}
