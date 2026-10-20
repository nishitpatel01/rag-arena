import React from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "border-moss bg-moss text-surface-0 hover:opacity-90 disabled:opacity-50",
  secondary: "border-line bg-surface-2 text-ink-1 hover:border-ink-3 disabled:opacity-50",
  ghost: "border-transparent bg-transparent text-ink-2 hover:bg-surface-2 disabled:opacity-50",
  danger: "border-err bg-transparent text-err hover:bg-surface-2 disabled:opacity-50",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "px-2.5 py-1 text-xs",
  md: "px-3.5 py-1.5 text-sm",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  readonly variant?: ButtonVariant;
  readonly size?: ButtonSize;
  readonly leadingIcon?: React.ReactNode;
}

/** Always a real <button>; `type` defaults to "button" to avoid accidental form submits. */
export const Button: React.FC<ButtonProps> = ({
  variant = "secondary",
  size = "md",
  leadingIcon,
  className = "",
  type = "button",
  children,
  ...rest
}) => (
  <button
    type={type}
    className={`inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors disabled:cursor-not-allowed ${VARIANT_CLASS[variant]} ${SIZE_CLASS[size]} ${className}`}
    {...rest}
  >
    {leadingIcon}
    {children}
  </button>
);
