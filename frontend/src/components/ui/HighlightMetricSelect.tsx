import React, { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_HIGHLIGHT_METRIC,
  HIGHLIGHT_METRIC_OPTIONS,
  HIGHLIGHT_METRIC_STORAGE_KEY,
  parseHighlightMetric,
  type HighlightMetric,
} from "../../lib/winnerHighlight";

const HIGHLIGHT_EVENT = "rag-arena:highlight-metric";

function readStoredMetric(): HighlightMetric {
  if (typeof window === "undefined") return DEFAULT_HIGHLIGHT_METRIC;
  try {
    return parseHighlightMetric(window.localStorage.getItem(HIGHLIGHT_METRIC_STORAGE_KEY));
  } catch {
    return DEFAULT_HIGHLIGHT_METRIC;
  }
}

export function useHighlightMetric(): readonly [HighlightMetric, (next: HighlightMetric) => void] {
  const [metric, setMetricState] = useState<HighlightMetric>(readStoredMetric);

  useEffect(() => {
    const sync = () => setMetricState(readStoredMetric());
    window.addEventListener("storage", sync);
    window.addEventListener(HIGHLIGHT_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(HIGHLIGHT_EVENT, sync);
    };
  }, []);

  const setMetric = useCallback((next: HighlightMetric) => {
    setMetricState(next);
    try {
      window.localStorage.setItem(HIGHLIGHT_METRIC_STORAGE_KEY, next);
      window.dispatchEvent(new CustomEvent(HIGHLIGHT_EVENT));
    } catch {
      // Storage disabled in private browser context: state still updates in-memory.
    }
  }, []);

  return [metric, setMetric] as const;
}

export interface HighlightMetricSelectProps {
  readonly value: HighlightMetric;
  readonly onChange: (next: HighlightMetric) => void;
  readonly id?: string;
}

const selectClass =
  "rounded-md border border-line bg-surface-0 px-2 py-1 text-xs text-ink-1 disabled:opacity-60";

export const HighlightMetricSelect: React.FC<HighlightMetricSelectProps> = ({
  value,
  onChange,
  id = "highlight-metric-select",
}) => (
  <label htmlFor={id} className="flex flex-col gap-1 text-xs font-medium text-ink-2">
    Highlight best by
    <select
      id={id}
      data-testid="highlight-metric-select"
      className={selectClass}
      value={value}
      onChange={(e) => onChange(parseHighlightMetric(e.target.value))}
    >
      {HIGHLIGHT_METRIC_OPTIONS.map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  </label>
);
