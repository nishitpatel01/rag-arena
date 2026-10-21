import React, { useState } from "react";
import { Check, Copy } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";

export interface CodeBlockProps {
  readonly code: string;
  readonly language?: string;
  readonly label?: string;
  readonly maxHeightClass?: string;
}

/** Monospace, wrapping code block with a copy button. Used for SQL / API bodies / payloads. */
export const CodeBlock: React.FC<CodeBlockProps> = ({
  code: rawCode,
  language,
  label,
  maxHeightClass = "max-h-72",
}) => {
  const [copied, setCopied] = useState(false);
  const { mask } = useSafeMode();
  // Safe mode masks both what is shown and what the Copy button puts on the clipboard.
  const code = mask(rawCode);

  const onCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <figure className="min-w-0 rounded-md border border-line bg-surface-0">
      <figcaption className="flex items-center justify-between gap-2 border-b border-line px-2 py-1 text-xs text-ink-3">
        <span className="truncate font-mono">
          {mask(label ?? "code")}
          {language ? ` · ${language}` : ""}
        </span>
        <button
          type="button"
          onClick={onCopy}
          onBlur={() => setCopied(false)}
          aria-label={copied ? "Copied to clipboard" : "Copy to clipboard"}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-ink-3 hover:bg-surface-2 hover:text-ink-1"
        >
          {copied ? <Check className="h-3 w-3" aria-hidden /> : <Copy className="h-3 w-3" aria-hidden />}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </figcaption>
      <pre className={`${maxHeightClass} overflow-auto whitespace-pre-wrap break-words p-2.5 font-mono text-xs leading-relaxed text-ink-1`}>
        <code>{code}</code>
      </pre>
    </figure>
  );
};

export function stringifyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}
