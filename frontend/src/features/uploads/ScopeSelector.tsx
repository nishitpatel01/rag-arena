import React from "react";
import { Link } from "react-router-dom";
import { useSafeMode } from "../../app/SafeMode";
import { Pill } from "../../components/ui/Pill";
import { MASK_UPLOAD_WORKSPACE, maskUploadScopeSummary } from "../../lib/safeMode";
import {
  CORPUS_SCOPE,
  isUploadScope,
  uploadsEnabled,
  type UploadWorkspace,
} from "./uploadModel";

export interface ScopeSelectorProps {
  readonly demoMode: string | null | undefined;
  readonly scope: string;
  readonly workspaces: readonly UploadWorkspace[];
  readonly loading: boolean;
  readonly error: string | null;
  readonly disabled?: boolean;
  readonly onChange: (next: string) => void;
  readonly id?: string;
}

const selectClass =
  "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:opacity-60";

function workspaceLabel(ws: UploadWorkspace, safeMode: boolean): string {
  const ready = ws.files.filter((f) => f.status === "ready");
  if (safeMode) {
    const noun = ready.length === 1 ? "1 ready PDF (masked)" : `${ready.length} ready PDFs (masked)`;
    return `${MASK_UPLOAD_WORKSPACE} · ${noun}`;
  }
  if (ready.length === 0) return `${ws.id} · 0 ready PDFs`;
  const names = ready.slice(0, 2).map((f) => f.name).join(", ");
  const more = ready.length > 2 ? ` +${ready.length - 2}` : "";
  return `${ws.id} · ${names}${more}`;
}

export const ScopeSelector: React.FC<ScopeSelectorProps> = ({
  demoMode,
  scope,
  workspaces,
  loading,
  error,
  disabled = false,
  onChange,
  id = "document-scope-select",
}) => {
  const { enabled: safeMode, mask } = useSafeMode();
  if (!uploadsEnabled(demoMode)) return null;
  const activeWs = isUploadScope(scope) ? workspaces.find((w) => w.id === scope) ?? null : null;
  const readyNames = activeWs
    ? activeWs.files.filter((f) => f.status === "ready").map((f) => f.name)
    : [];

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-line bg-surface-1 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="text-xs font-medium text-ink-2">
          Document scope
        </label>
        <Link to="/ingest" className="text-xs font-medium text-moss underline">
          {workspaces.length === 0 ? "Upload PDFs →" : "Manage uploads →"}
        </Link>
      </div>
      <select
        id={id}
        data-testid="scope-select"
        value={scope}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={selectClass}
      >
        <option value={CORPUS_SCOPE}>Fixed procurement corpus (45 solicitations)</option>
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {workspaceLabel(ws, safeMode)}
          </option>
        ))}
      </select>
      {loading && workspaces.length === 0 && (
        <span className="text-xs text-ink-3">Checking upload workspaces…</span>
      )}
      {error && (
        <span className="text-xs text-clay" role="alert">
          Uploads list unavailable: {mask(error)}
        </span>
      )}
      {isUploadScope(scope) && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-2">
          <Pill tone="warn">no reference answer</Pill>
          <span>
            Ad-hoc questions over{" "}
            <span className="font-mono">{maskUploadScopeSummary(scope, readyNames, safeMode)}</span>.
          </span>
        </div>
      )}
    </div>
  );
};
