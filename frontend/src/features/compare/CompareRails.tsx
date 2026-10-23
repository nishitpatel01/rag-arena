import React from "react";
import { Pill } from "../../components/ui/Pill";
import { formatMs } from "../../lib/format";
import type { EngineTimeline } from "../../types/arena";
import { STAGE_LABELS, STATUS_META, StageRail } from "../levels/StageRail";
import { IDLE_STAGE } from "../levels/model";
import type { LevelChoice, ScoreRow } from "./compareModel";

export interface CompareRailsProps {
  readonly levels: readonly (LevelChoice & { readonly stages: readonly string[] })[];
  readonly timelines: Readonly<Record<string, EngineTimeline>>;
  readonly rows: readonly ScoreRow[];
  /** Selected stage per engine (kept in the page session). */
  readonly picked: Readonly<Record<string, string>>;
  readonly onPick: (engine: string, stageId: string) => void;
}

function mergeOrder(declared: readonly string[], seen: readonly string[]): string[] {
  const out = [...declared];
  for (const s of seen) if (!out.includes(s)) out.push(s);
  return out;
}

const STATUS_PILL: Record<ScoreRow["status"], { tone: "neutral" | "active" | "ok" | "warn" | "err"; text: string }> = {
  pending: { tone: "neutral", text: "waiting for events" },
  running: { tone: "active", text: "running" },
  scored: { tone: "ok", text: "scored" },
  unscored: { tone: "warn", text: "no server Battle Score" },
  failed: { tone: "err", text: "failed" },
};

const StageNote: React.FC<{ readonly tl: EngineTimeline | undefined; readonly stageId: string | null }> = ({ tl, stageId }) => {
  if (!stageId) return <p className="text-xs text-ink-3">Select a stage for its status, service and timing.</p>;
  const st = tl?.stages[stageId] ?? IDLE_STAGE;
  return (
    <p className="break-words text-xs text-ink-2" role="status">
      <span className="font-semibold text-ink-1">{STAGE_LABELS[stageId] ?? stageId}</span> · {STATUS_META[st.status].text}
      {st.service ? ` · ${st.service}` : ""}
      {st.durationMs !== null ? ` · ${formatMs(st.durationMs)}` : ""}
      {st.skipReason ? ` · skipped: ${st.skipReason}` : ""}
      {st.error ? <span className="text-err"> · {st.error.type}: {st.error.message}</span> : null}
    </p>
  );
};

/** Stacked per-level stage rails (ladder order), each driven only by that engine's events. */
export const CompareRails: React.FC<CompareRailsProps> = ({ levels, timelines, rows, picked, onPick }) => {
  return (
    <ol className="flex flex-col gap-4" aria-label="Stage rails per level">
      {levels.map((lvl) => {
        const tl = timelines[lvl.levelId];
        const row = rows.find((r) => r.engine === lvl.levelId);
        const pill = STATUS_PILL[row?.status ?? "pending"];
        const stageIds = tl?.stageOrder.length ? mergeOrder(lvl.stages, tl.stageOrder) : lvl.stages;
        return (
          <li key={lvl.levelId} className="flex flex-col gap-2" data-engine={lvl.levelId} data-status={row?.status ?? "pending"}>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-ink-1">{lvl.short} · {lvl.label}</h3>
              <Pill tone={pill.tone}>{pill.text}</Pill>
              {row?.error?.stage && <Pill tone="err" mono>failed stage: {row.error.stage}</Pill>}
            </div>
            <StageRail label={`${lvl.short} stages`} stageIds={stageIds} stages={tl?.stages ?? {}} selectedStage={picked[lvl.levelId] ?? null}
              onSelectStage={(id) => onPick(lvl.levelId, id)} />
            <StageNote tl={tl} stageId={picked[lvl.levelId] ?? null} />
          </li>
        );
      })}
    </ol>
  );
};
