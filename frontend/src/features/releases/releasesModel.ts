/**
 * Pure domain model & dependency-free AST Markdown parser for the `/releases` page.
 *
 * Security invariants:
 * - Zero raw HTML injection (emits React JSX elements only; innerHTML is never used).
 * - Strict URL scheme allowlist (`#`, `/`, `https://`, `http://`) in `sanitizeDocHref`.
 * - Bounded loops (`MAX_DOC_LINES`, `MAX_INLINE_PARTS`) with linear single-pass parsing.
 */

import type { ReleaseEntry, RouteDeepLink } from "../../types/arena";

export type ReleasesTab = "timeline" | "architecture" | "setup_guide";
export type TableAlign = "left" | "center" | "right";

export interface SafeDocHref {
  readonly href: string;
  readonly external: boolean;
  readonly internalRoute: boolean;
}

export type InlineToken =
  | { readonly kind: "text"; readonly text: string }
  | { readonly kind: "strong"; readonly text: string }
  | { readonly kind: "code"; readonly text: string }
  | { readonly kind: "link"; readonly label: string; readonly href: string; readonly external: boolean; readonly internalRoute: boolean };

export interface DocListItem {
  readonly text: string;
  readonly children: readonly string[];
}

export type DocBlock =
  | { readonly kind: "heading"; readonly level: 1 | 2 | 3 | 4; readonly id: string; readonly text: string }
  | { readonly kind: "paragraph"; readonly text: string }
  | { readonly kind: "blockquote"; readonly text: string }
  | { readonly kind: "code"; readonly language: string; readonly code: string }
  | { readonly kind: "list"; readonly ordered: boolean; readonly items: readonly DocListItem[] }
  | { readonly kind: "table"; readonly headers: readonly string[]; readonly alignments: readonly TableAlign[]; readonly rows: readonly (readonly string[])[] }
  | { readonly kind: "hr" };

const MAX_DOC_LINES = 8_000;
const MAX_INLINE_PARTS = 512;
const NON_SLUG_RE = /[^a-z0-9_ -]/g;
const SPACE_HYPHEN_RE = /[\s-]+/g;
const HEADING_RE = /^(#{1,4})\s+(.+?)\s*$/;
const FENCE_RE = /^```([A-Za-z0-9_+-]*)\s*$/;
const HR_RE = /^(?:-{3,}|\*{3,}|_{3,})\s*$/;
const TOP_UL_RE = /^[*-]\s+(.*)$/;
const TOP_OL_RE = /^\d+[.)]\s+(.*)$/;
const NESTED_ITEM_RE = /^\s{2,}(?:[*-]|\d+[.)])\s+(.*)$/;
const TABLE_SEP_CELL_RE = /^:?-+:?$/;
const INLINE_TOKEN_RE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\))/g;
const LINK_PART_RE = /^\[([^\]\n]+)\]\(([^)\s]+)\)$/;

/** Converts a Markdown heading title into a deterministic GitHub-compatible anchor slug. */
export function slugifyHeading(title: string): string {
  const cleaned = title.trim().toLowerCase().replace(/`/g, "").replace(NON_SLUG_RE, "");
  const slug = cleaned.replace(SPACE_HYPHEN_RE, "-").replace(/^-+|-+$/g, "");
  return slug || "section";
}

