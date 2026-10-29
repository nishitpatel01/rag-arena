import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useSafeMode } from "../../app/SafeMode";
import { uploadApi } from "./uploadApi";
import {
  CORPUS_SCOPE,
  UPLOAD_SCOPE_STORAGE_KEY,
  isUploadScope,
  mergeWorkspaceList,
  normalizeScopeSelection,
  uploadsEnabled,
  type UploadWorkspace,
} from "./uploadModel";

const SCOPE_EVENT = "rag-arena:upload-scope";
const WORKSPACES_EVENT = "rag-arena:upload-workspaces";

function readStoredScope(): string {
  if (typeof window === "undefined") return CORPUS_SCOPE;
  try {
    const raw = window.localStorage.getItem(UPLOAD_SCOPE_STORAGE_KEY) ?? CORPUS_SCOPE;
    return isUploadScope(raw) ? raw : CORPUS_SCOPE;
  } catch {
    return CORPUS_SCOPE;
  }
}

function persistScope(next: string): void {
  if (typeof window === "undefined") return;
  try {
    if (next) window.localStorage.setItem(UPLOAD_SCOPE_STORAGE_KEY, next);
    else window.localStorage.removeItem(UPLOAD_SCOPE_STORAGE_KEY);
    window.dispatchEvent(new CustomEvent(SCOPE_EVENT));
  } catch {
    // Private browsing mode without storage: in-memory state still updates.
  }
}

export function broadcastWorkspacesChanged(senderId?: string): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(WORKSPACES_EVENT, { detail: { senderId: senderId ?? null } }));
  }
}

export interface UploadWorkspacesState {
  readonly instanceId: string;
  readonly enabled: boolean;
  readonly workspaces: readonly UploadWorkspace[];
  readonly loading: boolean;
  readonly error: string | null;
  readonly scope: string;
  readonly isUploadActive: boolean;
  readonly activeWorkspace: UploadWorkspace | null;
  readonly setScope: (next: string) => void;
  readonly upsertWorkspace: (ws: UploadWorkspace) => void;
  readonly removeWorkspace: (workspaceId: string) => void;
  readonly reload: () => Promise<UploadWorkspace[]>;
}

export function useUploadWorkspaces(demoMode: string | null | undefined): UploadWorkspacesState {
  const instanceId = useId();
  const enabled = uploadsEnabled(demoMode);
  const [workspaces, setWorkspaces] = useState<readonly UploadWorkspace[]>([]);
  const optimisticRef = useRef<readonly UploadWorkspace[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [hasLoaded, setHasLoaded] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [rawScope, setRawScope] = useState<string>(readStoredScope);
  const { mask, maskValue } = useSafeMode();

  const reload = useCallback(async (): Promise<UploadWorkspace[]> => {
    if (!enabled) return [];
    setLoading(true);
    try {
      const list = await uploadApi.listWorkspaces();
      const fetchedIds = new Set(list.map((w) => w.id));
      optimisticRef.current = optimisticRef.current.filter((w) => !fetchedIds.has(w.id));
      const merged = mergeWorkspaceList(list, optimisticRef.current);
      setWorkspaces(merged);
      setError(null);
      setHasLoaded(true);
      return merged;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setHasLoaded(true);
      return [];
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      optimisticRef.current = [];
      setWorkspaces([]);
      setHasLoaded(false);
      setError(null);
      return undefined;
    }
    void reload();
    const onRefresh = (evt: Event) => {
      const detail = (evt as CustomEvent<{ senderId?: string | null }>).detail;
      if (detail?.senderId && detail.senderId === instanceId) return;
      void reload();
    };
    window.addEventListener(WORKSPACES_EVENT, onRefresh);
    return () => window.removeEventListener(WORKSPACES_EVENT, onRefresh);
  }, [enabled, instanceId, reload]);

  useEffect(() => {
    const syncScope = () => setRawScope(readStoredScope());
    window.addEventListener("storage", syncScope);
    window.addEventListener(SCOPE_EVENT, syncScope);
    return () => {
      window.removeEventListener("storage", syncScope);
      window.removeEventListener(SCOPE_EVENT, syncScope);
    };
  }, []);

  const setScope = useCallback((next: string) => {
    const clean = isUploadScope(next) ? next : CORPUS_SCOPE;
    setRawScope(clean);
    persistScope(clean);
  }, []);

  const upsertWorkspace = useCallback((ws: UploadWorkspace) => {
    optimisticRef.current = [ws, ...optimisticRef.current.filter((item) => item.id !== ws.id)];
    setWorkspaces((prev) => [ws, ...prev.filter((item) => item.id !== ws.id)]);
    setHasLoaded(true);
  }, []);

  const removeWorkspace = useCallback((workspaceId: string) => {
    optimisticRef.current = optimisticRef.current.filter((item) => item.id !== workspaceId);
    setWorkspaces((prev) => prev.filter((item) => item.id !== workspaceId));
  }, []);

  const shownWorkspaces = useMemo(() => maskValue(workspaces), [maskValue, workspaces]);
  const scope = !enabled
    ? CORPUS_SCOPE
    : !hasLoaded && isUploadScope(rawScope)
      ? rawScope
      : normalizeScopeSelection(rawScope, shownWorkspaces, demoMode);
  const isUploadActive = isUploadScope(scope);
  const activeWorkspace = useMemo(
    () => (isUploadActive ? shownWorkspaces.find((w) => w.id === scope) ?? null : null),
    [isUploadActive, scope, shownWorkspaces]
  );

  return {
    instanceId,
    enabled,
    workspaces: shownWorkspaces,
    loading,
    error: error === null ? null : mask(error),
    scope,
    isUploadActive,
    activeWorkspace,
    setScope,
    upsertWorkspace,
    removeWorkspace,
    reload,
  };
}
