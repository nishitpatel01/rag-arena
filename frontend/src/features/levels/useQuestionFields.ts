import { useCallback, useRef } from "react";
import { useSessionUi } from "../../app/BattleSessions";

/** Question of a viewed (Compare) run, mirrored in the form until the user edits it. */
export interface QuestionOverride {
  readonly query: string;
  readonly questionId: string;
  /** Called once on the first edit: the form then holds the user's own draft again. */
  readonly onEdit: () => void;
}

export interface QuestionFields {
  readonly selectedId: string;
  readonly questionId: string;
  readonly query: string;
  readonly setSelectedId: (v: string) => void;
  readonly setQuestionId: (v: string) => void;
  readonly setQuery: (v: string) => void;
}

/**
 * The level page's question form fields (golden id, question_id, query) from the page session. While a
 * Compare run is shown (`override`), the form displays that run's question; the first edit copies it
 * into the session fields (becoming the draft for the next level run) and stops mirroring.
 */
export function useQuestionFields(sessionKey: string, override: QuestionOverride | null, goldenIds: readonly string[]): QuestionFields {
  const [storedGolden, setStoredGolden] = useSessionUi<string>(sessionKey, "goldenId", "");
  const [storedQid, setStoredQid] = useSessionUi<string>(sessionKey, "questionId", "");
  const [storedQuery, setStoredQuery] = useSessionUi<string>(sessionKey, "query", "");
  const selectedId = override ? (goldenIds.includes(override.questionId) ? override.questionId : "") : storedGolden;
  const questionId = override ? override.questionId : storedQid;
  const query = override ? override.query : storedQuery;

  // One user action may call several setters before re-render (golden pick sets all three fields):
  // copy the override only once per override instance, or a later setter would undo an earlier one.
  const detachedFor = useRef<QuestionOverride | null>(null);
  const detach = useCallback((): void => {
    if (!override || detachedFor.current === override) return;
    detachedFor.current = override;
    setStoredGolden(selectedId);
    setStoredQid(override.questionId);
    setStoredQuery(override.query);
    override.onEdit();
  }, [override, selectedId, setStoredGolden, setStoredQid, setStoredQuery]);

  const setSelectedId = useCallback((v: string) => { detach(); setStoredGolden(v); }, [detach, setStoredGolden]);
  const setQuestionId = useCallback((v: string) => { detach(); setStoredQid(v); }, [detach, setStoredQid]);
  const setQuery = useCallback((v: string) => { detach(); setStoredQuery(v); }, [detach, setStoredQuery]);
  return { selectedId, questionId, query, setSelectedId, setQuestionId, setQuery };
}
