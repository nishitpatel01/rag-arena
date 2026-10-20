/**
 * Bounded SSE reader (salvaged from v2 `consumeSseResponse`, hardened).
 *
 * - Parses `data: {json}` frames separated by blank lines; `event:` lines are ignored because the
 *   backend puts the type in the JSON `event` field (spec §6.3).
 * - Hard cap of MAX_SSE_FRAMES; on reaching it a single synthetic `stream_warning` is emitted and
 *   the body is cancelled.
 * - Every stream carries an abort timeout (default 180 s).
 */

import type { StreamWarningEvent } from "../types/arena";

export const MAX_SSE_FRAMES = 600;
export const DEFAULT_STREAM_TIMEOUT_MS = 180_000;

export interface SseFrame {
  readonly event: string;
  readonly [key: string]: unknown;
}

export interface SseReaderOptions {
  readonly maxFrames?: number;
  readonly onParseError?: (rawLine: string, err: unknown) => void;
}

function isFrame(value: unknown): value is SseFrame {
  return typeof value === "object" && value !== null && typeof (value as SseFrame).event === "string";
}

function syntheticWarning(limit: number): StreamWarningEvent & SseFrame {
  return {
    event: "stream_warning",
    run_id: null,
    message: `Client SSE frame limit (${limit}) reached; stream truncated by the browser.`,
    ts: new Date().toISOString(),
  };
}

/** Parse one SSE block (lines between blank lines) into a frame, or null if it has no data. */
export function parseSseBlock(block: string): SseFrame | null {
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  const parsed: unknown = JSON.parse(dataLines.join("\n"));
  if (!isFrame(parsed)) {
    throw new Error("SSE frame is not an object with a string `event` field");
  }
  return parsed;
}

export async function readSseStream(
  response: Response,
  onFrame: (frame: SseFrame) => void,
  options: SseReaderOptions = {}
): Promise<void> {
  const limit = options.maxFrames ?? MAX_SSE_FRAMES;
  if (!response.body) {
    throw new Error("SSE response has no body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let frameCount = 0;

  const emitBlock = (block: string): boolean => {
    let frame: SseFrame | null;
    try {
      frame = parseSseBlock(block.replace(/\r/g, ""));
    } catch (err) {
      options.onParseError?.(block, err);
      return true;
    }
    if (!frame) return true;
    frameCount += 1;
    onFrame(frame);
    if (frameCount >= limit) {
      onFrame(syntheticWarning(limit));
      return false;
    }
    return true;
  };

  try {
    // Bounded: exits on `done`, on the frame cap, or on abort (reader.read rejects).
    while (frameCount < limit) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\n\n/);
      buffer = blocks.pop() ?? "";
      let keepGoing = true;
      for (const block of blocks) {
        keepGoing = emitBlock(block);
        if (!keepGoing) break;
      }
      if (!keepGoing) break;
    }
    if (frameCount < limit && buffer.trim().length > 0) {
      emitBlock(buffer);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export interface StreamHandle {
  readonly abort: () => void;
}

export interface OpenStreamParams {
  readonly url: string;
  readonly method?: "GET" | "POST";
  readonly body?: unknown;
  readonly timeoutMs?: number;
  readonly onFrame: (frame: SseFrame) => void;
  readonly onError: (err: Error) => void;
  readonly onEnd: () => void;
}

/** Open a fetch-backed SSE stream with abort timeout; returns a handle to cancel it. */
export function openSseStream(params: OpenStreamParams): StreamHandle {
  const controller = new AbortController();
  const timeoutMs = params.timeoutMs ?? DEFAULT_STREAM_TIMEOUT_MS;
  const timer = window.setTimeout(() => controller.abort(new Error(`SSE timeout after ${timeoutMs} ms`)), timeoutMs);

  void (async () => {
    try {
      const response = await fetch(params.url, {
        method: params.method ?? "POST",
        headers: params.body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: params.body !== undefined ? JSON.stringify(params.body) : undefined,
        signal: controller.signal,
      });
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(`HTTP ${response.status} from ${params.url}${text ? `: ${text.slice(0, 300)}` : ""}`);
      }
      await readSseStream(response, params.onFrame);
    } catch (err) {
      if (controller.signal.aborted && !(controller.signal.reason instanceof Error)) {
        // User-initiated abort: not an error.
      } else {
        params.onError(err instanceof Error ? err : new Error(String(err)));
      }
    } finally {
      window.clearTimeout(timer);
      params.onEnd();
    }
  })();

  return { abort: () => controller.abort() };
}
