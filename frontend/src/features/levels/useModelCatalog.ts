import { useCallback, useEffect, useRef, useState } from "react";
import { fetchModels } from "../../lib/api";
import type { ModelsResponse } from "../../types/arena";
import { isMockRequested } from "./useBattleStream";
import {
  MODEL_SELECTION_STORAGE_KEY,
  loadSelection,
  serializeSelection,
  type ModelSelection,
} from "./modelSelection";

/** Custom window event broadcast whenever any UI control changes the active (synthesis, judge) pair. */
export const MODELS_CHANGED_EVENT = "rag-arena:models-changed";

export interface ModelCatalogState {
  readonly catalog: ModelsResponse | null;
  /** Verbatim fetch error (e.g. 404 on a pre-picker backend); the picker is then disabled. */
  readonly error: string | null;
  readonly loading: boolean;
  /** True when the catalog is the DEV-only `?mock=1` fixture (never in production bundles). */
  readonly isFixture: boolean;
  readonly selection: ModelSelection | null;
  readonly setSelection: (sel: ModelSelection) => void;
}

function readStored(): string | null {
  try {
    return window.localStorage.getItem(MODEL_SELECTION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(sel: ModelSelection): void {
  try {
    window.localStorage.setItem(MODEL_SELECTION_STORAGE_KEY, serializeSelection(sel));
  } catch {
    // Storage disabled (private mode / quota): the selection still applies to this page view.
  }
}

export function broadcastModelSelection(sel: ModelSelection): void {
  if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") return;
  try {
    window.dispatchEvent(new CustomEvent<ModelSelection>(MODELS_CHANGED_EVENT, { detail: sel }));
  } catch {
    // Non-browser environment (SSR/Node tests).
  }
}

/**
 * Loads GET /api/v1/models and keeps the persisted, catalog-validated selection synced app-wide
 * across Shell, Overview, Compare, Workbench, and Level pages via `rag-arena:models-changed`.
 * A stored selection that is not valid on `levelId` (e.g. a Level 1/2 pseudo model on another level)
 * falls back to the server defaults for that page without overwriting the stored preference.
 */
export function useModelCatalog(levelId: string | null = null): ModelCatalogState {
  const [catalog, setCatalog] = useState<ModelsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [isFixture, setIsFixture] = useState(false);
  const [selection, setSelectionState] = useState<ModelSelection | null>(null);
  const catalogRef = useRef<ModelsResponse | null>(null);

  useEffect(() => {
    catalogRef.current = catalog;
  }, [catalog]);

  useEffect(() => {
    let cancelled = false;
    const accept = (res: ModelsResponse, fixture: boolean): void => {
      if (!Array.isArray(res.models) || !res.defaults) {
        throw new Error("GET /api/v1/models returned an unexpected shape (missing models/defaults)");
      }
      catalogRef.current = res;
      setCatalog(res);
      setIsFixture(fixture);
      setSelectionState(loadSelection(res, readStored(), levelId));
      setError(null);
    };
    fetchModels()
      .then((res) => {
        if (!cancelled) accept(res, false);
      })
      .catch(async (err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        // DEV + ?mock=1 only: fixture catalog so the picker can be exercised without the backend.
        // Statically eliminated from production bundles (import.meta.env.DEV is false there).
        if (import.meta.env.DEV && isMockRequested()) {
          const { DEV_MODELS_FIXTURE } = await import("../../dev/fixtures/models");
          if (!cancelled) accept(DEV_MODELS_FIXTURE, true);
          return;
        }
        setError(message);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [levelId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const syncFromRaw = (raw: string | null): void => {
      const cat = catalogRef.current;
      if (!cat) return;
      const next = loadSelection(cat, raw, levelId);
      setSelectionState((prev) =>
        prev && prev.synthesis === next.synthesis && prev.judge === next.judge ? prev : next
      );
    };
    const onModelsChanged = (evt: Event): void => {
      const detail = (evt as CustomEvent<ModelSelection | undefined>).detail;
      const raw = detail && typeof detail.synthesis === "string" && typeof detail.judge === "string"
        ? serializeSelection(detail)
        : readStored();
      syncFromRaw(raw);
    };
    const onStorage = (evt: StorageEvent): void => {
      if (evt.key === MODEL_SELECTION_STORAGE_KEY) {
        syncFromRaw(evt.newValue);
      }
    };
    window.addEventListener(MODELS_CHANGED_EVENT, onModelsChanged);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(MODELS_CHANGED_EVENT, onModelsChanged);
      window.removeEventListener("storage", onStorage);
    };
  }, [levelId]);

  const setSelection = useCallback((sel: ModelSelection): void => {
    setSelectionState((prev) =>
      prev && prev.synthesis === sel.synthesis && prev.judge === sel.judge ? prev : sel
    );
    writeStored(sel);
    broadcastModelSelection(sel);
  }, []);

  return { catalog, error, loading, isFixture, selection, setSelection };
}
