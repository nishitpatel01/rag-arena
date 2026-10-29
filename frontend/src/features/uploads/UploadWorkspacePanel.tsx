import React, { useCallback, useEffect, useState } from "react";
import { Panel } from "../../components/ui/Panel";
import type { LevelDescriptor } from "../../types/arena";
import type { IngestJobStream } from "../ingest/useIngestJobStream";
import { BucketStatusCard } from "./BucketStatusCard";
import { WorkspaceFilesCard, type ActiveFileUpload } from "./WorkspaceFilesCard";
import { WorkspaceIngestCard } from "./WorkspaceIngestCard";
import { uploadApi } from "./uploadApi";
import {
  CORPUS_SCOPE,
  validateUploadPdfCandidate,
  type BucketStatusResponse,
  type UploadCostEstimate,
  type UploadIngestLevel,
} from "./uploadModel";
import { broadcastWorkspacesChanged, useUploadWorkspaces } from "./useUploadWorkspaces";

export interface UploadWorkspacePanelProps {
  readonly demoMode: string | null | undefined;
  readonly levels: readonly LevelDescriptor[];
  readonly job: IngestJobStream;
  readonly onJobStart: () => void;
  readonly onJobFinished: () => void;
}

export const UploadWorkspacePanel: React.FC<UploadWorkspacePanelProps> = ({
  demoMode,
  levels,
  job,
  onJobStart,
  onJobFinished,
}) => {
  const wsState = useUploadWorkspaces(demoMode);
  const [bucket, setBucket] = useState<BucketStatusResponse | null>(null);
  const [bucketLoading, setBucketLoading] = useState(false);
  const [ensuring, setEnsuring] = useState(false);
  const [bucketError, setBucketError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [uploadsInFlight, setUploadsInFlight] = useState<readonly ActiveFileUpload[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingEstimate, setPendingEstimate] = useState<UploadCostEstimate | null>(null);
  const [estimatingLevel, setEstimatingLevel] = useState<string | null>(null);
  const [ingestError, setIngestError] = useState<string | null>(null);

  const selectedWorkspace =
    wsState.activeWorkspace ?? (wsState.workspaces.length > 0 ? wsState.workspaces[0] : null);

  const loadBucket = useCallback(async () => {
    if (!wsState.enabled) return;
    setBucketLoading(true);
    try {
      const status = await uploadApi.bucketStatus();
      setBucket(status);
      setBucketError(null);
    } catch (err) {
      setBucketError(err instanceof Error ? err.message : String(err));
    } finally {
      setBucketLoading(false);
    }
  }, [wsState.enabled]);

  useEffect(() => {
    void loadBucket();
  }, [loadBucket]);

  if (!wsState.enabled) return null;

  const onEnsureBucket = async (): Promise<void> => {
    setEnsuring(true);
    setBucketError(null);
    try {
      const status = await uploadApi.ensureBucket();
      setBucket(status);
    } catch (err) {
      setBucketError(err instanceof Error ? err.message : String(err));
    } finally {
      setEnsuring(false);
    }
  };

  const onCreateWorkspace = async (): Promise<void> => {
    setCreating(true);
    setActionError(null);
    setPendingEstimate(null);
    try {
      const res = await uploadApi.createWorkspace();
      setBucket(res.bucket);
      wsState.upsertWorkspace(res.workspace);
      wsState.setScope(res.workspace.id);
      broadcastWorkspacesChanged(wsState.instanceId);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      void loadBucket();
    } finally {
      setCreating(false);
    }
  };

  const onDeleteWorkspace = async (workspaceId: string): Promise<void> => {
    setDeleting(true);
    setActionError(null);
    setPendingEstimate(null);
    try {
      await uploadApi.deleteWorkspace(workspaceId);
      wsState.removeWorkspace(workspaceId);
      if (wsState.scope === workspaceId) wsState.setScope(CORPUS_SCOPE);
      await wsState.reload();
      broadcastWorkspacesChanged(wsState.instanceId);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  const updateInFlight = (key: string, patch: Partial<ActiveFileUpload>): void => {
    setUploadsInFlight((prev) => prev.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  };

  const onUploadFiles = async (files: readonly File[]): Promise<void> => {
    if (!selectedWorkspace) return;
    setActionError(null);
    setPendingEstimate(null);
    const wsId = selectedWorkspace.id;
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      const key = `${file.name}-${Date.now()}-${i}`;
      const invalid = validateUploadPdfCandidate(file.name, file.size);
      if (invalid) {
        setUploadsInFlight((prev) => [
          ...prev,
          { key, filename: file.name, totalBytes: file.size, committedBytes: 0, phase: "error", error: invalid },
        ]);
        continue;
      }
      setUploadsInFlight((prev) => [
        ...prev,
        { key, filename: file.name, totalBytes: file.size, committedBytes: 0, phase: "uploading", error: null },
      ]);
      try {
        const session = await uploadApi.createFileSession(wsId, file.name, file.size);
        await uploadApi.uploadFileToSession(session.session_uri, file, (sent) =>
          updateInFlight(key, { committedBytes: sent })
        );
        updateInFlight(key, { committedBytes: file.size, phase: "finalizing" });
        const fin = await uploadApi.finalizeFile(wsId, session.file.id);
        await wsState.reload();
        broadcastWorkspacesChanged(wsState.instanceId);
        if (!fin.accepted) {
          updateInFlight(key, { phase: "error", error: fin.error ?? "PDF rejected" });
        } else {
          setUploadsInFlight((prev) => prev.filter((item) => item.key !== key));
        }
      } catch (err) {
        updateInFlight(key, { phase: "error", error: err instanceof Error ? err.message : String(err) });
      }
    }
  };

  const runWorkspaceIngest = async (level: UploadIngestLevel, confirmUsd?: number): Promise<void> => {
    if (!selectedWorkspace) return;
    const wsId = selectedWorkspace.id;
    setIngestError(null);
    setPendingEstimate(null);
    onJobStart();
    await job.startCustom(
      level,
      async () => {
        const out = await uploadApi.startIngest(wsId, level, confirmUsd);
        if (out.needsConfirmation && out.estimate) {
          setPendingEstimate(out.estimate);
          throw new Error(out.error ?? "Cost confirmation required before running Level 3 ingestion.");
        }
        if (!out.job) throw new Error(out.error ?? "Upload ingestion did not return a job.");
        wsState.setScope(wsId);
        return out.job;
      },
      () => {
        void wsState.reload().then(() => broadcastWorkspacesChanged(wsState.instanceId));
        onJobFinished();
      },
      wsId
    );
  };

  const onRequestIngest = async (level: UploadIngestLevel): Promise<void> => {
    if (!selectedWorkspace) return;
    setIngestError(null);
    if (level !== "level3_multimodal") {
      await runWorkspaceIngest(level);
      return;
    }
    setEstimatingLevel(level);
    try {
      const est = await uploadApi.estimateIngest(selectedWorkspace.id, level);
      if (est.requires_confirmation) {
        setPendingEstimate(est);
      } else {
        await runWorkspaceIngest(level, est.max_usd);
      }
    } catch (err) {
      setIngestError(err instanceof Error ? err.message : String(err));
    } finally {
      setEstimatingLevel(null);
    }
  };

  const anyUploading = uploadsInFlight.some((u) => u.phase === "uploading" || u.phase === "finalizing");
  const busy = job.isStreaming || anyUploading || creating || deleting;

  return (
    <Panel
      title="Your own documents (upload workspaces)"
      subtitle="Private Cloud Storage bucket · direct-to-GCS resumable PDF uploads (no app size/page cap) · scoped L1–L5 ingestion"
      bodyClassName="flex flex-col gap-3"
    >
      <BucketStatusCard
        bucket={bucket}
        loading={bucketLoading}
        ensuring={ensuring}
        error={bucketError}
        onEnsure={() => void onEnsureBucket()}
        onRefresh={() => void loadBucket()}
      />
      <WorkspaceFilesCard
        workspaces={wsState.workspaces}
        selectedWorkspace={selectedWorkspace}
        activeScopeId={wsState.scope}
        busy={busy}
        creating={creating}
        deleting={deleting}
        uploadsInFlight={uploadsInFlight}
        actionError={actionError ?? wsState.error}
        onSelectWorkspace={(id) => {
          setPendingEstimate(null);
          wsState.setScope(id);
        }}
        onCreateWorkspace={() => void onCreateWorkspace()}
        onDeleteWorkspace={(id) => void onDeleteWorkspace(id)}
        onUploadFiles={(files) => void onUploadFiles(files)}
      />
      {selectedWorkspace && (
        <WorkspaceIngestCard
          workspace={selectedWorkspace}
          levels={levels}
          busy={busy}
          pendingEstimate={pendingEstimate}
          estimatingLevel={estimatingLevel}
          ingestError={ingestError}
          onRequestIngest={(lvl) => void onRequestIngest(lvl)}
          onConfirmCostIngest={(est) => void runWorkspaceIngest("level3_multimodal", est.max_usd)}
          onCancelEstimate={() => setPendingEstimate(null)}
        />
      )}
    </Panel>
  );
};
