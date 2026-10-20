import React from "react";

export interface SegmentedOption<T extends string> {
  readonly value: T;
  readonly label: React.ReactNode;
  readonly disabled?: boolean;
  readonly title?: string;
}

export interface SegmentedControlProps<T extends string> {
  readonly label: string;
  readonly value: T;
  readonly options: readonly SegmentedOption<T>[];
  readonly onChange: (next: T) => void;
  readonly size?: "sm" | "md";
}

/** Button-group selector; each option is a real <button> with aria-pressed. */
export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
  size = "sm",
}: SegmentedControlProps<T>): React.ReactElement {
  const pad = size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm";
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-md border border-line bg-surface-1 p-0.5">
      {options.map((opt) => {
        const pressed = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            aria-pressed={pressed}
            disabled={opt.disabled}
            title={opt.title}
            onClick={() => onChange(opt.value)}
            className={`rounded font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${pad} ${
              pressed ? "bg-surface-2 text-ink-1 shadow-sm" : "text-ink-3 hover:text-ink-1"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
