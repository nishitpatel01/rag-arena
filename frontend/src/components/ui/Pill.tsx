import React from "react";
import {
  AlertTriangle,
  Check,
  Circle,
  CircleDashed,
  Info,
  Loader,
  Lock,
  XCircle,
} from "lucide-react";
import { maskNode, useSafeMode } from "../../app/SafeMode";

export type PillTone = "neutral" | "ok" | "err" | "warn" | "info" | "active" | "locked";

const TONE_CLASS: Record<PillTone, string> = {
  neutral: "border-line bg-surface-2 text-ink-2",
  ok: "border-ok text-ok",
  err: "border-err text-err",
  warn: "border-clay text-clay",
  info: "border-slate text-slate",
  active: "border-moss text-moss",
  locked: "border-line text-ink-3",
};

const TONE_ICON: Record<PillTone, React.ComponentType<{ className?: string }>> = {
  neutral: Circle,
  ok: Check,
  err: XCircle,
  warn: AlertTriangle,
  info: Info,
  active: Loader,
  locked: Lock,
};

export interface PillProps {
  readonly tone?: PillTone;
  readonly icon?: React.ReactNode | false;
  readonly mono?: boolean;
  readonly title?: string;
  readonly children: React.ReactNode;
}

/** Status chip: tone is always paired with an icon and text, never colour alone. */
export const Pill: React.FC<PillProps> = ({ tone = "neutral", icon, mono = false, title, children }) => {
  const Icon = TONE_ICON[tone];
  const showIcon = icon !== false;
  const { mask } = useSafeMode();
  return (
    <span
      title={title === undefined ? undefined : mask(title)}
      className={`inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${
        mono ? "font-mono" : "font-medium"
      } ${TONE_CLASS[tone]}`}
    >
      {showIcon && (icon ?? <Icon className="h-3 w-3 shrink-0" aria-hidden />)}
      <span className="truncate">{React.Children.map(children, (c) => maskNode(c, mask))}</span>
    </span>
  );
};

export { CircleDashed as SkippedIcon };
