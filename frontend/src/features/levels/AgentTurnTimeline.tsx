import React, { useMemo, useState } from "react";
import {
  Bot,
  Calculator,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  FileClock,
  Loader,
  Search,
  ShieldCheck,
  Wrench,
  XCircle,
  AlertTriangle,
  RotateCcw,
} from "lucide-react";
import type { AgentTurnRecord, JsonObject, StageStatus } from "../../types/arena";
import { maskNode, useSafeMode } from "../../app/SafeMode";
import { CodeBlock, stringifyJson } from "../../components/ui/CodeBlock";
import { EmptyState } from "../../components/ui/EmptyState";
import { Pill } from "../../components/ui/Pill";
import { formatMs, parseTs } from "../../lib/format";
import {
  CLAIM_DIFF_LABEL,
  argsPreview,
  claimsDiff,
  finalCheckLine,
  formatTokens,
  groupAgentTurns,
  headerLine,
  parseAgentStagePayload,
  pricingView,
  timelineHeader,
  toolRowState,
  toolStyle,
  verifyResultClaims,
  type Claim,
  type ClaimDiffRow,
  type ModelRow,
  type ToolBinding,
  type ToolRow,
  type ToolRowState,
  type ToolStyle,
  rowsForBinding,
} from "./agentTimelineModel";
import { useElapsedMs } from "./useElapsedMs";

const TOOL_ICON: Readonly<Record<ToolStyle["icon"], React.ComponentType<{ className?: string }>>> = {
  search: Search,
  amendments: FileClock,
  calculator: Calculator,
  verify: ShieldCheck,
  tool: Wrench,
};

const TONE_CLASS: Readonly<Record<ToolStyle["tone"], string>> = {
  slate: "border-slate text-slate",
  clay: "border-clay text-clay",
  ochre: "border-ochre text-ochre",
  moss: "border-moss text-moss",
  neutral: "border-line text-ink-2",
};

/** Per-tool colored-token badge: icon + text label (never colour alone). */
export const ToolBadge: React.FC<{ readonly name: string }> = ({ name }) => {
  const style = toolStyle(name);
  const Icon = TOOL_ICON[style.icon];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${TONE_CLASS[style.tone]}`} title={name}>
      <Icon className="h-3 w-3 shrink-0" aria-hidden />
      <span className="font-mono">{name}</span>
    </span>
  );
};

const STATE_META: Readonly<Record<ToolRowState, { readonly text: string; readonly cls: string; readonly Icon: React.ComponentType<{ className?: string }> }>> = {
  running: { text: "running", cls: "text-moss", Icon: Loader },
  done: { text: "result", cls: "text-ok", Icon: CheckCircle2 },
  error: { text: "tool error", cls: "text-err", Icon: XCircle },
  no_result: { text: "no result reported", cls: "text-clay", Icon: AlertTriangle },
  failed: { text: "failed (stage failed)", cls: "text-err", Icon: XCircle },
};

const RepairPill: React.FC = () => (
  <Pill tone="warn" icon={<RotateCcw className="h-3 w-3 shrink-0" aria-hidden />}>
    repair
  </Pill>
);

/** Masked text node (server strings rendered outside Pill/CodeBlock primitives). */
const Masked: React.FC<{ readonly children: string }> = ({ children }) => {
  const { mask } = useSafeMode();
  return <>{maskNode(children, mask)}</>;
};

// ---------------------------------------------------------------------------
// Claims
// ---------------------------------------------------------------------------

const DIFF_META: Readonly<Record<ClaimDiffRow["status"], { readonly cls: string; readonly Icon: React.ComponentType<{ className?: string }> }>> = {
  resolved: { cls: "text-ok", Icon: CheckCircle2 },
  still_unsupported: { cls: "text-err", Icon: XCircle },
  new_unsupported: { cls: "text-clay", Icon: AlertTriangle },
};

export const ClaimsDiffList: React.FC<{ readonly before: readonly Claim[]; readonly after: readonly Claim[] }> = ({ before, after }) => {
  const rows = claimsDiff(before, after);
  return (
    <div className="flex flex-col gap-1" data-testid="claims-diff">
      <p className="text-xs text-ink-2">
        Unsupported claims: <span className="font-mono">{before.filter((c) => c.status !== "supported").length}</span> before repair →{" "}
        <span className="font-mono">{after.filter((c) => c.status !== "supported").length}</span> after
      </p>
      {rows.length === 0 ? (
        <p className="text-xs text-ink-3">verify_citations reported no unsupported claims.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.map((r, i) => {
            const meta = DIFF_META[r.status];
            return (
              <li key={`${i}-${r.text}`} className="flex items-start gap-1.5 text-xs text-ink-1">
                <meta.Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${meta.cls}`} aria-hidden />
                <span className={`shrink-0 font-medium ${meta.cls}`}>{CLAIM_DIFF_LABEL[r.status]}:</span>
                <span className="break-words"><Masked>{r.text}</Masked></span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};

