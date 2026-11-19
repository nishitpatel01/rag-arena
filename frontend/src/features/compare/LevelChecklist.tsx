import React from "react";
import { Pill } from "../../components/ui/Pill";
import { formatUsd } from "../../lib/format";
import { sharedDisabledReason, type CostEstimate, type LevelChoice } from "./compareModel";

export interface LevelChecklistProps {
  readonly choices: readonly LevelChoice[];
  readonly checked: ReadonlySet<string>;
  readonly onToggle: (levelId: string, next: boolean) => void;
  readonly disabled: boolean;
}

/** One labelled checkbox per enabled level (ladder order); a level that cannot run is disabled with its reason. */
export const LevelChecklist: React.FC<LevelChecklistProps> = ({ choices, checked, onToggle, disabled }) => {
  const sharedReason = sharedDisabledReason(choices);
  return (
    <fieldset className="flex flex-col gap-1.5 rounded-md border border-line p-2" data-testid="level-checklist">
      <legend className="px-1 text-xs font-medium text-ink-2">Levels to include</legend>
      {choices.length === 0 && <p className="text-xs text-ink-3">No enabled levels returned by GET /api/v1/levels.</p>}
      {sharedReason !== null && (
        <p id="compare-shared-disabled-reason" className="rounded bg-surface-1 px-2 py-1 text-xs text-clay" role="status" data-testid="level-checklist-shared-reason">
          Cannot run: {sharedReason}
        </p>
      )}
      {choices.map((c) => {
        const id = `compare-level-${c.levelId}`;
        const blocked = c.disabledReason !== null;
        const showPerRowReason = blocked && sharedReason === null;
        return (
          <div key={c.levelId} className="min-w-0 flex flex-col gap-0.5">
            <label htmlFor={id} className={`flex min-w-0 items-center gap-2 text-xs ${blocked ? "text-ink-3" : "text-ink-1"}`}>
              <input id={id} type="checkbox" className="h-3.5 w-3.5 shrink-0 accent-moss" disabled={disabled || blocked}
                checked={!blocked && checked.has(c.levelId)} onChange={(e) => onToggle(c.levelId, e.target.checked)}
                aria-describedby={blocked ? (sharedReason !== null ? "compare-shared-disabled-reason" : `${id}-why`) : undefined} />
              <span className="shrink-0 font-medium">{c.short}</span>
              <span className="min-w-0 flex-1 truncate">{c.label}</span>
              {blocked && <Pill tone="locked">unavailable</Pill>}
            </label>
            {showPerRowReason && (
              <p id={`${id}-why`} className="ml-5 break-words text-xs text-clay">Cannot run: {c.disabledReason}</p>
            )}
          </div>
        );
      })}
    </fieldset>
  );
};

export interface CostLineProps {
  readonly demoMode: string | null;
  readonly estimate: CostEstimate;
  readonly labelFor: (engine: string) => string;
  readonly leaderboardError: string | null;
}

/** Live: estimate from recordings of the same pair (or "unknown"); replay: recordings play at no model cost. */
export const CostLine: React.FC<CostLineProps> = ({ demoMode, estimate, labelFor, leaderboardError }) => {
  if (demoMode === "replay") {
    return <p className="text-xs text-ink-3" data-testid="cost-line">Replay mode: recorded runs are played back; no model calls are made.</p>;
  }
  if (estimate.lines.length === 0) return null;
  const unknown = estimate.unknownEngines.map(labelFor).join(", ");
  return (
    <div className="text-xs text-ink-2" role="status" data-testid="cost-line">
      {estimate.totalUsd !== null ? (
        <p>
          Estimated cost: <span className="font-mono font-semibold text-ink-1">{formatUsd(estimate.totalUsd)}</span>{" "}
          <span className="text-ink-3">(estimate from recordings — average recorded run cost incl. judge, same model pair)</span>
        </p>
      ) : (
        <p>
          Estimated cost: <span className="font-semibold text-ink-1">unknown</span>{" "}
          <span className="text-ink-3">— no recordings of this model pair for {unknown}{leaderboardError ? ` (leaderboard: ${leaderboardError})` : ""}</span>
        </p>
      )}
    </div>
  );
};
