import React from "react";
import { AlertTriangle, Check, Circle, CircleDashed, Loader } from "lucide-react";
import type { StageState, StageStatus } from "../../types/arena";
import { formatMs } from "../../lib/format";
import { IDLE_STAGE } from "./model";
import { useElapsedMs } from "./useElapsedMs";

export const STAGE_LABELS: Record<string, string> = {
  query_prep: "Prep",
  embed_query: "Embed",
  retrieve: "Retrieve",
  rerank: "Rerank",
  agent_turn: "Agent",
  synthesize: "Synthesize",
  evaluate: "Evaluate",
  discover_docs: "Discover",
  parse: "Parse",
  chunk: "Chunk",
  embed: "Embed",
  load_bigquery: "Load BQ",
  build_indexes: "Indexes",
  verify_counts: "Verify",
  upload_gcs: "Upload GCS",
  import_documents: "Import",
};

export const STATUS_META: Record<
  StageStatus,
  { readonly text: string; readonly Icon: React.ComponentType<{ className?: string }>; readonly cls: string }
> = {
  idle: { text: "idle", Icon: Circle, cls: "border-line text-ink-3" },
  active: { text: "running", Icon: Loader, cls: "border-moss text-moss" },
  done: { text: "done", Icon: Check, cls: "border-ok text-ok" },
  failed: { text: "failed", Icon: AlertTriangle, cls: "border-err text-err" },
  skipped: { text: "skipped", Icon: CircleDashed, cls: "border-dashed border-line text-ink-3" },
};

interface StageNodeProps {
  readonly stageId: string;
  readonly state: StageState;
  readonly selected: boolean;
  readonly onSelect: (stageId: string) => void;
}

const StageNode: React.FC<StageNodeProps> = ({ stageId, state, selected, onSelect }) => {
  const meta = STATUS_META[state.status];
  const elapsed = useElapsedMs(state.startedAt, state.status === "active");
  const timing =
    state.status === "active"
      ? `${formatMs(elapsed)} …`
      : state.durationMs !== null
        ? formatMs(state.durationMs)
        : state.status === "skipped"
          ? "skipped"
          : "—";
  return (
    <li className="flex min-w-0">
      <button
        type="button"
        aria-pressed={selected}
        aria-label={`${STAGE_LABELS[stageId] ?? stageId} stage: ${meta.text}`}
        onClick={() => onSelect(stageId)}
        className={`flex w-full min-w-0 flex-col items-start gap-0.5 rounded-md border bg-surface-1 px-2.5 py-1.5 text-left transition-colors hover:bg-surface-2 ${meta.cls} ${
          selected ? "ring-2 ring-moss ring-offset-1 ring-offset-surface-0" : ""
        } ${state.status === "active" ? "stage-active-outline" : ""}`}
      >
        <span className="flex items-center gap-1.5 text-xs font-semibold text-ink-1">
          <meta.Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {STAGE_LABELS[stageId] ?? stageId}
        </span>
        <span className="font-mono text-xs tabular-nums">{timing}</span>
        <span className="w-full truncate text-xs text-ink-3" title={state.service ?? undefined}>
          {state.service ?? meta.text}
        </span>
      </button>
    </li>
  );
};

export interface StageRailProps {
  readonly stageIds: readonly string[];
  readonly stages: Readonly<Record<string, StageState>>;
  readonly selectedStage: string | null;
  readonly onSelectStage: (stageId: string) => void;
  readonly label?: string;
}

/**
 * Horizontal stage nodes: equal-width columns that wrap only when the panel is too narrow
 * (`auto-fit` grid). Status/time come only from the reduced event timeline.
 */
export const StageRail: React.FC<StageRailProps> = ({ stageIds, stages, selectedStage, onSelectStage, label }) => (
  <ol
    aria-label={label ?? "Pipeline stages"}
    className="grid gap-2"
    style={{ gridTemplateColumns: "repeat(auto-fit, minmax(7.5rem, 1fr))" }}
  >
    {stageIds.map((id) => (
      <StageNode key={id} stageId={id} state={stages[id] ?? IDLE_STAGE} selected={selectedStage === id} onSelect={onSelectStage} />
    ))}
  </ol>
);
