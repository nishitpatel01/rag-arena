import React from "react";

/**
 * Minimal, dependency-free rendering of the Markdown subset Gemini answers use:
 * paragraphs, `*`/`-` bullet lists, numbered lists, **bold**, `inline code`.
 * Everything is emitted as React nodes (never innerHTML), so model output cannot inject markup.
 */

const INLINE_RE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

export function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  return text.split(INLINE_RE).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key} className="font-semibold text-ink-1">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={key} className="rounded bg-surface-2 px-1 font-mono text-xs">{part.slice(1, -1)}</code>;
    }
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

type Block = { kind: "p"; lines: string[] } | { kind: "ul" | "ol"; items: string[] };

const BULLET_RE = /^\s*[*-]\s+(.*)$/;
const NUMBER_RE = /^\s*\d+[.)]\s+(.*)$/;

export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = BULLET_RE.exec(line);
    const numbered = NUMBER_RE.exec(line);
    const last = blocks[blocks.length - 1];
    if (bullet) {
      if (last?.kind === "ul") last.items.push(bullet[1]);
      else blocks.push({ kind: "ul", items: [bullet[1]] });
    } else if (numbered) {
      if (last?.kind === "ol") last.items.push(numbered[1]);
      else blocks.push({ kind: "ol", items: [numbered[1]] });
    } else if (line.trim() === "") {
      if (last?.kind === "p" && last.lines.length > 0) blocks.push({ kind: "p", lines: [] });
    } else if (last?.kind === "p") {
      last.lines.push(line);
    } else {
      blocks.push({ kind: "p", lines: [line] });
    }
  }
  return blocks.filter((b) => (b.kind === "p" ? b.lines.length > 0 : b.items.length > 0));
}

export const MiniMarkdown: React.FC<{ readonly text: string; readonly className?: string }> = ({ text, className = "" }) => (
  <div className={`flex flex-col gap-2 text-sm leading-relaxed text-ink-1 ${className}`}>
    {parseBlocks(text).map((block, bi) => {
      if (block.kind === "p") return <p key={bi}>{renderInline(block.lines.join(" "), `p${bi}`)}</p>;
      const Tag = block.kind;
      return (
        <Tag key={bi} className={`${block.kind === "ul" ? "list-disc" : "list-decimal"} flex flex-col gap-1 pl-5`}>
          {block.items.map((item, ii) => (
            <li key={ii}>{renderInline(item, `l${bi}-${ii}`)}</li>
          ))}
        </Tag>
      );
    })}
  </div>
);
