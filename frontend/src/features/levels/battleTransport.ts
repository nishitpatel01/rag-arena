import { api } from "../../lib/api";
import { openSseStream, type SseFrame } from "../../lib/sse";
import type { OpenTransport, TransportHandle } from "./battleSession";

export function isMockRequested(): boolean {
  return import.meta.env.DEV && typeof window !== "undefined" && window.location.search.includes("mock=1");
}

/** Real transport: one `POST /battle/stream` SSE request. */
export const sseTransport: OpenTransport = (params, cb) =>
  openSseStream({
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
    onFrame: (frame: SseFrame) => cb.onFrame(frame),
    onError: cb.onError,
    onEnd: cb.onEnd,
  });

/**
 * Dev-only fixture replay (`?mock=1`) at each item's own `t_rel` pacing. The whole branch (including the
 * dynamic imports) is eliminated from the production bundle because `import.meta.env.DEV` is statically false.
 */
export const mockTransport: OpenTransport = (params, cb) => {
  const timers: number[] = [];
  let cancelled = false;
  const handle: TransportHandle = {
    abort: () => {
      cancelled = true;
      for (const id of timers) window.clearTimeout(id);
      timers.length = 0;
    },
  };
  if (!import.meta.env.DEV) {
    cb.onError(new Error("mock transport is DEV-only"));
    cb.onEnd();
    return handle;
  }
  void (async () => {
    const engines = params.engines;
    const search = window.location.search;
    const fixture = engines.includes("managed_vertex")
      ? (await import("../../dev/fixtures/level5_run")).level5DevFixture(search.includes("mock_mode=model") ? "model" : "summary")
      : engines.includes("level4_agentic")
      ? (await import("../../dev/fixtures/level4_run")).LEVEL4_DEV_FIXTURE
      : engines.includes("level3_multimodal")
      ? (await import("../../dev/fixtures/level3_run")).LEVEL3_DEV_FIXTURE
      : engines.includes("level2_hybrid")
        ? (await import("../../dev/fixtures/level2_run")).level2DevFixture(search.includes("mock_fail=rerank"))
        : (await import("../../dev/fixtures/level1_run")).LEVEL1_DEV_FIXTURE;
    if (cancelled) return;
    let offset = 0;
    const total = fixture.length;
    fixture.forEach((item, index) => {
      offset += item.t_rel;
      const id = window.setTimeout(() => {
        // Stamp `ts` at dispatch so the elapsed counter behaves as with a live stream.
        cb.onFrame({ ...item.event, ts: new Date().toISOString() });
        if (index === total - 1) cb.onEnd();
      }, offset);
      timers.push(id);
    });
  })();
  return handle;
};
