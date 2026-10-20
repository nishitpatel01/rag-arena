import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import { openSseStream, type SseFrame, type StreamHandle } from "../../lib/sse";
import type { IngestEvent } from "../../types/arena";
import { INITIAL_STREAM_STATE, reduceStreamEvent, type StreamState } from "../levels/model";

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

  const abort = useCallback((): void => {
    handleRef.current?.abort();
    handleRef.current = null;
    setIsStreaming(false);
  }, []);

  const start = useCallback(
    async (nextLevel: string): Promise<void> => {
      abort();
      setState(INITIAL_STREAM_STATE);
      setTransportError(null);
      setLevel(nextLevel);
      setIsStreaming(true);
      let created: { job_id: string };
      try {
        created = await api.createIngestJob(nextLevel);
      } catch (err) {
        setTransportError(err instanceof Error ? err.message : String(err));
        setIsStreaming(false);
        return;
      }
      setJobId(created.job_id);
      handleRef.current = openSseStream({
        url: api.ingestJobStreamUrl(created.job_id),
        method: "GET",
        timeoutMs: INGEST_STREAM_TIMEOUT_MS,
        onFrame: (frame: SseFrame) => setState((prev) => reduceStreamEvent(prev, frame as unknown as IngestEvent)),
        onError: (err) => setTransportError(err.message),
        onEnd: () => setIsStreaming(false),
      });
    },
    [abort]
  );

  useEffect(() => () => abort(), [abort]);

  return { state, jobId, level, isStreaming, transportError, start, abort };
}
