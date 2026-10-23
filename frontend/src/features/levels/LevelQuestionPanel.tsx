import React from "react";
import { Panel } from "../../components/ui/Panel";
import type { RunGate } from "../ingest/ingestStatus";
import { QuestionPicker } from "./QuestionPicker";
import type { ModelSelection } from "./modelSelection";
import type { ModelCatalogState } from "./useModelCatalog";
import type { QuestionOverride } from "./useQuestionFields";
import type { LevelRun } from "./useLevelRun";

export interface LevelQuestionPanelProps {
  readonly levelId: string;
  readonly sessionKey: string;
  readonly gate: RunGate;
  readonly run: LevelRun;
  readonly models: ModelCatalogState;
  readonly viewing: QuestionOverride | null;
  readonly hasResults: boolean;
  readonly onRun: (query: string, questionId: string | null, models: ModelSelection | null) => void;
  readonly onReset: () => void;
}

/** Question pane: picker (Run / Stop / Reset|Clear), ingest gate note, run error and stream warnings. */
export const LevelQuestionPanel: React.FC<LevelQuestionPanelProps> = ({ levelId, sessionKey, gate, run, models, viewing, hasResults, onRun, onReset }) => (
  <Panel title="Question" subtitle="Golden benchmark or custom" className="flex-1">
    {/* Run / Stop act on this page's own run only; a Compare run shown here is never stopped from the level page. */}
    <QuestionPicker levelId={levelId} sessionKey={sessionKey} gate={gate} isStreaming={run.ownStreaming} onRun={onRun}
      onAbort={run.abort} onReset={onReset} hasResults={hasResults} models={models} viewing={viewing}
      resetMode={run.source === "compare" ? "clear" : "reset"} />
    {gate.note && (
      <p className={`mt-2 text-xs ${gate.tone === "err" ? "text-err" : gate.tone === "warn" ? "text-clay" : "text-slate"}`} role="status">
        {gate.tone === "err" ? "Error" : gate.tone === "warn" ? "Blocked" : "Note"}: {gate.note}
      </p>
    )}
    {run.transportError && (
      <div className="mt-2 rounded-md border border-err p-2 text-xs text-err" role="alert" data-testid="run-error">
        <span className="font-semibold">Run rejected / stream error:</span> <span className="break-words">{run.transportError}</span>
      </div>
    )}
    {run.state.warnings.map((w, i) => (
      <p key={i} className="mt-2 text-xs text-clay" role="status">{w}</p>
    ))}
  </Panel>
);
