import React, { createContext, useCallback, useContext, useMemo, useState, useSyncExternalStore } from "react";
import { BattleSession, SessionRegistry, createRunClock, readUi, sameKindAs, type SessionSnapshot, type UiValue } from "../features/levels/battleSession";
import { isMockRequested, mockTransport, sseTransport } from "../features/levels/battleTransport";

/**
 * Holds one BattleSession per page (`level:<levelId>`, `compare`) above the routes, so a page's run,
 * results and form state survive navigation (and keep streaming) for the life of the browser tab.
 * In-memory only: a full reload starts fresh.
 */
const RegistryContext = createContext<SessionRegistry | null>(null);

export const BattleSessionsProvider: React.FC<{ readonly children: React.ReactNode }> = ({ children }) => {
  const [registry] = useState(() => {
    // One clock for every page session: run start stamps order Compare vs level runs ("latest run wins").
    const clock = createRunClock();
    return new SessionRegistry(() => new BattleSession(import.meta.env.DEV && isMockRequested() ? mockTransport : sseTransport, clock));
  });
  return <RegistryContext.Provider value={registry}>{children}</RegistryContext.Provider>;
};

/** The session for `sessionKey` (created on first use) plus its live snapshot. */
export function useBattleSession(sessionKey: string): { readonly session: BattleSession; readonly snapshot: SessionSnapshot } {
  const registry = useContext(RegistryContext);
  if (!registry) throw new Error("useBattleSession used outside <BattleSessionsProvider>");
  const session = useMemo(() => registry.get(sessionKey), [registry, sessionKey]);
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  return { session, snapshot };
}

/** `useState`-like accessor for one page form/selection field stored in the session (validated on read). */
export function useSessionUi<T extends UiValue>(
  sessionKey: string,
  field: string,
  initial: T,
  guard?: (v: unknown) => v is T
): readonly [T, (next: T) => void] {
  const { session, snapshot } = useBattleSession(sessionKey);
  const check = useMemo(() => guard ?? sameKindAs(initial), [guard, initial]);
  const value = readUi(snapshot.ui, field, initial, check);
  const setValue = useCallback(
    (next: T): void => {
      // Storing the initial value deletes the field, so "nothing to reset" stays accurate.
      const isInitial = Object.is(next, initial) || (Array.isArray(next) && Array.isArray(initial) && next.length === 0 && initial.length === 0);
      session.setUi(field, isInitial ? undefined : next);
    },
    [session, field, initial]
  );
  return [value, setValue] as const;
}
