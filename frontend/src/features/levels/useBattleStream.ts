import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { openSseStream, type SseFrame, type StreamHandle } from "../../lib/sse";
import type { BattleEvent } from "../../types/arena";
import { INITIAL_STREAM_STATE, reduceStreamEvent, type StreamState } from "./model";

export interface BattleRunParams {
  readonly query: string;
  readonly questionId?: string | null;
  readonly engines: readonly string[];
  readonly solicitationFilter?: string | null;
  /** Catalog ids; omitted from the request when null (server defaults apply). */
  readonly synthesisModel?: string | null;
  readonly judgeModel?: string | null;
}

export interface BattleStream {
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly isMock: boolean;
  readonly run: (params: BattleRunParams) => void;
  readonly abort: () => void;
  readonly reset: () => void;
}

export function isMockRequested(): boolean {
  return import.meta.env.DEV && typeof window !== "undefined" && window.location.search.includes("mock=1");
}

/**
 * Append-only event log + derived timeline. Every state change is caused by an SSE event
 * (or, in dev with `?mock=1`, by the fixture replayed at its own `t_rel` pacing).
 */
export function useBattleStream(): BattleStream {
  const [state, setState] = useState<StreamState>(INITIAL_STREAM_STATE);
  const [isStreaming, setIsStreaming] = useState(false);
  const [transportError, setTransportError] = useState<string | null>(null);
  const handleRef = useRef<StreamHandle | null>(null);
  const mockTimersRef = useRef<number[]>([]);
  const isMock = isMockRequested();

  const append = useCallback((frame: SseFrame): void => {
    setState((prev) => reduceStreamEvent(prev, frame as unknown as BattleEvent));
  }, []);

  const clearMockTimers = (): void => {
    for (const id of mockTimersRef.current) window.clearTimeout(id);
    mockTimersRef.current = [];
  };

  const abort = useCallback((): void => {
    handleRef.current?.abort();
    handleRef.current = null;
    clearMockTimers();
    setIsStreaming(false);
  }, []);

  const reset = useCallback((): void => {
    abort();
    setState(INITIAL_STREAM_STATE);
    setTransportError(null);
  }, [abort]);

  const runMock = useCallback(async (): Promise<void> => {
    // Dev-only fixture replay. The whole branch (including the dynamic import) is eliminated from
    // the production bundle because `import.meta.env.DEV` is statically `false` there.
    if (import.meta.env.DEV) {
      const { LEVEL1_DEV_FIXTURE } = await import("../../dev/fixtures/level1_run");
      let offset = 0;
      const total = LEVEL1_DEV_FIXTURE.length;
      LEVEL1_DEV_FIXTURE.forEach((item, index) => {
        offset += item.t_rel;
        const id = window.setTimeout(() => {
          // Stamp `ts` at dispatch so the elapsed counter behaves as with a live stream.
          append({ ...item.event, ts: new Date().toISOString() } as unknown as SseFrame);
          if (index === total - 1) setIsStreaming(false);
        }, offset);
        mockTimersRef.current.push(id);
      });
    }
  }, [append]);

  const run = useCallback(
    (params: BattleRunParams): void => {
      abort();
      setState(INITIAL_STREAM_STATE);
      setTransportError(null);
      setIsStreaming(true);

      if (import.meta.env.DEV && isMock) {
        void runMock();
        return;
      }

      handleRef.current = openSseStream({
        url: api.battleStreamUrl(),
        method: "POST",
        body: {
          query: params.query,
          question_id: params.questionId ?? null,
          engines: params.engines,
          solicitation_filter: params.solicitationFilter ?? null,
          ...(params.synthesisModel ? { synthesis_model: params.synthesisModel } : {}),
          ...(params.judgeModel ? { judge_model: params.judgeModel } : {}),
        },
        onFrame: append,
        onError: (err) => setTransportError(err.message),
        onEnd: () => setIsStreaming(false),
      });
    },
    [abort, append, isMock, runMock]
  );

  useEffect(() => () => abort(), [abort]);

  // Screen-share safe mode: consumers only ever see the masked view of server data.
  const { mask, maskValue } = useSafeMode();
  const shownState = useMemo(() => maskValue(state), [maskValue, state]);
  const shownError = transportError === null ? null : mask(transportError);
  return { state: shownState, isStreaming, transportError: shownError, isMock, run, abort, reset };
}
