import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { openSseStream, type SseFrame, type StreamHandle } from "../../lib/sse";
import type { IngestEvent } from "../../types/arena";
import { INITIAL_STREAM_STATE, reduceStreamEvent, streamEndReason, streamInterruptedEvent, type StreamState } from "../levels/model";
import { isMockRequested } from "../levels/useBattleStream";

/** Levels with a DEV `?mock=1` ingestion fixture (managed import job). */
export const DEV_INGEST_FIXTURE_LEVELS: readonly string[] = ["managed_vertex"];

const INGEST_STREAM_TIMEOUT_MS = 30 * 60_000;

export interface IngestJobStream {
  readonly state: StreamState;
  readonly jobId: string | null;
  readonly level: string | null;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly start: (level: string) => Promise<void>;
  readonly abort: () => void;
}

/** Creates an ingestion job and follows its SSE stream with the shared pure reducer. */
export function useIngestJobStream(): IngestJobStream {
  const [state, setState] = useState<StreamState>(INITIAL_STREAM_STATE);
  const [jobId, setJobId] = useState<string | null>(null);
  const [level, setLevel] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [transportError, setTransportError] = useState<string | null>(null);
  const handleRef = useRef<StreamHandle | null>(null);
  const mockTimersRef = useRef<number[]>([]);

  // Each job follow gets a generation; transport callbacks of a superseded stream are ignored.
  const genRef = useRef(0);

  const interrupt = useCallback((reason: string): void => {
    setState((prev) => reduceStreamEvent(prev, streamInterruptedEvent(prev.runId, reason)));
  }, []);

  const stop = useCallback((): void => {
    genRef.current += 1;
    handleRef.current?.abort();
    handleRef.current = null;
    for (const id of mockTimersRef.current) window.clearTimeout(id);
    mockTimersRef.current = [];
    setIsStreaming(false);
  }, []);

  /** User abort: stops following the stream and fails still-running stages loudly. */
  const abort = useCallback((): void => {
    stop();
    interrupt(streamEndReason({ userAborted: true, error: null }));
  }, [stop, interrupt]);

  const start = useCallback(
    async (nextLevel: string): Promise<void> => {
      stop();
      const gen = genRef.current;
      let lastError: string | null = null;
      setState(INITIAL_STREAM_STATE);
      setTransportError(null);
      setLevel(nextLevel);
      setIsStreaming(true);
      // DEV + ?mock=1 only: play the managed import fixture (statically removed from production bundles).
      if (import.meta.env.DEV && isMockRequested() && DEV_INGEST_FIXTURE_LEVELS.includes(nextLevel)) {
        const fixture = (await import("../../dev/fixtures/level5_run")).level5IngestFixture();
        if (gen !== genRef.current) return;
        setJobId("devfixture-ing-level5");
        let offset = 0;
        fixture.forEach((item, index) => {
          offset += item.t_rel;
          const id = window.setTimeout(() => {
            if (gen !== genRef.current) return;
            setState((prev) => reduceStreamEvent(prev, { ...item.event, ts: new Date().toISOString() } as unknown as IngestEvent));
            if (index === fixture.length - 1) setIsStreaming(false);
          }, offset);
          mockTimersRef.current.push(id);
        });
        return;
      }
      let created: { job_id: string };
      try {
        created = await api.createIngestJob(nextLevel);
      } catch (err) {
        if (gen !== genRef.current) return;
        setTransportError(err instanceof Error ? err.message : String(err));
        setIsStreaming(false);
        return;
      }
      if (gen !== genRef.current) return;
      setJobId(created.job_id);
      handleRef.current = openSseStream({
        url: api.ingestJobStreamUrl(created.job_id),
        method: "GET",
        timeoutMs: INGEST_STREAM_TIMEOUT_MS,
        onFrame: (frame: SseFrame) => {
          if (gen === genRef.current) setState((prev) => reduceStreamEvent(prev, frame as unknown as IngestEvent));
        },
        onError: (err) => {
          if (gen !== genRef.current) return;
          lastError = err.message;
          setTransportError(err.message);
        },
        onEnd: () => {
          if (gen !== genRef.current) return;
          setIsStreaming(false);
          interrupt(streamEndReason({ userAborted: false, error: lastError }));
        },
      });
    },
    [stop, interrupt]
  );

  useEffect(() => () => stop(), [stop]);

  // Screen-share safe mode: consumers only ever see the masked view of server data.
  const { mask, maskValue } = useSafeMode();
  const shownState = useMemo(() => maskValue(state), [maskValue, state]);
  const shownError = transportError === null ? null : mask(transportError);
  return { state: shownState, jobId, level, isStreaming, transportError: shownError, start, abort };
}
