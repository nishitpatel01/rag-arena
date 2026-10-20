import React from "react";

export interface EmptyStateProps {
  readonly title: string;
  readonly description?: React.ReactNode;
  readonly action?: React.ReactNode;
  readonly icon?: React.ReactNode;
  readonly compact?: boolean;
}

export const EmptyState: React.FC<EmptyStateProps> = ({ title, description, action, icon, compact = false }) => (
  <div
    className={`flex flex-col items-center justify-center rounded-md border border-dashed border-line text-center ${
      compact ? "gap-1 px-3 py-4" : "gap-2 px-6 py-10"
    }`}
  >
    {icon && <div className="text-ink-3">{icon}</div>}
    <p className="text-sm font-medium text-ink-2">{title}</p>
    {description && <p className="max-w-prose text-xs text-ink-3">{description}</p>}
    {action && <div className="mt-1">{action}</div>}
  </div>
);
