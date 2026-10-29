import React, { useRef, useState } from "react";
import { FileUp, FolderPlus, Trash2 } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { formatBytes, type UploadFileEntry, type UploadWorkspace } from "./uploadModel";

export interface ActiveFileUpload {
  readonly key: string;
  readonly filename: string;
  readonly totalBytes: number;
  readonly committedBytes: number;
  readonly phase: "uploading" | "finalizing" | "error";
  readonly error: string | null;
}

export interface WorkspaceFilesCardProps {
  readonly workspaces: readonly UploadWorkspace[];
  readonly selectedWorkspace: UploadWorkspace | null;
  readonly activeScopeId: string;
  readonly busy: boolean;
  readonly creating: boolean;
  readonly deleting: boolean;
  readonly uploadsInFlight: readonly ActiveFileUpload[];
  readonly actionError: string | null;
  readonly onSelectWorkspace: (workspaceId: string) => void;
  readonly onCreateWorkspace: () => void;
  readonly onDeleteWorkspace: (workspaceId: string) => void;
  readonly onUploadFiles: (files: readonly File[]) => void;
}

const selectClass =
  "rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:opacity-60";

function fileTone(status: string): PillTone {
  if (status === "ready") return "ok";
  if (status === "rejected") return "err";
  return "active";
}

