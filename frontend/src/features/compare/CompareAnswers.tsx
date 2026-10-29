import React from "react";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import type { Citation, EngineTimeline } from "../../types/arena";
import { AnswerPanel } from "../levels/AnswerPanels";
import type { LevelChoice, ScoreRow } from "./compareModel";

export interface SelectedCitation {
  readonly engine: string;
  readonly citation: Citation;
}

export interface CompareAnswersProps {
  readonly levels: readonly LevelChoice[];
  readonly timelines: Readonly<Record<string, EngineTimeline>>;
  readonly rows: readonly ScoreRow[];
  readonly running: boolean;
  readonly selected: SelectedCitation | null;
  readonly onSelectCitation: (engine: string, c: Citation) => void;
  readonly labelFor: (id: string | null) => string | null;
}

/** Answers + citations side by side (stacked under xl); a failed level shows its engine_error verbatim. */
export const CompareAnswers: React.FC<CompareAnswersProps> = ({ levels, timelines, rows, running, selected, onSelectCitation, labelFor }) => (
  <div className="grid gap-4 xl:grid-cols-[repeat(auto-fit,minmax(18rem,1fr))]">
    {levels.map((lvl) => {
      const row = rows.find((r) => r.engine === lvl.levelId);
      return (
        <Panel
          key={lvl.levelId}
          title={`${lvl.short} · ${lvl.label}`}
          actions={
            <div className="flex flex-wrap items-center gap-1.5">
              {row?.referenceAnswerUsed === false && <Pill tone="warn">no reference answer</Pill>}
              {row?.rank != null && (
                <Pill tone={row.rank === 1 ? "ok" : "neutral"} mono>
                  #{row.rank} · {row.battleScore?.toFixed(1)}
                </Pill>
              )}
            </div>
          }
        >
          <AnswerPanel
            timeline={timelines[lvl.levelId]}
            running={running}
            selectedCitation={selected?.engine === lvl.levelId ? selected.citation : null}
            onSelectCitation={(c) => onSelectCitation(lvl.levelId, c)}
            labelFor={labelFor}
          />
        </Panel>
      );
    })}
  </div>
);
