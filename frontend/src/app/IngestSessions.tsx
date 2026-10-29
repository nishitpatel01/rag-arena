import React, { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useSafeMode } from "./SafeMode";
import { api } from "../lib/api";
import { openSseStream, type SseFrame } from "../lib/sse";
import {
  IngestSession,
  ingestDataLevelFor,
  selectIngestForLevelPage,
  type IngestJobEntry,
  type IngestSessionSnapshot,
  type OpenIngestTransport,
} from "../features/ingest/ingestSession";
import { broadcastWorkspacesChanged } from "../features/uploads/useUploadWorkspaces";

export const DEV_FIXTURE_JOB_PREFIX = "devfixture-ing-";
const INGEST_STREAM_TIMEOUT_MS = 30 * 60_000;

function createRealOrMockTransport(): OpenIngestTransport {
  return (jobId, _level, cb) => {
    if (import.meta.env.DEV && jobId.startsWith(DEV_FIXTURE_JOB_PREFIX)) {
      const timers: number[] = [];
      let cancelled = false;
      void import("../dev/fixtures/level5_run").then((mod) => {
        if (cancelled) return;
        const fixture = mod.level5IngestFixture();
        let offset = 0;
        fixture.forEach((item, index) => {
          offset += item.t_rel;
          const id = window.setTimeout(() => {
            if (cancelled) return;
            cb.onFrame({ ...item.event, ts: new Date().toISOString() });
            if (index === fixture.length - 1) cb.onEnd();
          }, offset);
          timers.push(id);
        });
      });
      return {
        abort: () => {
          cancelled = true;
          for (const id of timers) window.clearTimeout(id);
        },
      };
    }
    return openSseStream({
      url: api.ingestJobStreamUrl(jobId),
      method: "GET",
      timeoutMs: INGEST_STREAM_TIMEOUT_MS,
      onFrame: (frame: SseFrame) => cb.onFrame(frame),
      onError: cb.onError,
      onEnd: cb.onEnd,
    });
  };
}

const IngestSessionContext = createContext<IngestSession | null>(null);

/**
 * Holds the app-wide IngestSession above the router so ingestion progress, stage selections,
 * and completion callbacks survive route changes (and re-attach after a full browser reload).
 */
export const IngestSessionProvider: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => {
  const [session] = useState(
    () =>
      new IngestSession(createRealOrMockTransport(), (entry) => {
        if (entry.scope && entry.scope.startsWith("upl_")) {
          broadcastWorkspacesChanged("ingest-session");
        }
      })
  );

  // Re-attach any in-flight backend ingestion job on initial app mount (even before /ingest is visited).
  useEffect(() => {
    let cancelled = false;
    api.ingestStatus().then(
      (s) => {
        if (!cancelled) session.syncActiveJobs(s.active_jobs);
      },
      () => {
        // Status errors are surfaced on IngestPage / LevelPage via useIngestStatus; never silent-crash startup.
      }
    );
    return () => {
      cancelled = true;
    };
  }, [session]);

  return <IngestSessionContext.Provider value={session}>{children}</IngestSessionContext.Provider>;
};

export function useOptionalIngestSession(): {
  readonly session: IngestSession;
  readonly snapshot: IngestSessionSnapshot;
} | null {
  const session = useContext(IngestSessionContext);
  const noopSubscribe = useCallback(() => () => {}, []);
  const emptySnap = useMemo<IngestSessionSnapshot>(
    () => ({ entries: {}, currentLevel: null, anyStreaming: false, completedSeq: 0 }),
    []
  );
  const snapshot = useSyncExternalStore(
    session ? session.subscribe : noopSubscribe,
    session ? session.getSnapshot : () => emptySnap,
    session ? session.getSnapshot : () => emptySnap
  );
  return session ? { session, snapshot } : null;
}

export function useIngestSession(): {
  readonly session: IngestSession;
  readonly snapshot: IngestSessionSnapshot;
} {
  const ctx = useOptionalIngestSession();
  if (!ctx) throw new Error("useIngestSession used outside <IngestSessionProvider>");
  return ctx;
}

export interface LevelIngestView {
  readonly entry: IngestJobEntry;
  readonly backingLevelId: string;
  readonly selectStage: (stageId: string | null) => void;
  readonly dismiss: () => void;
}

/** Masked ingestion view for `/levels/<levelId>` (including L4 observing L3 multimodal ingestion). */
export function useLevelIngest(levelId: string): LevelIngestView | null {
  const { session, snapshot } = useIngestSession();
  const { mask, maskValue } = useSafeMode();
  const raw = useMemo(
    () => (levelId ? selectIngestForLevelPage(snapshot, levelId) : null),
    [snapshot, levelId]
  );
  const backing = levelId ? ingestDataLevelFor(levelId) : "";
  const selectStage = useCallback((stageId: string | null) => session.selectStage(backing, stageId), [session, backing]);
  const dismiss = useCallback(() => session.dismissOnLevel(levelId), [session, levelId]);
  const maskedEntry = useMemo<IngestJobEntry | null>(() => {
    if (!raw) return null;
    const e = raw.entry;
    return {
      ...e,
      scope: e.scope ? mask(e.scope) : null,
      state: maskValue(e.state),
      transportError: e.transportError === null ? null : mask(e.transportError),
    };
  }, [raw, mask, maskValue]);

  if (!raw || !maskedEntry) return null;
  return {
    entry: maskedEntry,
    backingLevelId: raw.backingLevelId,
    selectStage,
    dismiss,
  };
}
