/**
 * Per-page battle session (no React). Owns the event log + derived StreamState, the SSE transport and
 * a small bounded `ui` record for page form/selection state, so results survive route changes:
 * unsubscribing (page unmount) never aborts the stream. Only `abort`, `reset` or a new `run` end it.
 * In-memory only — nothing here is written to localStorage/sessionStorage.
 */
import type { BattleEvent } from "../../types/arena";
import { INITIAL_STREAM_STATE, reduceStreamEvent, streamEndReason, streamInterruptedEvent, type StreamState } from "./model";

export interface BattleRunParams {
  readonly query: string;
  readonly questionId?: string | null;
  readonly engines: readonly string[];
  readonly solicitationFilter?: string | null;
  /** Catalog ids; omitted from the request when null (server defaults apply). */
  readonly synthesisModel?: string | null;
  readonly judgeModel?: string | null;
}

/** Transport callbacks; the frame is an already-parsed SSE `data:` JSON object. */
export interface TransportCallbacks {
  readonly onFrame: (frame: unknown) => void;
  readonly onError: (err: Error) => void;
  readonly onEnd: () => void;
}

export interface TransportHandle {
  readonly abort: () => void;
}

/** Opens one battle stream (real SSE in the app, a fake in tests, the dev fixture with `?mock=1`). */
export type OpenTransport = (params: BattleRunParams, cb: TransportCallbacks) => TransportHandle;

/** JSON-like page state (strings, flags, string lists, small plain objects such as a selected citation). */
export type UiValue = string | number | boolean | null | readonly string[] | object;
export type UiRecord = Readonly<Record<string, UiValue>>;

export interface SessionSnapshot {
  readonly state: StreamState;
  readonly isStreaming: boolean;
  readonly transportError: string | null;
  readonly ui: UiRecord;
  /** True when the session holds a run result, a live run or a transport error. */
  readonly hasContent: boolean;
  /** The current run's start stamp + request params; null before the first run and after `reset`. */
  readonly run: RunInfo | null;
}

/** When a run started (strictly increasing epoch ms from the shared RunClock) and what it requested. */
export interface RunInfo {
  readonly startedAt: number;
  readonly params: BattleRunParams;
}

/** Strictly increasing epoch-ms stamps; share ONE clock across sessions so "newest run" is well defined. */
export type RunClock = () => number;

export function createRunClock(now: () => number = Date.now): RunClock {
  let last = -Infinity;
  return () => {
    const t = Math.max(now(), last + 1);
    last = t;
    return t;
  };
}

export const MAX_UI_FIELDS = 32;
const FIELD_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
const EMPTY_UI: UiRecord = Object.freeze({});

export class BattleSession {
  private state: StreamState = INITIAL_STREAM_STATE;
  private streaming = false;
  private error: string | null = null;
  private ui: UiRecord = EMPTY_UI;
  private runInfo: RunInfo | null = null;
  private handle: TransportHandle | null = null;
  /** Each run gets a generation; transport callbacks of a superseded run are ignored. */
  private gen = 0;
  private readonly listeners = new Set<() => void>();
  private snapshot: SessionSnapshot;

