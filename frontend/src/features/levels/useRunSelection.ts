import { useCallback } from "react";
import { useSessionUi } from "../../app/BattleSessions";
import type { Citation } from "../../types/arena";
import type { LevelRun } from "./useLevelRun";

const isStageId = (v: unknown): v is string | null => v === null || typeof v === "string";
const isCitation = (v: unknown): v is Citation | null => v === null || (typeof v === "object" && !Array.isArray(v));

export interface RunSelection {
  readonly selectedStage: string | null;
  readonly userPicked: boolean;
  /** Unmasked stored citation (mask before rendering). */
  readonly citation: Citation | null;
  readonly setSelectedStage: (id: string | null) => void;
  readonly setUserPicked: (v: boolean) => void;
  readonly setCitation: (c: Citation | null) => void;
  /** Clears the level run's own selection fields (before starting a new level run). */
  readonly clearOwn: () => void;
}

/**
 * Stage / citation selection of the run the level page shows. Own level run → the level session's fields;
 * viewed Compare run → the page-local `compareView` record keyed by that run's start stamp (never leaks
 * into the Compare page or into another run).
 */
export function useRunSelection(sessionKey: string, run: LevelRun): RunSelection {
  const [ownStage, setOwnStage] = useSessionUi<string | null>(sessionKey, "selectedStage", null, isStageId);
  const [ownPicked, setOwnPicked] = useSessionUi<boolean>(sessionKey, "userPicked", false);
  const [ownCitation, setOwnCitation] = useSessionUi<Citation | null>(sessionKey, "citation", null, isCitation);
  const { compareView: cv, setCompareView } = run;

  const setSelectedStage = useCallback(
    (id: string | null) => (cv ? (cv.selectedStage !== id && setCompareView({ ...cv, selectedStage: id })) : setOwnStage(id)),
    [cv, setCompareView, setOwnStage]
  );
  const setUserPicked = useCallback(
    (v: boolean) => (cv ? (cv.userPicked !== v && setCompareView({ ...cv, userPicked: v })) : setOwnPicked(v)),
    [cv, setCompareView, setOwnPicked]
  );
  const setCitation = useCallback(
    (c: Citation | null) => (cv ? setCompareView({ ...cv, citation: c }) : setOwnCitation(c)),
    [cv, setCompareView, setOwnCitation]
  );

  const clearOwn = useCallback(() => {
    setOwnStage(null);
    setOwnPicked(false);
    setOwnCitation(null);
  }, [setOwnStage, setOwnPicked, setOwnCitation]);

  const setters = { setSelectedStage, setUserPicked, setCitation, clearOwn };
  if (cv) {
    const citation = isCitation(cv.citation) ? cv.citation : null;
    return { selectedStage: cv.selectedStage, userPicked: cv.userPicked, citation, ...setters };
  }
  return { selectedStage: ownStage, userPicked: ownPicked, citation: ownCitation, ...setters };
}
