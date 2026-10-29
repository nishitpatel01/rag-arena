import { useCallback, useMemo } from "react";
import { DEV_FIXTURE_JOB_PREFIX, useIngestSession } from "../../app/IngestSessions";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { selectIngestForIngestPage } from "./ingestSession";
import { INITIAL_STREAM_STATE, type StreamState } from "../levels/model";
import { isMockRequested } from "../levels/useBattleStream";

/** Levels with a DEV `?mock=1` ingestion fixture (managed import job). */
export const DEV_INGEST_FIXTURE_LEVELS: readonly string[] = ["managed_vertex"];

export interface IngestJobStream {
  readonly state: StreamState;
  readonly jobId: string | null;
  readonly level: string | null;
  readonly scope: string | null;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly selectedStage: string | null;
  readonly selectStage: (stageId: string | null) => void;
  readonly dismiss: () => void;
  readonly start: (level: string, onComplete?: () => void) => Promise<void>;
  readonly startCustom: (
    level: string,
    createJob: () => Promise<{ job_id: string }>,
    onComplete?: () => void,
    scope?: string | null
  ) => Promise<void>;
  readonly abort: () => void;
}

/**
 * Reads and controls the persistent app-wide IngestSession for `/ingest`.
 * Unmounting `/ingest` NEVER aborts the stream or drops accumulated progress.
 */
export function useIngestJobStream(): IngestJobStream {
  const { session, snapshot } = useIngestSession();
  const { mask, maskValue } = useSafeMode();
  const entry = useMemo(() => selectIngestForIngestPage(snapshot), [snapshot]);

  const startCustom = useCallback(
    async (
      nextLevel: string,
      createJob: () => Promise<{ job_id: string }>,
      onComplete?: () => void,
      scope: string | null = null
    ): Promise<void> => {
      await session.start(nextLevel, createJob, { scope, onComplete });
    },
    [session]
  );

  const start = useCallback(
    async (nextLevel: string, onComplete?: () => void): Promise<void> => {
      if (import.meta.env.DEV && isMockRequested() && DEV_INGEST_FIXTURE_LEVELS.includes(nextLevel)) {
        await session.start(
          nextLevel,
          async () => ({ job_id: `${DEV_FIXTURE_JOB_PREFIX}${nextLevel}` }),
          { scope: null, onComplete }
        );
        return;
      }
      await session.start(nextLevel, () => api.createIngestJob(nextLevel), { scope: null, onComplete });
    },
    [session]
  );

  const entryLevel = entry?.level ?? null;
  const selectStage = useCallback(
    (stageId: string | null) => {
      if (entryLevel) session.selectStage(entryLevel, stageId);
    },
    [session, entryLevel]
  );
  const dismiss = useCallback(() => {
    if (entryLevel) session.dismissOnIngest(entryLevel);
  }, [session, entryLevel]);
  const abort = useCallback(() => session.abort(entryLevel ?? undefined), [session, entryLevel]);

  const shownState = useMemo(() => (entry ? maskValue(entry.state) : INITIAL_STREAM_STATE), [entry, maskValue]);
  const shownError = entry?.transportError ? mask(entry.transportError) : null;
  const shownScope = entry?.scope ? mask(entry.scope) : null;

  return {
    state: shownState,
    jobId: entry?.jobId ?? null,
    level: entryLevel,
    scope: shownScope,
    isStreaming: snapshot.anyStreaming,
    transportError: shownError,
    selectedStage: entry?.selectedStage ?? null,
    selectStage,
    dismiss,
    start,
    startCustom,
    abort,
  };
}
