import { useCallback, useMemo } from "react";
import { useBattleSession } from "../../app/BattleSessions";
import { useSafeMode } from "../../app/SafeMode";
import type { BattleRunParams } from "./battleSession";
import { isMockRequested } from "./battleTransport";
import type { StreamState } from "./model";
import { compareViewFor, selectRunSource, type CompareViewUi, type RunSourceKind } from "./runSource";

/** Session key of the Compare page (kept here so level pages can read it without importing the page). */
export const COMPARE_SESSION = "compare";
const DISMISSED_FIELD = "dismissedCompare";
const COMPARE_VIEW_FIELD = "compareView";

export interface LevelRun {
  readonly source: RunSourceKind;
  /** Masked (safe mode) state of the shown run. */
  readonly state: StreamState;
  /** This level of the shown run is still running. */
  readonly isStreaming: boolean;
  /** The level page's OWN run is streaming (gates Run / Stop; a Compare run is never stopped from here). */
  readonly ownStreaming: boolean;
  readonly transportError: string | null;
  readonly startedAt: number | null;
  /** Masked request of the shown run (question text, question_id, requested models). */
  readonly params: BattleRunParams | null;
  readonly levelCount: number;
  readonly isMock: boolean;
  readonly hasContent: boolean;
  /** Page-local ui of the viewed Compare run (null unless source is "compare"). */
  readonly compareView: CompareViewUi | null;
  readonly setCompareView: (next: CompareViewUi) => void;
  readonly run: (params: BattleRunParams) => void;
  readonly abort: () => void;
  /** Level run shown: reset the level session (an older Compare run stays hidden). Compare shown: clear it here only. */
  readonly reset: () => void;
}

/** The run a level page shows: its own run or the newest Compare run that included the level. */
export function useLevelRun(levelId: string): LevelRun {
  const sessionKey = `level:${levelId}`;
  const own = useBattleSession(sessionKey);
  const cmp = useBattleSession(COMPARE_SESSION);
  const { mask, maskValue } = useSafeMode();
  const dismissedRaw = own.snapshot.ui[DISMISSED_FIELD];
  const dismissedCompareAt = typeof dismissedRaw === "number" ? dismissedRaw : null;
  const view = useMemo(
    () => selectRunSource({ levelId, level: own.snapshot, compare: cmp.snapshot, dismissedCompareAt }),
    [levelId, own.snapshot, cmp.snapshot, dismissedCompareAt]
  );
  const shownState = useMemo(() => maskValue(view.state), [maskValue, view.state]);
  const shownParams = useMemo(() => (view.params ? maskValue(view.params) : null), [maskValue, view.params]);
  const compareView = view.source === "compare" && view.startedAt !== null ? compareViewFor(own.snapshot.ui[COMPARE_VIEW_FIELD], view.startedAt) : null;
  const ownSession = own.session;
  const compareRun = cmp.snapshot.run;
  const compareAvailable = view.compareAvailable;

  const setCompareView = useCallback((next: CompareViewUi) => ownSession.setUi(COMPARE_VIEW_FIELD, next), [ownSession]);
  const reset = useCallback((): void => {
    if (view.source === "compare" && compareRun) {
      ownSession.setUi(COMPARE_VIEW_FIELD, undefined);
      ownSession.setUi(DISMISSED_FIELD, compareRun.startedAt);
      return;
    }
    ownSession.reset();
    // Reset means "empty page": an older Compare run that included this level must not resurface.
    if (compareAvailable && compareRun) ownSession.setUi(DISMISSED_FIELD, compareRun.startedAt);
  }, [view.source, compareRun, compareAvailable, ownSession]);

  return {
    source: view.source,
    state: shownState,
    isStreaming: view.isStreaming,
    ownStreaming: own.snapshot.isStreaming,
    transportError: view.transportError === null ? null : mask(view.transportError),
    startedAt: view.startedAt,
    params: shownParams,
    levelCount: view.levelCount,
    isMock: isMockRequested(),
    hasContent: view.source !== "none" || own.snapshot.hasContent,
    compareView,
    setCompareView,
    run: ownSession.run,
    abort: ownSession.abort,
    reset,
  };
}
