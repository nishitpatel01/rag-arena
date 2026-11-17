import React, { useEffect, useMemo, useState } from "react";
import { useAppData } from "../../app/AppDataProvider";
import { useSafeMode } from "../../app/SafeMode";
import { Pill } from "../../components/ui/Pill";
import { api } from "../../lib/api";
import { QUESTION_ID_ERROR, QUESTION_ID_PATTERN } from "../../lib/validation";
import type { BenchmarkQuestion } from "../../types/arena";
import { isMockRequested } from "../levels/useBattleStream";
import {
  GOLDEN_BADGE_LABEL,
  INDUSTRY_PACKS,
  LIVE_TEMPLATE_BADGE_LABEL,
  REPLAY_LIVE_TEMPLATE_REASON,
  filterPackItems,
  findMatchingLiveTemplate,
  resolvePackSelection,
  type IndustryPackId,
} from "../packs/industryPacks";
import { isUploadScope } from "../uploads/uploadModel";

export interface QuestionValue {
  readonly query: string;
  readonly questionId: string;
}

export interface CompareQuestionFieldsProps {
  readonly value: QuestionValue;
  readonly onChange: (next: QuestionValue) => void;
  readonly disabled: boolean;
  readonly scopeControl?: React.ReactNode;
  readonly uploadScopeId?: string | null;
  readonly demoMode?: string | null;
}

export const fieldClass = "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 placeholder:text-ink-3 disabled:opacity-60";

/** True when `questionId` is well-formed (empty = ad-hoc question). */
export function questionIdError(questionId: string): string | null {
  return QUESTION_ID_PATTERN.test(questionId) ? null : QUESTION_ID_ERROR;
}

function useOptionalAppDemoMode(): string | null {
  try {
    return useAppData().health?.demo_mode ?? null;
  } catch {
    return null;
  }
}

function safeIsMockRequested(): boolean {
  try {
    return isMockRequested();
  } catch {
    return false;
  }
}

