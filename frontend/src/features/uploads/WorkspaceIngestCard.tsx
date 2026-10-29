import React from "react";
import { Play } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";
import { Button } from "../../components/ui/Button";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { formatUsd } from "../../lib/format";
import type { LevelDescriptor } from "../../types/arena";
import {
  UPLOAD_INGEST_LEVELS,
  type UploadCostEstimate,
  type UploadIngestLevel,
  type UploadLevelStatus,
  type UploadWorkspace,
} from "./uploadModel";

export interface WorkspaceIngestCardProps {
  readonly workspace: UploadWorkspace;
  readonly levels: readonly LevelDescriptor[];
  readonly busy: boolean;
  readonly pendingEstimate: UploadCostEstimate | null;
  readonly estimatingLevel: string | null;
  readonly ingestError: string | null;
  readonly onRequestIngest: (level: UploadIngestLevel) => void;
  readonly onConfirmCostIngest: (estimate: UploadCostEstimate) => void;
  readonly onCancelEstimate: () => void;
}

function levelStatusPill(entry: UploadLevelStatus | undefined): { tone: PillTone; text: string } {
  if (!entry) return { tone: "neutral", text: "not ingested" };
  if (entry.status === "completed") {
    const unit = entry.tier_profile === "managed_vertex" ? "docs" : "chunks";
    const count = typeof entry.chunks === "number" ? ` · ${entry.chunks} ${unit}` : "";
    return { tone: "ok", text: `completed${count}` };
  }
  if (entry.status === "failed") return { tone: "err", text: "failed" };
  if (entry.status === "starting" || entry.status === "running") return { tone: "active", text: entry.status };
  return { tone: "info", text: entry.status };
}

export const WorkspaceIngestCard: React.FC<WorkspaceIngestCardProps> = ({
  workspace,
  levels,
  busy,
  pendingEstimate,
  estimatingLevel,
  ingestError,
  onRequestIngest,
  onConfirmCostIngest,
  onCancelEstimate,
}) => {
  const { mask } = useSafeMode();
  const readyCount = workspace.files.filter((f) => f.status === "ready").length;
  const readyPages = workspace.files
    .filter((f) => f.status === "ready")
    .reduce((sum, f) => sum + (f.pages ?? 0), 0);
  const l3Done = workspace.levels?.level3_multimodal?.status === "completed";

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line bg-surface-1 p-3" data-testid="workspace-ingest-card">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-ink-1">
          Per-level ingestion for <span className="font-mono">{workspace.id}</span>
        </span>
        <span className="text-xs text-ink-3">
          {readyCount} ready PDF{readyCount === 1 ? "" : "s"} · {readyPages} page{readyPages === 1 ? "" : "s"}
        </span>
      </div>

      {ingestError && (
        <p className="text-xs text-err" role="alert">
          {mask(ingestError)}
        </p>
      )}

      {pendingEstimate && (
        <div
          className="flex flex-col gap-2 rounded-md border border-clay bg-surface-0 p-3 text-xs"
          role="region"
          aria-label="Confirm Level 3 ingestion cost"
          data-testid="level3-cost-confirm"
        >
          <p className="font-semibold text-ink-1">
            Confirm Level 3 (Document AI Layout Parser + Gemini vision) cost upper bound:{" "}
            <span className="font-mono">{formatUsd(pendingEstimate.max_usd)}</span>
          </p>
          <p className="text-ink-2">
            {pendingEstimate.pages} page{pendingEstimate.pages === 1 ? "" : "s"} · Document AI:{" "}
            <span className="font-mono">{formatUsd(pendingEstimate.documentai_usd ?? null)}</span> · Gemini vision (
            <span className="font-mono">{pendingEstimate.vision_model ?? "vision"}</span>) max:{" "}
            <span className="font-mono">{formatUsd(pendingEstimate.vision_max_usd ?? null)}</span>
          </p>
          <p className="text-ink-3">{mask(pendingEstimate.basis)}</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              data-testid="confirm-level3-ingest"
              onClick={() => onConfirmCostIngest(pendingEstimate)}
            >
              Confirm up to {formatUsd(pendingEstimate.max_usd)} & start
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={onCancelEstimate}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      <ul className="grid gap-2 md:grid-cols-2">
        {UPLOAD_INGEST_LEVELS.map((levelId) => {
          const desc = levels.find((l) => l.level_id === levelId);
          const title = desc ? desc.label : levelId;
          const entry = workspace.levels?.[levelId];
          const pill = levelStatusPill(entry);
          const isEstimating = estimatingLevel === levelId;
          return (
            <li key={levelId} className="flex flex-col justify-between gap-2 rounded-md border border-line bg-surface-0 p-2.5 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <span className="font-medium text-ink-1">{title}</span>
                <Pill tone={pill.tone}>{pill.text}</Pill>
              </div>
              {entry?.error && (
                <p className="break-words text-xs text-err" role="alert">
                  {mask(entry.error)}
                </p>
              )}
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-ink-3">{levelId}</span>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy || isEstimating || readyCount === 0}
                  data-testid={`upload-ingest-${levelId}`}
                  onClick={() => onRequestIngest(levelId)}
                  leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}
                >
                  {isEstimating
                    ? "Estimating…"
                    : levelId === "level3_multimodal"
                      ? "Estimate & ingest"
                      : entry?.status === "completed"
                        ? "Re-ingest"
                        : "Ingest"}
                </Button>
              </div>
            </li>
          );
        })}
        <li className="flex flex-col justify-between gap-2 rounded-md border border-line bg-surface-0 p-2.5 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-1.5">
            <span className="font-medium text-ink-1">Level 4: Agentic RAG (Google ADK)</span>
            <Pill tone={l3Done ? "ok" : "neutral"}>{l3Done ? "ready via Level 3" : "needs Level 3"}</Pill>
          </div>
          <p className="text-ink-3">
            No separate ingestion job — Level 4's ADK agent tools query this workspace's Level 3 (<span className="font-mono">multimodal</span>) rows.
          </p>
        </li>
      </ul>
    </div>
  );
};
