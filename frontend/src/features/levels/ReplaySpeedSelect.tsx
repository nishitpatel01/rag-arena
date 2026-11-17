import React, { useCallback, useEffect, useState } from "react";
import { useAppData } from "../../app/AppDataProvider";
import { SegmentedControl } from "../../components/ui/SegmentedControl";

export type ReplaySpeedPreset = "1" | "4" | "instant";

export const REPLAY_SPEED_STORAGE_KEY = "rag-arena.replay-speed";
export const DEFAULT_REPLAY_SPEED: ReplaySpeedPreset = "1";
const REPLAY_SPEED_EVENT = "rag-arena:replay-speed";

export const REPLAY_SPEED_MULTIPLIER: Readonly<Record<ReplaySpeedPreset, number>> = {
  "1": 1.0,
  "4": 4.0,
  instant: 100.0,
};

export interface ReplaySpeedOption {
  readonly value: ReplaySpeedPreset;
  readonly label: string;
  readonly multiplier: number;
  readonly title: string;
}

export const REPLAY_SPEED_OPTIONS: readonly ReplaySpeedOption[] = [
  { value: "1", label: "1×", multiplier: 1.0, title: "Real-time recorded SSE pacing (1.0×)" },
  { value: "4", label: "4×", multiplier: 4.0, title: "Fast-forward demo pacing (4.0×)" },
  { value: "instant", label: "Instant", multiplier: 100.0, title: "Near-instant replay playback (100×)" },
];

export function parseReplaySpeed(raw: unknown): ReplaySpeedPreset {
  if (raw === "1" || raw === 1 || raw === "1.0") return "1";
  if (raw === "4" || raw === 4 || raw === "4.0") return "4";
  if (raw === "instant" || raw === 100 || raw === "100" || raw === "100.0") return "instant";
  return DEFAULT_REPLAY_SPEED;
}

export function replaySpeedMultiplier(preset: ReplaySpeedPreset): number {
  return REPLAY_SPEED_MULTIPLIER[preset] ?? 1.0;
}

export function readStoredReplaySpeed(): ReplaySpeedPreset {
  if (typeof window === "undefined") return DEFAULT_REPLAY_SPEED;
  try {
    return parseReplaySpeed(window.localStorage.getItem(REPLAY_SPEED_STORAGE_KEY));
  } catch {
    return DEFAULT_REPLAY_SPEED;
  }
}

export function readReplaySpeedMultiplier(): number {
  return replaySpeedMultiplier(readStoredReplaySpeed());
}

/**
 * Wire helper for `battleTransport.ts`: emits `replay_speed` (`1.0` | `4.0` | `100.0`) ONLY when
 * `demoMode === "replay"` (or when `demoMode` is omitted and the caller already verified replay mode).
 * Returns `undefined` in `live` and `record` modes so live telemetry is never altered.
 */
export function replaySpeedWireValue(
  demoMode: string | null | undefined,
  preset?: ReplaySpeedPreset,
): number | undefined {
  if (demoMode !== "replay") return undefined;
  return replaySpeedMultiplier(preset ?? readStoredReplaySpeed());
}

export function useReplaySpeed(): readonly [ReplaySpeedPreset, (next: ReplaySpeedPreset) => void, number] {
  const [speed, setSpeedState] = useState<ReplaySpeedPreset>(readStoredReplaySpeed);

  useEffect(() => {
    const sync = () => setSpeedState(readStoredReplaySpeed());
    window.addEventListener("storage", sync);
    window.addEventListener(REPLAY_SPEED_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(REPLAY_SPEED_EVENT, sync);
    };
  }, []);

  const setSpeed = useCallback((next: ReplaySpeedPreset) => {
    const valid = parseReplaySpeed(next);
    setSpeedState(valid);
    try {
      window.localStorage.setItem(REPLAY_SPEED_STORAGE_KEY, valid);
      window.dispatchEvent(new CustomEvent(REPLAY_SPEED_EVENT));
    } catch {
      // Storage unavailable (private browsing / quota): in-memory state still updates.
    }
  }, []);

  return [speed, setSpeed, replaySpeedMultiplier(speed)] as const;
}

function useOptionalDemoMode(explicit?: string | null): string | null {
  let ctxDemoMode: string | null = null;
  try {
    ctxDemoMode = useAppData().health?.demo_mode ?? null;
  } catch {
    ctxDemoMode = null;
  }
  return explicit !== undefined ? explicit : ctxDemoMode;
}

export interface ReplaySpeedSelectProps {
  /** Optional controlled value; defaults to shared `useReplaySpeed()` state. */
  readonly value?: ReplaySpeedPreset;
  readonly onChange?: (next: ReplaySpeedPreset) => void;
  /** Current `DEMO_MODE` (`"replay"` | `"live"` | `"record"`). Defaults to `AppDataProvider` health. */
  readonly demoMode?: string | null;
  readonly disabled?: boolean;
  /** When true (default), renders nothing in `live` or `record` mode so live runs show zero replay clutter. */
  readonly hideOutsideReplay?: boolean;
}

/**
 * Compact accessible `1×` | `4×` | `Instant` replay pacing selector.
 *
 * Mount snippet for `ComparePage.tsx` / `LevelPage.tsx`:
 * ```tsx
 * <ReplaySpeedSelect demoMode={health?.demo_mode ?? null} />
 * ```
 */
export const ReplaySpeedSelect: React.FC<ReplaySpeedSelectProps> = ({
  value: controlledValue,
  onChange: controlledOnChange,
  demoMode: explicitDemoMode,
  disabled = false,
  hideOutsideReplay = true,
}) => {
  const [storedSpeed, setStoredSpeed] = useReplaySpeed();
  const effectiveDemoMode = useOptionalDemoMode(explicitDemoMode);

  const isOutsideReplay = effectiveDemoMode !== null && effectiveDemoMode !== "replay";
  if (isOutsideReplay && hideOutsideReplay) {
    return null;
  }

  const active = controlledValue ?? storedSpeed;
  const handleChange = (next: ReplaySpeedPreset): void => {
    setStoredSpeed(next);
    controlledOnChange?.(next);
  };

  return (
    <div
      className="inline-flex items-center gap-2"
      data-testid="replay-speed-select"
      data-print-hide="true"
    >
      <span className="text-xs font-medium text-ink-2">Replay speed</span>
      <SegmentedControl<ReplaySpeedPreset>
        label="Replay speed"
        value={active}
        onChange={handleChange}
        options={REPLAY_SPEED_OPTIONS.map((opt) => ({
          value: opt.value,
          label: opt.label,
          disabled: disabled || isOutsideReplay,
          title: isOutsideReplay
            ? "Replay speed applies only when DEMO_MODE=replay"
            : opt.title,
        }))}
      />
    </div>
  );
};