/** Golden-question select + Industry Pack filter + editable question + explicit question_id (same rules as level pages). */
export const CompareQuestionFields: React.FC<CompareQuestionFieldsProps> = ({
  value,
  onChange,
  disabled,
  scopeControl = null,
  uploadScopeId = null,
  demoMode: explicitDemoMode,
}) => {
  const [questions, setQuestions] = useState<readonly BenchmarkQuestion[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [packId, setPackId] = useState<IndustryPackId>("all");
  const [replayTemplateNotice, setReplayTemplateNotice] = useState<string | null>(null);
  const { mask } = useSafeMode();
  const appDemoMode = useOptionalAppDemoMode();
  const uploadMode = isUploadScope(uploadScopeId);

  const effectiveDemoMode = explicitDemoMode ?? appDemoMode;
  const isReplayMode = effectiveDemoMode === "replay" && !safeIsMockRequested();

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

  const golden = questions.find((q) => q.question_id === value.questionId && q.question === value.query) ?? null;
  const activeTemplate = useMemo(
    () => (golden ? null : findMatchingLiveTemplate(value.query)),
    [golden, value.query],
  );
  const filtered = useMemo(() => filterPackItems(packId, questions), [packId, questions]);

  const visibleRecorded = useMemo(() => {
    if (!golden) return filtered.recordedQuestions;
    if (filtered.recordedQuestions.some((q) => q.question_id === golden.question_id)) {
      return filtered.recordedQuestions;
    }
    return [golden, ...filtered.recordedQuestions];
  }, [golden, filtered.recordedQuestions]);

  const selectValue = golden
    ? golden.question_id
    : activeTemplate
      ? `tpl:${activeTemplate.id}`
      : "";

  const onSelectPack = (nextPack: IndustryPackId): void => {
    setPackId(nextPack);
    if (nextPack === "legal_cuad" && isReplayMode) {
      setReplayTemplateNotice(REPLAY_LIVE_TEMPLATE_REASON);
    } else {
      setReplayTemplateNotice(null);
    }
  };

  const applySelectionValue = (rawVal: string): void => {
    if (!rawVal) {
      setReplayTemplateNotice(null);
      return;
    }
    const resolved = resolvePackSelection(rawVal, questions, isReplayMode ? "replay" : (effectiveDemoMode ?? "live"));
    if (!resolved) return;
    if (resolved.blockedInReplay) {
      setReplayTemplateNotice(resolved.reason);
      return;
    }
    setReplayTemplateNotice(null);
    onChange({ query: resolved.query, questionId: resolved.questionId });
  };

  const onPick = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    applySelectionValue(e.target.value);
  };

  const onType = (e: React.ChangeEvent<HTMLTextAreaElement>): void => {
    setReplayTemplateNotice(null);
    const text = e.target.value;
    const match = questions.find((x) => x.question === text);
    // Editing a golden question away from its text makes it ad-hoc (no recording / reference answer applies).
    onChange({ query: text, questionId: match ? match.question_id : golden ? "" : value.questionId });
  };
  const idError = uploadMode ? null : questionIdError(value.questionId);

  return (
    <div className="flex flex-col gap-3">
      {scopeControl}
      {!uploadMode && (
        <div className="flex flex-col gap-1.5" data-print-hide="true">
          <div className="flex flex-wrap items-center justify-between gap-1">
            <span className="text-xs font-medium text-ink-2">Industry pack</span>
            {golden && <Pill tone="ok">{GOLDEN_BADGE_LABEL}</Pill>}
            {activeTemplate && (
              <Pill tone="info">
                {LIVE_TEMPLATE_BADGE_LABEL} · {activeTemplate.vertical}
              </Pill>
            )}
          </div>
          <div
            role="group"
            aria-label="Industry benchmark pack"
            data-testid="compare-industry-pack-bar"
            className="flex flex-wrap gap-1"
          >
            {INDUSTRY_PACKS.map((pack) => {
              const pressed = pack.id === packId;
              return (
                <button
                  key={pack.id}
                  type="button"
                  disabled={disabled}
                  aria-pressed={pressed}
                  title={`${pack.label}: ${pack.description}`}
                  onClick={() => onSelectPack(pack.id)}
                  data-testid={`compare-industry-pack-${pack.id}`}
                  className={`rounded-md border px-2 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    pressed
                      ? "border-moss bg-surface-2 text-ink-1"
                      : "border-line bg-surface-1 text-ink-2 hover:text-ink-1"
                  }`}
                >
                  {pack.shortLabel}
                </button>
              );
            })}
          </div>
          {packId !== "all" && (
            <p className="text-xs text-ink-3">{filtered.pack.description}</p>
          )}
        </div>
      )}
      {!uploadMode && (
        <div className="flex flex-col gap-1">
          <label htmlFor="compare-golden-question" className="text-xs font-medium text-ink-2">Golden question</label>
          <select id="compare-golden-question" value={selectValue} onChange={onPick} disabled={disabled} className={fieldClass}>
            <option value="">— choose a benchmark question —</option>
            {visibleRecorded.length > 0 && (
              <optgroup label={GOLDEN_BADGE_LABEL}>
                {visibleRecorded.map((q) => (
                  <option key={q.question_id} value={q.question_id}>
                    {q.question_id} · {q.failure_mode_category}
                  </option>
                ))}
              </optgroup>
            )}
            {filtered.liveTemplates.length > 0 && (
              <optgroup
                label={
                  isReplayMode
                    ? `${LIVE_TEMPLATE_BADGE_LABEL} (requires DEMO_MODE=live)`
                    : `${LIVE_TEMPLATE_BADGE_LABEL} (CUAD / FSI / Healthcare)`
                }
              >
                {filtered.liveTemplates.map((tpl) => (
                  <option
                    key={tpl.id}
                    value={`tpl:${tpl.id}`}
                    disabled={isReplayMode}
                  >
                    [{LIVE_TEMPLATE_BADGE_LABEL}] {tpl.vertical} · {tpl.title}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          {packId === "legal_cuad" && filtered.liveTemplates.length > 0 && (
            <div className="mt-1 flex flex-col gap-1 rounded-md border border-line bg-surface-1 p-2">
              <div className="flex flex-wrap items-center justify-between gap-1">
                <span className="text-xs font-medium text-ink-2">CUAD / FSI / Healthcare corpus templates</span>
                <Pill tone={isReplayMode ? "warn" : "info"}>{LIVE_TEMPLATE_BADGE_LABEL}</Pill>
              </div>
              <div className="flex flex-wrap gap-1">
                {filtered.liveTemplates.map((tpl) => (
                  <button
                    key={tpl.id}
                    type="button"
                    disabled={disabled || isReplayMode}
                    title={isReplayMode ? REPLAY_LIVE_TEMPLATE_REASON : `Populate prompt from ${tpl.sourceDoc}`}
                    onClick={() => applySelectionValue(`tpl:${tpl.id}`)}
                    data-testid={`compare-live-template-chip-${tpl.id}`}
                    className="rounded border border-line bg-surface-0 px-2 py-0.5 text-left text-xs text-ink-1 transition-colors hover:border-ink-3 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {tpl.vertical}: {tpl.title.split("(")[0].trim()}
                  </button>
                ))}
              </div>
            </div>
          )}
          {replayTemplateNotice && (
            <p className="text-xs text-clay" role="status" data-testid="compare-replay-live-template-notice">
              {replayTemplateNotice}
            </p>
          )}
          {activeTemplate && (
            <p className="text-xs text-ink-3">
              Corpus doc: <span className="font-mono">{activeTemplate.sourceDoc}</span>
            </p>
          )}
          {loadError && <p className="text-xs text-err" role="alert">Benchmarks unavailable: {mask(loadError)}</p>}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <label htmlFor="compare-question" className="text-xs font-medium text-ink-2">
          {uploadMode ? "Question for your uploaded PDFs" : "Question (editable)"}
        </label>
        <textarea
          id="compare-question"
          value={value.query}
          onChange={onType}
          rows={4}
          disabled={disabled}
          placeholder={uploadMode ? "Ask a question grounded in your uploaded documents…" : "Ask about CLIN rates, SLA matrices, amendments…"}
          className={`${fieldClass} resize-y`}
        />
        <span className="self-end text-xs text-ink-3">{value.query.length} chars</span>
      </div>
      {!uploadMode && (
        <div className="flex flex-col gap-1">
          <label htmlFor="compare-question-id" className="text-xs font-medium text-ink-2">
            question_id <span className="font-normal text-ink-3">(recording key in replay; empty = ad-hoc)</span>
          </label>
          <input id="compare-question-id" value={value.questionId} disabled={disabled} spellCheck={false}
            onChange={(e) => onChange({ ...value, questionId: e.target.value })} placeholder="e.g. q_exact_clin_lookup" className={`${fieldClass} font-mono`} />
          {idError && <p className="text-xs text-clay" role="alert">{idError}</p>}
        </div>
      )}
    </div>
  );
};
