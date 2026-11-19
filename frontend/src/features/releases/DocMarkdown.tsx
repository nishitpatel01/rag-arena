import React from "react";
import { Link } from "react-router-dom";
import { CodeBlock } from "../../components/ui/CodeBlock";
import { useSafeMode } from "../../app/SafeMode";
import { parseDocBlocks, parseInlineTokens, type DocBlock, type InlineToken, type LeafInlineToken, type TableAlign } from "./releasesModel";

function renderLeafToken(tok: LeafInlineToken, key: string): React.ReactNode {
  if (tok.kind === "code") {
    return <code key={key} className="rounded bg-surface-2 px-1 py-0.5 font-mono text-xs text-ink-1">{tok.text}</code>;
  }
  if (tok.kind === "link") {
    if (tok.internalRoute) {
      return (
        <Link key={key} to={tok.href} className="font-medium text-moss underline-offset-2 hover:underline">
          {tok.label}
        </Link>
      );
    }
    if (tok.external) {
      return (
        <a
          key={key}
          href={tok.href}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium text-moss underline-offset-2 hover:underline"
        >
          {tok.label}
        </a>
      );
    }
    return (
      <a key={key} href={tok.href} className="font-medium text-moss underline-offset-2 hover:underline">
        {tok.label}
      </a>
    );
  }
  return <React.Fragment key={key}>{tok.text}</React.Fragment>;
}

export function renderRichInline(text: string, keyPrefix: string, mask: (s: string) => string): React.ReactNode[] {
  return parseInlineTokens(mask(text)).map((tok: InlineToken, idx: number) => {
    const key = `${keyPrefix}-${idx}`;
    if (tok.kind === "strong") {
      return (
        <strong key={key} className="font-semibold text-ink-1">
          {tok.children.map((child, cIdx) => renderLeafToken(child, `${key}-s${cIdx}`))}
        </strong>
      );
    }
    return renderLeafToken(tok, key);
  });
}

function alignClass(align: TableAlign): string {
  if (align === "center") return "text-center";
  if (align === "right") return "text-right";
  return "text-left";
}

function renderDocBlock(block: DocBlock, idx: number, mask: (s: string) => string): React.ReactNode {
  const key = `blk-${idx}`;
  if (block.kind === "heading") {
    const content = renderRichInline(block.text, `${key}-h`, mask);
    if (block.level === 1) {
      return <h2 key={key} id={block.id} className="scroll-mt-20 border-b border-line pb-2 text-base font-semibold text-ink-1">{content}</h2>;
    }
    if (block.level === 2) {
      return <h2 key={key} id={block.id} className="mt-4 scroll-mt-20 border-b border-line pb-1.5 text-sm font-semibold text-ink-1">{content}</h2>;
    }
    if (block.level === 3) {
      return <h3 key={key} id={block.id} className="mt-3 scroll-mt-20 text-xs font-semibold uppercase tracking-wide text-ink-2">{content}</h3>;
    }
    return <h4 key={key} id={block.id} className="mt-2 scroll-mt-20 text-xs font-semibold text-ink-1">{content}</h4>;
  }

  if (block.kind === "paragraph") {
    return <p key={key} className="text-sm leading-relaxed text-ink-2">{renderRichInline(block.text, `${key}-p`, mask)}</p>;
  }

  if (block.kind === "blockquote") {
    return (
      <blockquote key={key} className="rounded-md border border-line bg-surface-0 px-3 py-2 text-xs leading-relaxed text-ink-2">
        {renderRichInline(block.text, `${key}-q`, mask)}
      </blockquote>
    );
  }

  if (block.kind === "code") {
    const label = block.language === "mermaid" ? "architecture diagram (mermaid)" : "snippet";
    return <CodeBlock key={key} code={block.code} language={block.language || undefined} label={label} maxHeightClass="max-h-96" />;
  }

  if (block.kind === "hr") {
    return <hr key={key} className="my-2 border-line" />;
  }

  if (block.kind === "list") {
    const ListTag = block.ordered ? "ol" : "ul";
    return (
      <ListTag key={key} className={`${block.ordered ? "list-decimal" : "list-disc"} flex flex-col gap-1.5 pl-5 text-sm leading-relaxed text-ink-2`}>
        {block.items.map((item, itemIdx) => (
          <li key={`${key}-li-${itemIdx}`}>
            <span>{renderRichInline(item.text, `${key}-li-${itemIdx}`, mask)}</span>
            {item.children.length > 0 && (
              <ul className="mt-1 flex list-disc flex-col gap-1 pl-5 text-xs leading-relaxed text-ink-2">
                {item.children.map((sub, subIdx) => (
                  <li key={`${key}-sub-${itemIdx}-${subIdx}`}>
                    {renderRichInline(sub, `${key}-sub-${itemIdx}-${subIdx}`, mask)}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ListTag>
    );
  }

  return (
    <div key={key} className="overflow-x-auto rounded-md border border-line bg-surface-0">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-line bg-surface-2 text-ink-1">
            {block.headers.map((hdr, colIdx) => (
              <th key={`${key}-th-${colIdx}`} className={`px-3 py-2 font-semibold ${alignClass(block.alignments[colIdx] ?? "left")}`}>
                {renderRichInline(hdr, `${key}-th-${colIdx}`, mask)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line text-ink-2">
          {block.rows.map((row, rowIdx) => (
            <tr key={`${key}-tr-${rowIdx}`} className="hover:bg-surface-1">
              {row.map((cell, colIdx) => (
                <td key={`${key}-td-${rowIdx}-${colIdx}`} className={`px-3 py-2 align-top ${alignClass(block.alignments[colIdx] ?? "left")}`}>
                  {renderRichInline(cell, `${key}-td-${rowIdx}-${colIdx}`, mask)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const DocMarkdown: React.FC<{ readonly markdown: string }> = ({ markdown }) => {
  const { mask } = useSafeMode();
  const blocks = React.useMemo(() => parseDocBlocks(markdown), [markdown]);
  return <div className="flex min-w-0 flex-col gap-3">{blocks.map((b, i) => renderDocBlock(b, i, mask))}</div>;
};