/** Validates and normalizes Markdown link targets, mapping repo doc links to in-app `/releases` tabs. */
export function sanitizeDocHref(rawHref: string): SafeDocHref | null {
  const trimmed = rawHref.trim();
  if (!trimmed || trimmed.length > 1024) return null;

  if (trimmed.startsWith("#")) {
    return trimmed.length > 1 ? { href: trimmed, external: false, internalRoute: false } : null;
  }
  if (trimmed.startsWith("/") && !trimmed.startsWith("//")) {
    return { href: trimmed, external: false, internalRoute: true };
  }

  const hashIdx = trimmed.indexOf("#");
  const pathPart = (hashIdx >= 0 ? trimmed.slice(0, hashIdx) : trimmed).replace(/^\.\//, "");
  const hashSuffix = hashIdx >= 0 ? trimmed.slice(hashIdx) : "";
  if (pathPart === "CHANGELOG.md") {
    return { href: `/releases?tab=timeline${hashSuffix}`, external: false, internalRoute: true };
  }
  if (pathPart === "ARCHITECTURE.md" || pathPart === "docs/ARCHITECTURE.md") {
    return { href: `/releases?tab=architecture${hashSuffix}`, external: false, internalRoute: true };
  }
  if (pathPart === "SETUP_GUIDE.md" || pathPart === "docs/SETUP_GUIDE.md") {
    return { href: `/releases?tab=setup_guide${hashSuffix}`, external: false, internalRoute: true };
  }

  const lower = trimmed.toLowerCase();
  if (lower.startsWith("https://") || lower.startsWith("http://")) {
    return { href: trimmed, external: true, internalRoute: false };
  }
  return null;
}

/** Tokenizes inline bold, monospace code, and safe links without interpreting raw HTML. */
export function parseInlineTokens(text: string): InlineToken[] {
  if (!text) return [];
  const parts = text.split(INLINE_TOKEN_RE).slice(0, MAX_INLINE_PARTS);
  const out: InlineToken[] = [];
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      out.push({ kind: "strong", text: part.slice(2, -2) });
      continue;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      out.push({ kind: "code", text: part.slice(1, -1) });
      continue;
    }
    const linkMatch = LINK_PART_RE.exec(part);
    if (linkMatch) {
      const safe = sanitizeDocHref(linkMatch[2]);
      if (safe) {
        out.push({ kind: "link", label: linkMatch[1], ...safe });
        continue;
      }
    }
    out.push({ kind: "text", text: part });
  }
  return out;
}

function splitTableCells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((c) => c.trim());
}

function parseTableAlignments(sepLine: string, expectedCols: number): TableAlign[] | null {
  if (!sepLine.includes("|")) return null;
  const cells = splitTableCells(sepLine);
  if (cells.length !== expectedCols || cells.length === 0) return null;
  const aligns: TableAlign[] = [];
  for (const cell of cells) {
    if (!TABLE_SEP_CELL_RE.test(cell)) return null;
    if (cell.startsWith(":") && cell.endsWith(":")) aligns.push("center");
    else if (cell.endsWith(":")) aligns.push("right");
    else aligns.push("left");
  }
  return aligns;
}

function consumeCodeFence(lines: readonly string[], startIdx: number, language: string): { block: DocBlock; nextIdx: number } {
  const buf: string[] = [];
  let i = startIdx + 1;
  while (i < lines.length && !lines[i].trim().startsWith("```")) {
    buf.push(lines[i]);
    i += 1;
  }
  return {
    block: { kind: "code", language, code: buf.join("\n") },
    nextIdx: i < lines.length ? i + 1 : i,
  };
}

function consumeTable(lines: readonly string[], startIdx: number, alignments: TableAlign[]): { block: DocBlock; nextIdx: number } {
  const headers = splitTableCells(lines[startIdx]);
  const rows: string[][] = [];
  let i = startIdx + 2;
  while (i < lines.length && lines[i].trim().startsWith("|")) {
    const cells = splitTableCells(lines[i]);
    rows.push(headers.map((_, colIdx) => cells[colIdx] ?? ""));
    i += 1;
  }
  return { block: { kind: "table", headers, alignments, rows }, nextIdx: i };
}

function consumeList(lines: readonly string[], startIdx: number, ordered: boolean): { block: DocBlock; nextIdx: number } {
  const items: { text: string; children: string[] }[] = [];
  let i = startIdx;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) break;
    const topMatch = (ordered ? TOP_OL_RE : TOP_UL_RE).exec(line);
    if (topMatch) {
      items.push({ text: topMatch[1].trim(), children: [] });
      i += 1;
      continue;
    }
    const nested = NESTED_ITEM_RE.exec(line);
    if (nested && items.length > 0) {
      items[items.length - 1].children.push(nested[1].trim());
      i += 1;
      continue;
    }
    if (/^\s{2,}\S/.test(line) && items.length > 0) {
      const current = items[items.length - 1];
      if (current.children.length > 0) {
        current.children[current.children.length - 1] += ` ${line.trim()}`;
      } else {
        current.text += ` ${line.trim()}`;
      }
      i += 1;
      continue;
    }
    break;
  }
  return { block: { kind: "list", ordered, items }, nextIdx: i };
}

function clampHeadingLevel(len: number): 1 | 2 | 3 | 4 {
  if (len <= 1) return 1;
  if (len === 2) return 2;
  if (len === 3) return 3;
  return 4;
}