const InFlightUploadsList: React.FC<{ readonly items: readonly ActiveFileUpload[] }> = ({ items }) => {
  const { mask } = useSafeMode();
  if (items.length === 0) return null;
  return (
    <ul className="flex flex-col gap-2" aria-label="Uploads in progress">
      {items.map((u) => {
        const pct = u.totalBytes > 0 ? Math.min(100, Math.round((u.committedBytes / u.totalBytes) * 100)) : 0;
        return (
          <li key={u.key} className="rounded-md border border-line bg-surface-0 p-2 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono font-medium text-ink-1">{u.filename}</span>
              <span className="font-mono text-ink-2">
                {u.phase === "finalizing"
                  ? "validating PDF…"
                  : u.phase === "error"
                    ? "failed"
                    : `${formatBytes(u.committedBytes)} / ${formatBytes(u.totalBytes)} (${pct}%)`}
              </span>
            </div>
            {u.phase !== "error" && (
              <div
                role="progressbar"
                aria-label={`Upload progress for ${u.filename}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                className="mt-1.5 h-1.5 w-full overflow-hidden rounded bg-surface-2"
              >
                <div className="h-full bg-moss" style={{ width: `${pct}%` }} />
              </div>
            )}
            {u.error && (
              <p className="mt-1 text-xs text-err" role="alert">
                {mask(u.error)}
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
};

export const WorkspaceFilesCard: React.FC<WorkspaceFilesCardProps> = ({
  workspaces,
  selectedWorkspace,
  activeScopeId,
  busy,
  creating,
  deleting,
  uploadsInFlight,
  actionError,
  onSelectWorkspace,
  onCreateWorkspace,
  onDeleteWorkspace,
  onUploadFiles,
}) => {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const { mask } = useSafeMode();

  const onFilePick = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const picked = e.target.files ? Array.from(e.target.files) : [];
    e.target.value = "";
    if (picked.length > 0) onUploadFiles(picked);
  };

  return (
    <div className="flex flex-col gap-3 rounded-md border border-line bg-surface-1 p-3" data-testid="workspace-files-card">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <label htmlFor="ingest-workspace-select" className="flex flex-col gap-1 text-xs font-medium text-ink-2">
            Upload workspace
            <select
              id="ingest-workspace-select"
              data-testid="ingest-workspace-select"
              className={selectClass}
              value={selectedWorkspace?.id ?? ""}
              disabled={busy || workspaces.length === 0}
              onChange={(e) => {
                setConfirmDelete(false);
                onSelectWorkspace(e.target.value);
              }}
            >
              {workspaces.length === 0 ? (
                <option value="">— no workspaces yet —</option>
              ) : (
                workspaces.map((ws) => (
                  <option key={ws.id} value={ws.id}>
                    {ws.id} ({ws.files.filter((f) => f.status === "ready").length} ready PDFs)
                  </option>
                ))
              )}
            </select>
          </label>
          <Button
            size="sm"
            variant="primary"
            disabled={busy || creating}
            onClick={() => {
              setConfirmDelete(false);
              onCreateWorkspace();
            }}
            data-testid="create-workspace-btn"
            leadingIcon={<FolderPlus className="h-3.5 w-3.5" aria-hidden />}
          >
            {creating ? "Creating…" : "New workspace"}
          </Button>
          {selectedWorkspace && activeScopeId === selectedWorkspace.id && (
            <Pill tone="ok">active in Levels & Compare</Pill>
          )}
        </div>

        {selectedWorkspace && (
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={fileInputRef}
              id="upload-pdf-input"
              data-testid="upload-pdf-input"
              type="file"
              accept=".pdf,application/pdf"
              multiple
              disabled={busy}
              onChange={onFilePick}
              className="sr-only"
            />
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              onClick={() => fileInputRef.current?.click()}
              data-testid="upload-pdf-trigger"
              leadingIcon={<FileUp className="h-3.5 w-3.5" aria-hidden />}
            >
              Upload PDFs
            </Button>
            {!confirmDelete && (
              <Button
                size="sm"
                variant="danger"
                disabled={busy || deleting}
                onClick={() => setConfirmDelete(true)}
                data-testid="delete-workspace-btn"
                leadingIcon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
              >
                Delete workspace
              </Button>
            )}
          </div>
        )}
      </div>

      {selectedWorkspace && confirmDelete && (
        <div
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-err bg-surface-0 p-2.5 text-xs"
          role="region"
          aria-label="Confirm workspace deletion"
        >
          <span className="text-ink-1">
            Permanently delete <span className="font-mono font-semibold">{selectedWorkspace.id}</span> (BigQuery rows, Vertex AI Search docs, and bucket objects)?
          </span>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="danger"
              disabled={deleting}
              data-testid="confirm-delete-workspace"
              onClick={() => {
                setConfirmDelete(false);
                onDeleteWorkspace(selectedWorkspace.id);
              }}
            >
              {deleting ? "Deleting…" : "Confirm delete"}
            </Button>
            <Button size="sm" variant="ghost" disabled={deleting} onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {actionError && (
        <p className="text-xs text-err" role="alert">
          {mask(actionError)}
        </p>
      )}

      <InFlightUploadsList items={uploadsInFlight} />

      {selectedWorkspace ? (
        <DataTable<UploadFileEntry>
          caption={`Files in workspace ${selectedWorkspace.id}`}
          rows={selectedWorkspace.files}
          rowKey={(f) => f.id}
          emptyText="No PDFs uploaded yet. Click Upload PDFs (direct-to-GCS resumable transfer; no app size or page cap)."
          columns={[
            { key: "name", header: "Document", render: (f) => <span className="font-medium">{f.name}</span>, mono: true },
            { key: "status", header: "Status", render: (f) => <Pill tone={fileTone(f.status)}>{f.status}</Pill> },
            { key: "pages", header: "Pages", render: (f) => f.pages ?? "—", align: "right", mono: true },
            {
              key: "size",
              header: "Size",
              render: (f) => formatBytes(f.size_bytes ?? f.declared_size_bytes),
              align: "right",
              mono: true,
            },
            {
              key: "note",
              header: "Validation",
              render: (f) =>
                f.error ? (
                  <span className="text-err">{f.error}</span>
                ) : f.sha256 ? (
                  <span className="font-mono text-ink-3" title={f.sha256}>
                    sha256:{f.sha256.slice(0, 12)}…
                  </span>
                ) : (
                  "—"
                ),
            },
          ]}
        />
      ) : (
        <p className="text-xs text-ink-3">
          Create a workspace to upload your own PDFs directly to your private Cloud Storage bucket. Uploads remain until you delete the workspace.
        </p>
      )}
    </div>
  );
};
