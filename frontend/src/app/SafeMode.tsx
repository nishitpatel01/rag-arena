import React, { createContext, useCallback, useContext, useMemo, useState } from "react";
import { ShieldCheck, ShieldOff } from "lucide-react";
import { useAppData } from "./AppDataProvider";
import { SAFE_MODE_STORAGE_KEY, initialSafeMode, maskDeep, maskIdentity } from "../lib/safeMode";

export interface SafeMode {
  readonly enabled: boolean;
  readonly setEnabled: (next: boolean) => void;
  /** Masks one string when safe mode is on; identity function otherwise. */
  readonly mask: (text: string) => string;
  /** Masks every string inside plain JSON-like data when safe mode is on; returns the input otherwise. */
  readonly maskValue: <T>(value: T) => T;
}

const IDENTITY_MASK: SafeMode = {
  enabled: false,
  setEnabled: () => {
    throw new Error("SafeMode.setEnabled used outside <SafeModeProvider>");
  },
  mask: (text) => text,
  maskValue: (value) => value,
};

const SafeModeContext = createContext<SafeMode>(IDENTITY_MASK);

function readStored(): string | null {
  try {
    return window.localStorage.getItem(SAFE_MODE_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(on: boolean): void {
  try {
    window.localStorage.setItem(SAFE_MODE_STORAGE_KEY, on ? "1" : "0");
  } catch {
    // Storage unavailable (private mode / quota): the in-memory toggle still applies.
  }
}

function computeInitial(): boolean {
  if (typeof window === "undefined") return false;
  const on = initialSafeMode(window.location.search, readStored());
  if (on) writeStored(true);
  return on;
}

/** Screen-share safe mode: masks project id / bucket / project number in every server-derived string. */
export const SafeModeProvider: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => {
  const { health } = useAppData();
  const [enabled, setEnabledState] = useState<boolean>(computeInitial);

  const setEnabled = useCallback((next: boolean) => {
    writeStored(next);
    setEnabledState(next);
  }, []);

  const value = useMemo<SafeMode>(() => {
    if (!enabled) return { ...IDENTITY_MASK, setEnabled };
    return {
      enabled,
      setEnabled,
      mask: (text: string) => maskIdentity(text, health),
      maskValue: <T,>(v: T) => maskDeep(v, health),
    };
  }, [enabled, health, setEnabled]);

  return <SafeModeContext.Provider value={value}>{children}</SafeModeContext.Provider>;
};

export function useSafeMode(): SafeMode {
  return useContext(SafeModeContext);
}

/** Memoised masked view of server data (re-derived when the data, health, or toggle changes). */
export function useMasked<T>(value: T): T {
  const { maskValue } = useSafeMode();
  return useMemo(() => maskValue(value), [maskValue, value]);
}

/** Masks a ReactNode when it is a plain string; elements and other nodes pass through unchanged. */
export function maskNode(node: React.ReactNode, mask: (s: string) => string): React.ReactNode {
  return typeof node === "string" ? mask(node) : node;
}

export const SafeModeToggle: React.FC = () => {
  const { enabled, setEnabled } = useSafeMode();
  const Icon = enabled ? ShieldCheck : ShieldOff;
  return (
    <button
      type="button"
      aria-pressed={enabled}
      onClick={() => setEnabled(!enabled)}
      title={enabled ? "Safe mode on: GCP identities are masked (screen-share safe)" : "Safe mode off: click to mask GCP identities for screen sharing"}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors ${
        enabled ? "border-moss bg-surface-2 text-moss" : "border-line bg-surface-1 text-ink-3 hover:text-ink-1"
      }`}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      <span>Safe mode</span>
    </button>
  );
};
