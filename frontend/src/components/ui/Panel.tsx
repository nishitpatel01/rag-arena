import React from "react";

export interface PanelProps {
  readonly title?: React.ReactNode;
  readonly subtitle?: React.ReactNode;
  readonly actions?: React.ReactNode;
  readonly className?: string;
  readonly bodyClassName?: string;
  readonly as?: "section" | "aside" | "div";
  readonly children: React.ReactNode;
}

/** Bordered surface used for every content region. */
export const Panel: React.FC<PanelProps> = ({
  title,
  subtitle,
  actions,
  className = "",
  bodyClassName = "",
  as: Tag = "section",
  children,
}) => (
  <Tag className={`flex min-w-0 flex-col rounded-lg border border-line bg-surface-1 ${className}`}>
    {(title || actions) && (
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-line px-4 py-2.5">
        <div className="min-w-0">
          {title && <h2 className="text-sm font-semibold text-ink-1">{title}</h2>}
          {subtitle && <p className="text-xs text-ink-3">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </header>
    )}
    <div className={`min-w-0 flex-1 p-4 ${bodyClassName}`}>{children}</div>
  </Tag>
);
