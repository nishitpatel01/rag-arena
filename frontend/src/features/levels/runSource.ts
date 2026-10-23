/**
 * "Latest run wins, per level" (pure, no React). A level page shows the most recent run that included its
 * level: its own level run or a Compare run (live, from the one shared Compare stream), ordered by the
 * sessions' shared RunClock stamps. A Compare run the user cleared on this level page is skipped.
 */
import type { BattleRunParams, RunInfo } from "./battleSession";
import { INITIAL_STREAM_STATE, type StreamState } from "./model";

export type RunSourceKind = "level" | "compare" | "none";

/** The parts of a SessionSnapshot this model needs (keeps tests free of BattleSession). */
export interface RunSnapshot {
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly run: RunInfo | null;
}

export interface RunSourceInput {
  readonly levelId: string;
  readonly level: RunSnapshot;
  readonly compare: RunSnapshot;
  /** `startedAt` of the Compare run cleared on this level page (null = none). */
  readonly dismissedCompareAt: number | null;
}

export interface RunView {
  readonly source: RunSourceKind;
  readonly state: StreamState;
  /** True while THIS level is still running (a Compare run may continue with other levels). */
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly startedAt: number | null;
  readonly params: BattleRunParams | null;
  /** Levels in the shown run (1 for a level run). */
  readonly levelCount: number;
  /** True when a Compare run exists that includes this level and is not cleared (shown or superseded). */
  readonly compareAvailable: boolean;
}

const NONE: RunView = {
  source: "none",
  state: INITIAL_STREAM_STATE,
  isStreaming: false,
  transportError: null,
  startedAt: null,
  params: null,
  levelCount: 0,
  compareAvailable: false,
};

/** This level's part of a (possibly still streaming) multi-level run is finished. */
export function levelFinished(state: StreamState, levelId: string): boolean {
  if (state.terminated || state.interrupted) return true;
  const tl = state.timelines[levelId];
  return Boolean(tl && (tl.error || tl.evaluation));
}

/** True when `run` is a Compare run that includes `levelId` and was not cleared on that level page. */
export function compareIncludes(run: RunInfo | null, levelId: string, dismissedAt: number | null): run is RunInfo {
  if (!run) return false;
  if (dismissedAt !== null && run.startedAt === dismissedAt) return false;
  return run.params.engines.includes(levelId);
}

export function selectRunSource(input: RunSourceInput): RunView {
  const { levelId, level, compare, dismissedCompareAt } = input;
  if (levelId.length === 0) throw new Error("selectRunSource: empty levelId");
  const compareOk = compareIncludes(compare.run, levelId, dismissedCompareAt);
  const levelAt = level.run?.startedAt ?? null;
  if (compareOk && compare.run && (levelAt === null || compare.run.startedAt > levelAt)) {
    return {
      source: "compare",
      state: compare.state,
      isStreaming: compare.isStreaming && !levelFinished(compare.state, levelId),
      transportError: compare.transportError,
      startedAt: compare.run.startedAt,
      params: compare.run.params,
      levelCount: compare.run.params.engines.length,
      compareAvailable: true,
    };
  }
  if (level.run) {
    return {
      source: "level",
      state: level.state,
      isStreaming: level.isStreaming,
      transportError: level.transportError,
      startedAt: level.run.startedAt,
      params: level.run.params,
      levelCount: 1,
      compareAvailable: compareOk,
    };
  }
  return { ...NONE, compareAvailable: compareOk };
}

/** Models the shown run executed: run_started values, else the requested ids (null = server default). */
export function runModels(view: Pick<RunView, "state" | "params">): { readonly synthesis: string | null; readonly judge: string | null } {
  return {
    synthesis: view.state.synthesisModel ?? view.params?.synthesisModel ?? null,
    judge: view.state.judgeModel ?? view.params?.judgeModel ?? null,
  };
}

/** Local `hh:mm:ss` of a run start stamp (epoch ms). */
export function formatClock(epochMs: number): string {
  if (!Number.isFinite(epochMs)) throw new Error(`formatClock: invalid stamp ${epochMs}`);
  const d = new Date(epochMs);
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Page-local ui for a viewed Compare run, keyed by its `startedAt` so it never leaks into another run. */
export interface CompareViewUi {
  readonly runAt: number;
  readonly selectedStage: string | null;
  readonly userPicked: boolean;
  readonly citation: object | null;
  /** The user edited the question form / model dropdown while viewing this run (stop mirroring the run). */
  readonly formEdited: boolean;
  readonly modelsEdited: boolean;
}

export function emptyCompareView(runAt: number): CompareViewUi {
  return { runAt, selectedStage: null, userPicked: false, citation: null, formEdited: false, modelsEdited: false };
}

export function isCompareViewUi(v: unknown): v is CompareViewUi {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.runAt === "number" && (o.selectedStage === null || typeof o.selectedStage === "string") &&
    typeof o.userPicked === "boolean" && (o.citation === null || typeof o.citation === "object") &&
    typeof o.formEdited === "boolean" && typeof o.modelsEdited === "boolean"
  );
}

/** The stored compare-view ui if it belongs to run `runAt`, else a fresh one (old run's picks are ignored). */
export function compareViewFor(stored: unknown, runAt: number): CompareViewUi {
  return isCompareViewUi(stored) && stored.runAt === runAt ? stored : emptyCompareView(runAt);
}
