import { useCallback, useEffect, useState } from "react";
import { fetchModels } from "../../lib/api";
import type { ModelsResponse } from "../../types/arena";
import { isMockRequested } from "./useBattleStream";
import {
  MODEL_SELECTION_STORAGE_KEY,
  loadSelection,
  serializeSelection,
  type ModelSelection,
} from "./modelSelection";

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

/**
 * Loads GET /api/v1/models once per level page and keeps the persisted, catalog-validated selection. A stored
 * selection that is not valid on `levelId` (e.g. the Level 5 summary pseudo model on Level 1) falls back to the
 * server defaults for this page without overwriting the stored preference.
 */
export function useModelCatalog(levelId: string | null = null): ModelCatalogState {
  const [catalog, setCatalog] = useState<ModelsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [isFixture, setIsFixture] = useState(false);
  const [selection, setSelectionState] = useState<ModelSelection | null>(null);

  useEffect(() => {
    let cancelled = false;
    const accept = (res: ModelsResponse, fixture: boolean): void => {
      if (!Array.isArray(res.models) || !res.defaults) throw new Error("GET /api/v1/models returned an unexpected shape (missing models/defaults)");
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

  const setSelection = useCallback((sel: ModelSelection): void => {
    setSelectionState(sel);
    writeStored(sel);
  }, []);

  return { catalog, error, loading, isFixture, selection, setSelection };
}
