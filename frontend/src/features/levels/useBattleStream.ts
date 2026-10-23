import { useCallback, useMemo } from "react";
import { useBattleSession } from "../../app/BattleSessions";
import { useSafeMode } from "../../app/SafeMode";
import type { BattleRunParams } from "./battleSession";
import { isMockRequested } from "./battleTransport";
import type { StreamState } from "./model";

export type { BattleRunParams } from "./battleSession";
export { isMockRequested } from "./battleTransport";

export interface BattleStream {
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly isMock: boolean;
  /** True when the page has a result, a live run or a transport error (Reset has something to clear). */
  readonly hasContent: boolean;
  readonly run: (params: BattleRunParams) => void;
  readonly abort: () => void;
  /** Stops any run and clears results + the page's session ui state (question, selections). */
  readonly reset: () => void;
}

/**
 * Append-only event log + derived timeline of the page session `sessionKey`. Every state change is caused
 * by an SSE event (or, in dev with `?mock=1`, by the fixture replayed at its own `t_rel` pacing). The run
 * lives in the session, not the component: unmounting the page does NOT abort it.
 */
export function useBattleStream(sessionKey: string): BattleStream {
  const { session, snapshot } = useBattleSession(sessionKey);
  const run = useCallback((params: BattleRunParams) => session.run(params), [session]);
  // Screen-share safe mode: consumers only ever see the masked view of server data.
  const { mask, maskValue } = useSafeMode();
  const shownState = useMemo(() => maskValue(snapshot.state), [maskValue, snapshot.state]);
  const shownError = snapshot.transportError === null ? null : mask(snapshot.transportError);
  return {
    state: shownState,
    isStreaming: snapshot.isStreaming,
    transportError: shownError,
    isMock: isMockRequested(),
    hasContent: snapshot.hasContent,
    run,
    abort: session.abort,
    reset: session.reset,
  };
}
