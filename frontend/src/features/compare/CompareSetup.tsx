import React from "react";
import { Play, RotateCcw, Square } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { validateQueryInput } from "../../lib/validation";
import { ModelPicker } from "../levels/ModelPicker";
import { validateSelection, type ModelSelection } from "../levels/modelSelection";
import type { ModelCatalogState } from "../levels/useModelCatalog";
import { CompareQuestionFields, questionIdError, type QuestionValue } from "./CompareQuestionFields";
import { CostLine, LevelChecklist } from "./LevelChecklist";
import type { CostEstimate, LevelChoice } from "./compareModel";
import { isUploadScope } from "../uploads/uploadModel";

export interface CompareSetupProps {
  readonly question: QuestionValue;
  readonly onQuestion: (q: QuestionValue) => void;
  readonly models: ModelCatalogState;
  readonly choices: readonly LevelChoice[];
  readonly checked: ReadonlySet<string>;
  readonly onToggle: (levelId: string, next: boolean) => void;
  /** Checked AND runnable levels (ladder order) — exactly what Run sends. */
  readonly engines: readonly string[];
  readonly estimate: CostEstimate;
  readonly leaderboardError: string | null;
  readonly labelFor: (engine: string) => string;
  readonly isStreaming: boolean;
  readonly onRun: (engines: readonly string[], sel: ModelSelection | null) => void;
  readonly onAbort: () => void;
  /** Stops any run and clears results, stage selections, question fields and level ticks (models are kept). */
  readonly onReset: () => void;
  readonly canReset: boolean;
  readonly scopeControl?: React.ReactNode;
  readonly uploadScopeId?: string | null;
}

/** Question + one model pair + level checkboxes + cost line; Run sends ONE battle request for every checked level. */
export const CompareSetup: React.FC<CompareSetupProps> = (p) => {
  const { catalog, selection } = p.models;
  const uploadMode = isUploadScope(p.uploadScopeId);
  const pairError = catalog && selection ? validateSelection(catalog, selection, null) : null;
  const blockers = [
    validateQueryInput(p.question.query, p.engines),
    uploadMode ? null : questionIdError(p.question.questionId),
    pairError,
  ].filter((x): x is string => x !== null);
  const disabled = p.isStreaming || blockers.length > 0;
  return (
    <form
      className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)_minmax(0,2fr)]"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled) p.onRun(p.engines, catalog && selection ? selection : null);
      }}
    >
      <CompareQuestionFields
        value={p.question}
        onChange={p.onQuestion}
        disabled={p.isStreaming}
        scopeControl={p.scopeControl}
        uploadScopeId={p.uploadScopeId}
      />
      <ModelPicker catalog={catalog} loading={p.models.loading} error={p.models.error} selection={selection}
        onChange={p.models.setSelection} disabled={p.isStreaming} validationError={pairError} replay={null} levelId={null} />
      <div className="flex flex-col gap-3">
        <LevelChecklist choices={p.choices} checked={p.checked} onToggle={p.onToggle} disabled={p.isStreaming} />
        <CostLine demoMode={catalog?.demo_mode ?? null} estimate={p.estimate} labelFor={p.labelFor} leaderboardError={p.leaderboardError} />
        <p className="text-xs text-ink-3">
          One synthesis and one judge model for every level; one request runs the selected levels concurrently.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" disabled={disabled} leadingIcon={<Play className="h-3.5 w-3.5" aria-hidden />}>
            {p.isStreaming ? "Running…" : `Run ${p.engines.length} level${p.engines.length === 1 ? "" : "s"}`}
          </Button>
          {p.isStreaming && (
            <Button variant="danger" onClick={p.onAbort} leadingIcon={<Square className="h-3.5 w-3.5" aria-hidden />}>Stop</Button>
          )}
          <Button variant="ghost" onClick={p.onReset} disabled={!p.canReset} data-testid="reset-page"
            title="Clear this page's results, selections, question and level ticks (models are kept)"
            leadingIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}>
            {p.isStreaming ? "Stop & reset" : "Reset"}
          </Button>
        </div>
        {!p.isStreaming && blockers.length > 0 && p.question.query.length > 0 && (
          <ul className="text-xs text-clay" aria-label="Why Run is disabled">
            {blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        )}
      </div>
    </form>
  );
};
