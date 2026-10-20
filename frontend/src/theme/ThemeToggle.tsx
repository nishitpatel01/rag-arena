import React from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { ThemePreference, useTheme } from "./ThemeProvider";

const OPTIONS: readonly {
  readonly value: ThemePreference;
  readonly label: string;
  readonly Icon: React.ComponentType<{ className?: string }>;
}[] = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
];

export const ThemeToggle: React.FC = () => {
  const { preference, resolved, setPreference } = useTheme();
  return (
    <div
      role="group"
      aria-label={`Theme (currently ${resolved})`}
      className="inline-flex items-center rounded-md border border-line bg-surface-1 p-0.5"
    >
      {OPTIONS.map(({ value, label, Icon }) => {
        const pressed = preference === value;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={pressed}
            title={`${label} theme`}
            onClick={() => setPreference(value)}
            className={`inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs font-medium transition-colors ${
              pressed
                ? "bg-surface-2 text-ink-1 shadow-sm"
                : "text-ink-3 hover:text-ink-1"
            }`}
          >
            <Icon className="h-3.5 w-3.5" aria-hidden />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
};
