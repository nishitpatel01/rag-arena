import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Play, RotateCcw, Square } from "lucide-react";
import type { BenchmarkQuestion } from "../../types/arena";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { QUESTION_ID_ERROR, QUESTION_ID_PATTERN, validateQueryInput } from "../../lib/validation";
import { Button } from "../../components/ui/Button";
import type { RunGate } from "../ingest/ingestStatus";
import { ModelPicker } from "./ModelPicker";
import { isMockRequested } from "./useBattleStream";
import { replayGate, validateSelection, type ModelSelection } from "./modelSelection";
import type { ModelCatalogState } from "./useModelCatalog";
import { useQuestionFields, type QuestionOverride } from "./useQuestionFields";

export interface QuestionPickerProps {
  readonly levelId: string;
  /** Page session holding the question fields, so they survive navigation. */
  readonly sessionKey: string;
  readonly gate: RunGate;
  readonly isStreaming: boolean;
  /** `models` is null when the catalog is unavailable (pre-picker backend): server defaults apply. */
  readonly onRun: (query: string, questionId: string | null, models: ModelSelection | null) => void;
  readonly onAbort: () => void;
  /** Stops any run and clears the page's results, selections and question fields. */
  readonly onReset: () => void;
  /** True when the page's results / selections hold something besides the question fields. */
  readonly hasResults: boolean;
  readonly models: ModelCatalogState;
  /** Question of a viewed Compare run: shown in the form until the user edits it. */
  readonly viewing?: QuestionOverride | null;
  /** "clear" while a Compare run is shown: the button only hides it on this page. */
  readonly resetMode?: "reset" | "clear";
}

const fieldClass = "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 placeholder:text-ink-3";

/**
 * Golden-question select + editable question + explicit `question_id` (the recording key in replay
 * mode, the reference-answer key for the judge). Run is gated by validation and the ingest gate.
 */
export const QuestionPicker: React.FC<QuestionPickerProps> = ({ levelId, sessionKey, gate, isStreaming, onRun, onAbort, onReset, hasResults, models, viewing = null, resetMode = "reset" }) => {
  const [questions, setQuestions] = useState<readonly BenchmarkQuestion[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { mask } = useSafeMode();
  const goldenIds = useMemo(() => questions.map((q) => q.question_id), [questions]);
  const { selectedId, setSelectedId, questionId, setQuestionId, query, setQuery } = useQuestionFields(sessionKey, viewing, goldenIds);
  const canReset = hasResults || selectedId !== "" || questionId !== "" || query !== "";

  useEffect(() => {
    let cancelled = false;
    api.benchmarks().then(
      (res) => !cancelled && setQuestions(res.questions),
      (err: unknown) => !cancelled && setLoadError(err instanceof Error ? err.message : String(err))
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const onPick = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    const id = e.target.value;
    setSelectedId(id);
    const q = questions.find((x) => x.question_id === id);
    if (q) {
      setQuery(q.question);
      setQuestionId(q.question_id);
    }
  };

  const onType = (e: React.ChangeEvent<HTMLTextAreaElement>): void => {
    setQuery(e.target.value);
    const match = questions.find((x) => x.question === e.target.value);
    setSelectedId(match ? match.question_id : "");
    if (!match && questionId === selectedId) setQuestionId("");
  };

  const validation = validateQueryInput(query, [levelId]);
  const idError = QUESTION_ID_PATTERN.test(questionId) ? null : QUESTION_ID_ERROR;
  const { catalog, selection } = models;
  const modelError = catalog && selection ? validateSelection(catalog, selection, levelId) : null;
  // DEV `?mock=1` plays a local fixture (never the backend), so the replay-recording gate does not apply.
  const replay = catalog && selection && !isMockRequested() ? replayGate(catalog, levelId, questionId, selection) : null;
  const disabled =
    isStreaming || validation !== null || idError !== null || gate.blocked || modelError !== null || Boolean(replay?.blockedReason);
  const runModels = catalog && selection ? selection : null;

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled) onRun(query.trim(), questionId.trim() || null, runModels);
      }}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="golden-question" className="text-xs font-medium text-ink-2">Golden question</label>
        <select id="golden-question" value={selectedId} onChange={onPick} className={fieldClass}>
          <option value="">— choose a benchmark question —</option>
          {questions.map((q) => (
            <option key={q.question_id} value={q.question_id}>
              {q.question_id} · {q.failure_mode_category}
            </option>
          ))}
        </select>
        {loadError && <p className="text-xs text-err" role="alert">Benchmarks unavailable: {mask(loadError)}</p>}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="custom-question" className="text-xs font-medium text-ink-2">Question (editable)</label>
        <textarea id="custom-question" value={query} onChange={onType} rows={5} placeholder="Ask about CLIN rates, SLA matrices, amendments…" className={`${fieldClass} resize-y`} />
        <div className="flex items-center justify-end text-xs text-ink-3">
          <span>{query.length} chars</span>
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="question-id" className="text-xs font-medium text-ink-2">
          question_id <span className="font-normal text-ink-3">(recording key in replay; empty = ad-hoc)</span>
        </label>
        <input id="question-id" value={questionId} onChange={(e) => setQuestionId(e.target.value)} placeholder="e.g. q_exact_clin_lookup" spellCheck={false} className={`${fieldClass} font-mono`} />
        {idError && <p className="text-xs text-clay" role="alert">{idError}</p>}
      </div>
      <ModelPicker
        catalog={catalog}
        loading={models.loading}
        error={models.error}
        selection={selection}
        onChange={models.setSelection}
        disabled={isStreaming}
        validationError={modelError}
        replay={replay}
        levelId={levelId}
      />
      {validation && query.length > 0 && <p className="text-xs text-clay">{validation}</p>}
      {gate.blocked && gate.count === 0 && (
        <p className="text-xs text-clay" role="alert">
          No chunks ingested for this level's tier. <Link to="/ingest" className="font-medium underline">Run ingestion</Link> first.
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" disabled={disabled} leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}>
          {isStreaming ? "Running…" : "Run level"}
        </Button>
        {isStreaming && (
          <Button variant="danger" onClick={onAbort} leadingIcon={<Square className="h-3.5 w-3.5" aria-hidden />}>Stop</Button>
        )}
        <Button variant="ghost" onClick={onReset} disabled={!canReset} data-testid="reset-page"
          title={resetMode === "clear" ? "Hide the Compare run on this page (Compare keeps it)" : "Clear this page's results, selections and question (models are kept)"}
          leadingIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}>
          {resetMode === "clear" ? "Clear" : isStreaming ? "Stop & reset" : "Reset"}
        </Button>
      </div>
    </form>
  );
};
