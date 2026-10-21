import React from "react";
import { maskNode, useSafeMode } from "../../app/SafeMode";

export interface KeyValueProps {
  readonly items: ReadonlyArray<readonly [string, React.ReactNode]>;
  readonly columns?: 1 | 2;
  readonly label?: string;
}

/** Definition-list rendering of label → value pairs (mono values for ids/numbers). */
export const KeyValue: React.FC<KeyValueProps> = ({ items, columns = 1, label }) => {
  const { mask } = useSafeMode();
  return (
  <dl
    aria-label={label}
    className={`grid gap-x-4 gap-y-1 text-xs ${columns === 2 ? "grid-cols-2" : "grid-cols-1"}`}
  >
    {items.map(([k, v]) => (
      <div key={k} className="flex min-w-0 items-baseline justify-between gap-3 border-b border-line py-1 last:border-b-0">
        <dt className="shrink-0 text-ink-3">{mask(k)}</dt>
        <dd className="min-w-0 break-words text-right font-mono text-ink-1">{maskNode(v, mask)}</dd>
      </div>
    ))}
  </dl>
  );
};

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(3);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return `[${value.length} items]`;
  return "{…}";
}