const ClaimList: React.FC<{ readonly claims: readonly Claim[]; readonly title: string }> = ({ claims, title }) => (
  <div className="flex flex-col gap-1">
    <p className="text-xs font-semibold text-ink-2">{title}</p>
    {claims.length === 0 ? (
      <p className="text-xs text-ink-3">none</p>
    ) : (
      <ul className="flex flex-col gap-1">
        {claims.map((c, i) => {
          const Icon = c.status === "supported" ? CheckCircle2 : c.status === "unsupported" ? XCircle : CircleHelp;
          const cls = c.status === "supported" ? "text-ok" : c.status === "unsupported" ? "text-err" : "text-ink-3";
          return (
            <li key={`${i}-${c.text}`} className="flex items-start gap-1.5 text-xs text-ink-1">
              <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${cls}`} aria-hidden />
              <span className={`shrink-0 font-medium ${cls}`}>{c.status}:</span>
              <span className="break-words">
                <Masked>{c.text}</Masked>
                {c.reason ? <span className="text-ink-3"> — <Masked>{c.reason}</Masked></span> : null}
              </span>
            </li>
          );
        })}
      </ul>
    )}
  </div>
);

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

const ModelTurnRow: React.FC<{ readonly row: ModelRow }> = ({ row }) => {
  const t = row.turn;
  const tokens = formatTokens(t.tokens);
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface-0 px-3 py-1.5" data-row-kind="model">
      <span className="font-mono text-xs text-ink-3">#{t.turnIndex ?? "?"}</span>
      <Bot className="h-3.5 w-3.5 shrink-0 text-ink-2" aria-hidden />
      <span className="text-xs font-medium text-ink-1">model turn</span>
      {t.repair && <RepairPill />}
      {tokens && <span className="font-mono text-xs text-ink-2">{tokens}</span>}
      {t.text && <span className="min-w-0 flex-1 truncate text-xs text-ink-3"><Masked>{t.text}</Masked></span>}
      <span className="ml-auto font-mono text-xs tabular-nums text-ink-2">{formatMs(t.durationMs)}</span>
    </li>
  );
};

interface ToolCallRowProps {
  readonly row: ToolRow;
  readonly stageStatus: StageStatus;
  readonly defaultOpen: boolean;
  readonly compact: boolean;
  /** Before/after claims from the stage payload, shown on the matching verify_citations row. */
  readonly claimsBefore: readonly Claim[] | null;
  readonly claimsAfter: readonly Claim[] | null;
  /** Counts from the stage payload (shown when the claim texts were not sent). */
  readonly claimCounts: { readonly before: number | null; readonly after: number | null } | null;
  readonly verifyPosition: "first" | "last" | "only" | "middle" | null;
}

const ToolCallRow: React.FC<ToolCallRowProps> = ({ row, stageStatus, defaultOpen, compact, claimsBefore, claimsAfter, claimCounts, verifyPosition }) => {
  const [open, setOpen] = useState(defaultOpen);
  const state = toolRowState(row, stageStatus);
  const meta = STATE_META[state];
  const elapsed = useElapsedMs(parseTs(row.call?.ts), state === "running");
  const turnIndex = row.call?.turnIndex ?? row.result?.turnIndex ?? null;
  const args: JsonObject | null = row.call?.toolArgs ?? null;
  const argsText: string | null = row.call?.toolArgsText ?? null;
  const pricing = row.toolName === "calculate_contract_pricing" ? pricingView(row) : null;
  const ownClaims = row.toolName === "verify_citations" ? verifyResultClaims(row) : null;
  const Chevron = open ? ChevronDown : ChevronRight;
  const durationText = state === "running" ? `${formatMs(elapsed)} …` : formatMs(row.result?.durationMs ?? null);

  const showBefore = claimsBefore && (verifyPosition === "first" || verifyPosition === "only");
  const isLastVerify = verifyPosition === "last" || verifyPosition === "only";
  const showDiff = claimsBefore && claimsAfter && isLastVerify;
  const showCounts = !showDiff && isLastVerify && claimCounts !== null && (claimCounts.before !== null || claimCounts.after !== null);

  return (
    <li className={`rounded-md border bg-surface-1 ${state === "running" ? "border-moss" : "border-line"}`} data-row-kind="tool" data-tool={row.toolName} data-state={state}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-surface-2"
      >
        <Chevron className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        <span className="font-mono text-xs text-ink-3">#{turnIndex ?? "?"}</span>
        <ToolBadge name={row.toolName} />
        {row.repair && <RepairPill />}
        {!compact && (args || argsText) && <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-2"><Masked>{argsPreview(args, 90, argsText)}</Masked></span>}
        <span className={`ml-auto inline-flex items-center gap-1 text-xs ${meta.cls}`}>
          <meta.Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {meta.text}
        </span>
        <span className="font-mono text-xs tabular-nums text-ink-2" title={state === "running" ? "Elapsed since tool_call (client tick)" : "Backend-measured tool duration"}>
          {durationText}
        </span>
      </button>
      {(row.result?.resultSummary || pricing) && (
        <div className="flex flex-col gap-1.5 border-t border-line px-3 py-2">
          {pricing && (
            <pre
              className="overflow-x-auto whitespace-pre-wrap break-words rounded border border-line bg-surface-0 px-2 py-1 font-mono text-xs text-ink-1"
              aria-label={`calculate_contract_pricing: ${pricing.line}`}
              data-testid="pricing-line"
            >
              <code><Masked>{pricing.line}</Masked></code>
            </pre>
          )}
          {row.result?.resultSummary && (!pricing || open) && (
            <p className={`whitespace-pre-wrap break-words text-xs text-ink-1 ${open ? "" : "line-clamp-2"}`}>
              <span className="mr-1 font-semibold text-ink-3">result:</span>
              <Masked>{row.result.resultSummary}</Masked>
            </p>
          )}
        </div>
      )}
      {row.toolName === "verify_citations" && (showBefore || showDiff || showCounts || ownClaims) && (
        <div className="flex flex-col gap-2 border-t border-line px-3 py-2" data-testid="verify-claims">
          {ownClaims && <ClaimList claims={ownClaims} title="Claims checked by this call" />}
          {!ownClaims && showBefore && !showDiff && claimsBefore && <ClaimList claims={claimsBefore} title="Unsupported claims before repair (stage payload)" />}
          {showDiff && claimsBefore && claimsAfter && <ClaimsDiffList before={claimsBefore} after={claimsAfter} />}
          {showCounts && claimCounts && (
            <p className="text-xs text-ink-2" data-testid="claims-counts">
              Unsupported claims (stage payload): <span className="font-mono">{claimCounts.before ?? "—"}</span> on the first verify_citations call →{" "}
              <span className="font-mono">{claimCounts.after ?? "—"}</span> on the last
            </p>
          )}
        </div>
      )}
      {open && (
        <div className={`grid gap-2 border-t border-line p-3 ${compact ? "" : "xl:grid-cols-2"}`}>
          <CodeBlock label="tool_args" language="json" code={args ? stringifyJson(args) : argsText ?? (row.call ? "— (no tool_args)" : "— (no tool_call event)")} maxHeightClass="max-h-48" />
          <CodeBlock
            label={row.result?.result ? "tool_result" : "tool_result_summary"}
            language="json"
            code={row.result?.result ? stringifyJson(row.result.result) : row.result?.resultSummary ?? (state === "running" ? "… waiting for tool_result" : "— (no tool_result event)")}
            maxHeightClass="max-h-48"
          />
        </div>
      )}
    </li>
  );
};

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------

export interface AgentTurnTimelineProps {
  readonly turns: readonly AgentTurnRecord[];
  /** Status of the `agent_turn` stage (running rows tick only while it is active). */
  readonly stageStatus: StageStatus;
  /** `agent_turn` stage_completed payload (counts, unsupported claims) once available. */
  readonly payload?: JsonObject | null;
  /** Narrow layout for the diagram's node-details pane. */
  readonly compact?: boolean;
  /** Restrict to the tools of one diagram node. */
  readonly binding?: ToolBinding;
  /** Expand calculator / verify rows initially. */
  readonly expandKeyRows?: boolean;
  /** Tailwind max-height class for a scrollable row list (header stays visible), e.g. "max-h-[34rem]". */
  readonly maxHeightClass?: string;
}

/** Level 4: `agent_turn` events as an expanding timeline — one row per model turn or tool call (arrival order). */
export const AgentTurnTimeline: React.FC<AgentTurnTimelineProps> = ({ turns, stageStatus, payload = null, compact = false, binding, expandKeyRows = false, maxHeightClass }) => {
  const allRows = useMemo(() => groupAgentTurns(turns), [turns]);
  const rows = useMemo(() => (binding ? rowsForBinding(allRows, binding) : allRows), [allRows, binding]);
  const stagePayload = useMemo(() => parseAgentStagePayload(payload), [payload]);
  const header = timelineHeader(allRows, stagePayload);
  const verifyKeys = allRows.filter((r): r is ToolRow => r.type === "tool" && r.toolName === "verify_citations").map((r) => r.key);
  const verifyPosition = (key: string): ToolCallRowProps["verifyPosition"] => {
    const i = verifyKeys.indexOf(key);
    if (i === -1) return null;
    if (verifyKeys.length === 1) return "only";
    return i === 0 ? "first" : i === verifyKeys.length - 1 ? "last" : "middle";
  };

  if (rows.length === 0) {
    return (
      <EmptyState
        compact
        title={stageStatus === "active" ? "Waiting for the first agent event…" : binding ? "No calls to this tool" : "No agent turns"}
        description="Each ADK model turn, tool call and tool result is appended here the moment the backend reports it."
      />
    );
  }
  return (
    <section aria-label="Agent tool-call timeline" className="flex min-w-0 flex-col gap-2" data-testid="agent-timeline">
      {!binding && (
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-mono text-xs font-semibold text-ink-1" data-testid="agent-timeline-header" title={header.source === "payload" ? "Counts from the agent_turn stage payload" : "Counted from agent_turn events so far"}>
            {headerLine(header)}
          </p>
          {finalCheckLine(stagePayload) && (
            <Pill tone={stagePayload?.finalCheck?.unsupported === 0 ? "ok" : "warn"} mono title="agent_turn payload final_answer_check">
              {finalCheckLine(stagePayload)}
            </Pill>
          )}
          {stagePayload?.toolCallsByName && !compact && (
            <span className="flex flex-wrap items-center gap-1">
              {[...stagePayload.toolCallsByName.entries()].map(([name, n]) => (
                <span key={name} className="inline-flex items-center gap-1">
                  <ToolBadge name={name} />
                  <span className="font-mono text-xs text-ink-2">×{n}</span>
                </span>
              ))}
            </span>
          )}
        </div>
      )}
      <ol
        className={`flex flex-col gap-1.5 ${maxHeightClass ? `${maxHeightClass} overflow-y-auto pr-1` : ""}`}
        data-testid="agent-timeline-rows"
        aria-label={`${rows.length} timeline rows`}
        tabIndex={maxHeightClass ? 0 : undefined}
      >
        {rows.map((row) =>
          row.type === "model" ? (
            <ModelTurnRow key={row.key} row={row} />
          ) : (
            <ToolCallRow
              key={row.key}
              row={row}
              stageStatus={stageStatus}
              compact={compact}
              defaultOpen={expandKeyRows && (row.toolName === "calculate_contract_pricing" || row.toolName === "verify_citations")}
              claimsBefore={stagePayload?.before ?? null}
              claimsAfter={stagePayload?.after ?? null}
              claimCounts={stagePayload ? { before: stagePayload.beforeCount, after: stagePayload.afterCount } : null}
              verifyPosition={verifyPosition(row.key)}
            />
          )
        )}
      </ol>
    </section>
  );
};
