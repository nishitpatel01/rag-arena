import React from "react";
import type { JsonObject, StageState } from "../../types/arena";
import { CodeBlock, stringifyJson } from "../../components/ui/CodeBlock";
import { EmptyState } from "../../components/ui/EmptyState";
import { KeyValue, formatValue } from "../../components/ui/KeyValue";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import { STAGE_LABELS, STATUS_META } from "./StageRail";
import { DEDICATED_KEYS, dedicatedStageView } from "./stageViews";

const CODE_KEYS = new Set(["sql", "api_request", "prompt", "answer"]);

const TONE: Record<StageState["status"], PillTone> = {
  idle: "neutral",
  active: "active",
  done: "ok",
  failed: "err",
  skipped: "locked",
};

/** Splits an object into scalar key/values (for KeyValue) and code-ish keys (for CodeBlock). */
export function splitPayload(obj: JsonObject | null, omitKeys: readonly string[] = []): {
  readonly scalars: ReadonlyArray<readonly [string, string]>;
  readonly code: ReadonlyArray<readonly [string, string]>;
} {
  if (!obj) return { scalars: [], code: [] };
  const scalars: Array<readonly [string, string]> = [];
  const code: Array<readonly [string, string]> = [];
  for (const [k, v] of Object.entries(obj)) {
    if (omitKeys.includes(k)) continue;
    if (CODE_KEYS.has(k) && typeof v === "string") code.push([k, v]);
    else if (v !== null && typeof v === "object") code.push([k, stringifyJson(v)]);
    else scalars.push([k, formatValue(v)]);
  }
  return { scalars, code };
}

export interface StageDetailProps {
  readonly stageId: string | null;
  readonly state: StageState | null;
  /** Retrieve payload of the same run (rerank rows join their Level 3 element metadata from it). */
  readonly retrievePayload?: JsonObject | null;
}

export const StageDetail: React.FC<StageDetailProps> = ({ stageId, state, retrievePayload = null }) => {
  if (!stageId || !state) {
    return <EmptyState compact title="Select a stage" description="Click a node on the rail or the diagram to see the exact request and response it recorded." />;
  }
  const meta = STATUS_META[state.status];
  const req = splitPayload(state.request);
  // Dedicated renderers (query_prep / hybrid retrieve / rerank / Level 3 synthesize) show their tables above; the same
  // keys are left out of the generic code blocks but remain in the raw payload JSON below.
  const dedicated = dedicatedStageView(stageId, state.payload, { retrievePayload });
  const res = splitPayload(state.payload, dedicated ? DEDICATED_KEYS[stageId] ?? [] : []);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink-1">{state.label ?? STAGE_LABELS[stageId] ?? stageId}</h3>
        <Pill tone={TONE[state.status]}>{meta.text}</Pill>
        {state.service && <Pill tone="info" mono>{state.service}</Pill>}
        {state.durationMs !== null && (
          <Pill tone="neutral" mono title="Backend-measured duration">
            {formatMs(state.durationMs)}
          </Pill>
        )}
      </div>

      {state.status === "idle" && <p className="text-xs text-ink-3">This stage has not started in the current run.</p>}
      {state.skipReason && <p className="text-xs text-ink-2">Skipped: {state.skipReason}</p>}
      {state.error && (
        <div role="alert" className="rounded-md border border-err px-3 py-2 text-xs">
          <span className="font-mono font-semibold text-err">{state.error.type}</span>
          <p className="mt-0.5 break-words text-ink-1">{state.error.message}</p>
        </div>
      )}

      {dedicated}

      <div className="grid gap-3 xl:grid-cols-2">
        <section aria-label="Request sent" className="flex min-w-0 flex-col gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-3">Request sent</h4>
          {req.scalars.length === 0 && req.code.length === 0 && <p className="text-xs text-ink-3">—</p>}
          {req.scalars.length > 0 && <KeyValue items={req.scalars} />}
          {req.code.map(([k, v]) => (
            <CodeBlock key={k} label={k} language={k === "sql" ? "sql" : "json"} code={v} />
          ))}
        </section>
        <section aria-label="Response summary" className="flex min-w-0 flex-col gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-3">Response summary</h4>
          {res.scalars.length === 0 && res.code.length === 0 && <p className="text-xs text-ink-3">—</p>}
          {res.scalars.length > 0 && <KeyValue items={res.scalars} />}
          {res.code.map(([k, v]) => (
            <CodeBlock key={k} label={k} language={k === "sql" ? "sql" : "json"} code={v} />
          ))}
          {dedicated && state.payload && (
            <details className="rounded-md border border-line bg-surface-0 p-2 text-xs">
              <summary className="cursor-pointer font-medium text-ink-2">Raw payload JSON (exact stage_completed.payload)</summary>
              <div className="mt-2">
                <CodeBlock label="payload" language="json" code={stringifyJson(state.payload)} />
              </div>
            </details>
          )}
        </section>
      </div>
    </div>
  );
};