/** Parses a Markdown document into typed block AST nodes with deduplicated heading slugs. */
export function parseDocBlocks(markdown: string): DocBlock[] {
  const lines = markdown.replace(/\r/g, "").split("\n").slice(0, MAX_DOC_LINES);
  const blocks: DocBlock[] = [];
  const slugCounts = new Map<string, number>();
  let i = 0;

  while (i < lines.length) {
    const raw = lines[i].trimEnd();
    const trimmed = raw.trim();
    if (!trimmed) {
      i += 1;
      continue;
    }

    const fence = FENCE_RE.exec(trimmed);
    if (fence) {
      const res = consumeCodeFence(lines, i, fence[1] || "");
      blocks.push(res.block);
      i = res.nextIdx;
      continue;
    }

    const heading = HEADING_RE.exec(raw);
    if (heading) {
      const level = clampHeadingLevel(heading[1].length);
      const text = heading[2].trim();
      const base = slugifyHeading(text);
      const nextCount = (slugCounts.get(base) ?? 0) + 1;
      slugCounts.set(base, nextCount);
      blocks.push({ kind: "heading", level, id: nextCount === 1 ? base : `${base}-${nextCount}`, text });
      i += 1;
      continue;
    }

    if (HR_RE.test(trimmed)) {
      blocks.push({ kind: "hr" });
      i += 1;
      continue;
    }

    if (trimmed.startsWith(">")) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, "").replace(/^\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i, ""));
        i += 1;
      }
      blocks.push({ kind: "blockquote", text: quoteLines.filter(Boolean).join(" ") });
      continue;
    }

    if (trimmed.includes("|") && i + 1 < lines.length) {
      const headerCount = splitTableCells(raw).length;
      const aligns = parseTableAlignments(lines[i + 1], headerCount);
      if (aligns) {
        const res = consumeTable(lines, i, aligns);
        blocks.push(res.block);
        i = res.nextIdx;
        continue;
      }
    }

    if (TOP_UL_RE.test(raw)) {
      const res = consumeList(lines, i, false);
      blocks.push(res.block);
      i = res.nextIdx;
      continue;
    }
    if (TOP_OL_RE.test(raw)) {
      const res = consumeList(lines, i, true);
      blocks.push(res.block);
      i = res.nextIdx;
      continue;
    }

    const para: string[] = [trimmed];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !FENCE_RE.test(lines[i].trim()) &&
      !HEADING_RE.test(lines[i].trimEnd()) &&
      !HR_RE.test(lines[i].trim()) &&
      !lines[i].trim().startsWith(">") &&
      !TOP_UL_RE.test(lines[i].trimEnd()) &&
      !TOP_OL_RE.test(lines[i].trimEnd())
    ) {
      para.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ kind: "paragraph", text: para.join(" ") });
  }
  return blocks;
}

/** Parses `?tab=` search param into a valid `ReleasesTab`. */
export function parseReleasesTab(search: string): ReleasesTab {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const raw = (params.get("tab") ?? "").trim().toLowerCase();
  if (raw === "architecture") return "architecture";
  if (raw === "setup_guide" || raw === "setup") return "setup_guide";
  return "timeline";
}

/** Filters releases case-insensitively across version, date, summary, and section items. */
export function filterReleases(releases: readonly ReleaseEntry[], query: string): readonly ReleaseEntry[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return releases;
  return releases.filter((rel) => {
    if (rel.version.toLowerCase().includes(needle) || rel.date.toLowerCase().includes(needle)) return true;
    if (rel.summary.toLowerCase().includes(needle)) return true;
    return rel.sections.some(
      (sec) =>
        sec.heading.toLowerCase().includes(needle) ||
        sec.items.some((item) => item.toLowerCase().includes(needle)),
    );
  });
}

/** Filters route deep-link pills to routes active in the current runtime mode and `enabled_levels`. */
export function visibleRouteLinks(
  links: readonly RouteDeepLink[],
  demoMode: string,
  enabledLevels: readonly string[],
): readonly RouteDeepLink[] {
  const enabledSet = new Set(enabledLevels);
  return links.filter((link) => {
    if (link.path === "/ingest" && demoMode === "replay") return false;
    if (link.path.startsWith("/levels/")) {
      const levelId = link.path.slice("/levels/".length);
      return enabledSet.has(levelId);
    }
    return true;
  });
}