  constructor(private readonly openTransport: OpenTransport, private readonly clock: RunClock = createRunClock()) {
    this.snapshot = this.build();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly getSnapshot = (): SessionSnapshot => this.snapshot;

  get isStreaming(): boolean {
    return this.streaming;
  }

  readonly run = (params: BattleRunParams): void => {
    this.stop();
    const gen = this.gen;
    let lastError: string | null = null;
    this.state = INITIAL_STREAM_STATE;
    this.error = null;
    this.streaming = true;
    this.runInfo = { startedAt: this.clock(), params: { ...params, engines: [...params.engines] } };
    this.emit();
    this.handle = this.openTransport(params, {
      onFrame: (frame) => {
        if (gen !== this.gen) return;
        this.state = reduceStreamEvent(this.state, frame as BattleEvent);
        this.emit();
      },
      onError: (err) => {
        if (gen !== this.gen) return;
        lastError = err.message;
        this.error = err.message;
        this.emit();
      },
      onEnd: () => {
        if (gen !== this.gen) return;
        this.handle = null;
        this.streaming = false;
        this.interrupt(streamEndReason({ userAborted: false, error: lastError }));
      },
    });
  };

  /** User "Stop": also marks still-running stages as interrupted (reducer no-ops after a terminal event). */
  readonly abort = (): void => {
    this.stop();
    this.interrupt(streamEndReason({ userAborted: true, error: null }));
  };

  /** Stops any run and clears results AND page ui state back to the initial state. */
  readonly reset = (): void => {
    this.stop();
    this.state = INITIAL_STREAM_STATE;
    this.error = null;
    this.runInfo = null;
    this.ui = EMPTY_UI;
    this.emit();
  };

  readonly getUi = (field: string): UiValue | undefined => this.ui[field];

  /** `undefined` deletes the field. Bounded: at most MAX_UI_FIELDS distinct fields. */
  readonly setUi = (field: string, value: UiValue | undefined): void => {
    if (!FIELD_PATTERN.test(field)) throw new Error(`BattleSession.setUi: invalid field name "${field}"`);
    if (value === undefined) {
      if (!(field in this.ui)) return;
      const next = { ...this.ui };
      delete next[field];
      this.ui = next;
    } else {
      if (Object.is(this.ui[field], value)) return;
      if (!(field in this.ui) && Object.keys(this.ui).length >= MAX_UI_FIELDS) {
        throw new Error(`BattleSession.setUi: more than ${MAX_UI_FIELDS} ui fields`);
      }
      this.ui = { ...this.ui, [field]: value };
    }
    this.emit();
  };

  readonly clearUi = (): void => {
    if (this.ui === EMPTY_UI) return;
    this.ui = EMPTY_UI;
    this.emit();
  };

  private interrupt(reason: string): void {
    this.state = reduceStreamEvent(this.state, streamInterruptedEvent(this.state.runId, reason));
    this.emit();
  }

  /** Stop the transport and invalidate its callbacks (no result change). */
  private stop(): void {
    this.gen += 1;
    const h = this.handle;
    this.handle = null;
    h?.abort();
    if (this.streaming) {
      this.streaming = false;
      this.emit();
    }
  }

  private build(): SessionSnapshot {
    return {
      state: this.state,
      isStreaming: this.streaming,
      transportError: this.error,
      ui: this.ui,
      hasContent: this.streaming || this.error !== null || this.state.events.length > 0,
      run: this.runInfo,
    };
  }

  private emit(): void {
    const prev = this.snapshot;
    const next = this.build();
    if (
      prev.state === next.state && prev.isStreaming === next.isStreaming && prev.transportError === next.transportError &&
      prev.ui === next.ui && prev.run === next.run
    ) return;
    this.snapshot = next;
    for (const l of [...this.listeners]) l();
  }
}

export const MAX_SESSIONS = 16;

/** Bounded LRU of sessions keyed by page (`level:<id>`, `compare`). Streaming sessions are never evicted. */
export class SessionRegistry {
  private readonly sessions = new Map<string, BattleSession>();

  constructor(private readonly factory: () => BattleSession, private readonly max: number = MAX_SESSIONS) {
    if (!Number.isInteger(max) || max < 1) throw new Error(`SessionRegistry: invalid max ${max}`);
  }

  get size(): number {
    return this.sessions.size;
  }

  has(key: string): boolean {
    return this.sessions.has(key);
  }

  /** Returns (creating if needed) the session for `key` and marks it most recently used. */
  get(key: string): BattleSession {
    if (key.length === 0 || key.length > 128) throw new Error(`SessionRegistry: invalid session key "${key}"`);
    const found = this.sessions.get(key);
    if (found) {
      this.sessions.delete(key);
      this.sessions.set(key, found);
      return found;
    }
    if (this.sessions.size >= this.max) this.evictOne();
    const created = this.factory();
    this.sessions.set(key, created);
    return created;
  }

  private evictOne(): void {
    for (const [k, s] of this.sessions) {
      if (!s.isStreaming) {
        this.sessions.delete(k);
        return;
      }
    }
    throw new Error(`SessionRegistry: all ${this.max} sessions are streaming; stop a run before opening another page`);
  }
}

/** Reads a ui field, falling back to `initial` when absent or when `guard` rejects the stored value. */
export function readUi<T>(ui: UiRecord, field: string, initial: T, guard: (v: unknown) => v is T): T {
  const v = ui[field];
  if (v === undefined) return initial;
  return guard(v) ? v : initial;
}

/** Default guard: same primitive kind as `initial` (arrays must hold strings). Null initials need an explicit guard. */
export function sameKindAs<T>(initial: T): (v: unknown) => v is T {
  if (initial === null) throw new Error("sameKindAs: null initial needs an explicit guard");
  if (Array.isArray(initial)) return (v: unknown): v is T => Array.isArray(v) && v.every((x) => typeof x === "string");
  return (v: unknown): v is T => typeof v === typeof initial && v !== null && !Array.isArray(v);
}
