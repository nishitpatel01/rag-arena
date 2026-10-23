import React, { useEffect, useState } from "react";
import { useSafeMode } from "../../app/SafeMode";
import { api } from "../../lib/api";
import { QUESTION_ID_ERROR, QUESTION_ID_PATTERN } from "../../lib/validation";
import type { BenchmarkQuestion } from "../../types/arena";

export interface QuestionValue {
  readonly query: string;
  readonly questionId: string;
}

export interface CompareQuestionFieldsProps {
  readonly value: QuestionValue;
  readonly onChange: (next: QuestionValue) => void;
  readonly disabled: boolean;
}

export const fieldClass = "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 placeholder:text-ink-3 disabled:opacity-60";

/** True when `questionId` is well-formed (empty = ad-hoc question). */
export function questionIdError(questionId: string): string | null {
  return QUESTION_ID_PATTERN.test(questionId) ? null : QUESTION_ID_ERROR;
}

/** Golden-question select + editable question + explicit question_id (same rules as the level pages). */
export const CompareQuestionFields: React.FC<CompareQuestionFieldsProps> = ({ value, onChange, disabled }) => {
  const [questions, setQuestions] = useState<readonly BenchmarkQuestion[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { mask } = useSafeMode();

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

  const golden = questions.find((q) => q.question_id === value.questionId && q.question === value.query);
  const onPick = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    const q = questions.find((x) => x.question_id === e.target.value);
    if (q) onChange({ query: q.question, questionId: q.question_id });
  };
  const onType = (e: React.ChangeEvent<HTMLTextAreaElement>): void => {
    const text = e.target.value;
    const match = questions.find((x) => x.question === text);
    // Editing a golden question away from its text makes it ad-hoc (no recording / reference answer applies).
    onChange({ query: text, questionId: match ? match.question_id : golden ? "" : value.questionId });
  };
  const idError = questionIdError(value.questionId);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <label htmlFor="compare-golden-question" className="text-xs font-medium text-ink-2">Golden question</label>
        <select id="compare-golden-question" value={golden?.question_id ?? ""} onChange={onPick} disabled={disabled} className={fieldClass}>
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
        <label htmlFor="compare-question" className="text-xs font-medium text-ink-2">Question (editable)</label>
        <textarea id="compare-question" value={value.query} onChange={onType} rows={4} disabled={disabled}
          placeholder="Ask about CLIN rates, SLA matrices, amendments…" className={`${fieldClass} resize-y`} />
        <span className="self-end text-xs text-ink-3">{value.query.length} chars</span>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="compare-question-id" className="text-xs font-medium text-ink-2">
          question_id <span className="font-normal text-ink-3">(recording key in replay; empty = ad-hoc)</span>
        </label>
        <input id="compare-question-id" value={value.questionId} disabled={disabled} spellCheck={false}
          onChange={(e) => onChange({ ...value, questionId: e.target.value })} placeholder="e.g. q_exact_clin_lookup" className={`${fieldClass} font-mono`} />
        {idError && <p className="text-xs text-clay" role="alert">{idError}</p>}
      </div>
    </div>
  );
};
