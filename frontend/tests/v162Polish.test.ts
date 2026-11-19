import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

import { sharedDisabledReason } from "../src/features/compare/compareModel";
import {
  MIN_FIT_SCALE,
  NODE_W,
  computeDiagramScale,
  layoutNodes,
  truncateEdgeLabel,
  truncateNodeLabel,
  truncateServiceLine,
} from "../src/features/levels/diagramModel";
import { parseInlineTokens } from "../src/features/releases/releasesModel";
import { maskUploadScopeSummary } from "../src/lib/safeMode";
import { architectureMenuLabel } from "../src/app/navMenuModel";
import type { ArchNode, LevelDescriptor } from "../src/types/arena";

const TOKENS_CSS_PATH = path.resolve(process.cwd(), "src/styles/tokens.css");

function srgbToLinear(channel: number): number {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(hex: string): number {
  const clean = hex.trim().replace(/^#/, "");
  assert.equal(clean.length, 6, `Expected 6-digit hex color, got ${hex}`);
  const r = Number.parseInt(clean.slice(0, 2), 16);
  const g = Number.parseInt(clean.slice(2, 4), 16);
  const b = Number.parseInt(clean.slice(4, 6), 16);
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

function contrastRatio(fg: string, bg: string): number {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  const [bright, dark] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (bright + 0.05) / (dark + 0.05);
}

function parseCssBlockVars(css: string, selector: string): Record<string, string> {
  const idx = css.indexOf(selector);
  assert.ok(idx >= 0, `Missing selector ${selector} in tokens.css`);
  const open = css.indexOf("{", idx);
  const close = css.indexOf("}", open);
  const body = css.slice(open + 1, close);
  const vars: Record<string, string> = {};
  for (const line of body.split("\n")) {
    const m = /^\s*(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;/.exec(line);
    if (m) vars[m[1]] = m[2].toLowerCase();
  }
  return vars;
}

test("tokens.css meets WCAG 2.1 AA (>= 4.5:1) across all light and dark surfaces", () => {
  const css = fs.readFileSync(TOKENS_CSS_PATH, "utf8");
  const light = parseCssBlockVars(css, ":root {");
  const dark = parseCssBlockVars(css, ":root.dark {");

  const surfaceKeys = ["--surface-0", "--surface-1", "--surface-2", "--winner-row-bg"];
  const textKeys = [
    "--ink-1",
    "--ink-2",
    "--ink-3",
    "--accent-moss",
    "--accent-clay",
    "--accent-ochre",
    "--accent-slate",
    "--status-ok",
    "--status-err",
  ];

  for (const [themeName, palette] of [
    ["light", light],
    ["dark", dark],
  ] as const) {
    for (const fgKey of textKeys) {
      const fg = palette[fgKey];
      assert.ok(fg, `Missing ${fgKey} in ${themeName}`);
      for (const bgKey of surfaceKeys) {
        const bg = palette[bgKey];
        assert.ok(bg, `Missing ${bgKey} in ${themeName}`);
        const ratio = contrastRatio(fg, bg);
        assert.ok(
          ratio >= 4.5,
          `${themeName} ${fgKey} (${fg}) on ${bgKey} (${bg}) is ${ratio.toFixed(2)}:1 (< 4.5:1 WCAG AA)`,
        );
      }
    }
  }
});

test("architectureMenuLabel shows 'Architectures' off-level and active short label on /levels/:id", () => {
  const levels: LevelDescriptor[] = [
    { level_id: "managed_vertex", label: "Level 1: Managed Vertex AI Search", order: 1, enabled: true, stages: [], architecture: { nodes: [], edges: [] } as unknown as LevelDescriptor["architecture"] },
    { level_id: "level4_agentic", label: "Level 8: Agentic RAG", order: 8, enabled: true, stages: [], architecture: { nodes: [], edges: [] } as unknown as LevelDescriptor["architecture"] },
  ];
  assert.equal(architectureMenuLabel("/", levels), "Architectures");
  assert.equal(architectureMenuLabel("/workbench", levels), "Architectures");
  assert.equal(architectureMenuLabel("/levels/managed_vertex", levels), "1 · Vertex Search");
  assert.equal(architectureMenuLabel("/levels/level4_agentic", levels), "8 · Agentic ADK");
});

test("Workbench 7-column SVG fits inside 1040px container without horizontal overflow via MIN_FIT_SCALE", () => {
  assert.equal(NODE_W, 168);
  assert.equal(MIN_FIT_SCALE, 0.72);
  const cols: ArchNode[] = Array.from({ length: 7 }, (_, i) => ({
    id: `n${i}`,
    label: `Stage ${i + 1}`,
    service: "Vertex AI",
    kind: "compute",
    col: i,
    row: 0,
    stage_ids: [`s${i}`],
  }));
  const layout = layoutNodes(cols, 1040);
  const scale = computeDiagramScale(1040, layout.width);
  assert.ok(scale >= MIN_FIT_SCALE && scale <= 1);
  assert.ok(Math.round(layout.width * scale) <= 1040, "7-col Workbench diagram must fit without horizontal overflow at 1040px");

  // Narrow mobile viewport clamps at MIN_FIT_SCALE rather than shrinking text microscopic
  assert.equal(computeDiagramScale(420, layout.width), MIN_FIT_SCALE);
  // Wide viewport scales up >= 1
  assert.ok(computeDiagramScale(1800, layout.width) > 1);

  assert.equal(truncateNodeLabel("User question + guardrail"), "User question + gua…");
  assert.equal(truncateNodeLabel("Pre-Guardrail Screen"), "Pre-Guardrail Screen");
  assert.equal(truncateServiceLine("default · Discovery Engine"), "default · Discovery Engine");
  assert.equal(truncateEdgeLabel("prepared query"), "prepared query");
});

test("parseInlineTokens parses nested inline code and safe links inside **strong** spans", () => {
  const tokens = parseInlineTokens("**In-App Releases (`/releases`) & [Compare](/compare)** ready.");
  assert.equal(tokens[0]?.kind, "strong");
  if (tokens[0]?.kind === "strong") {
    assert.ok(Array.isArray(tokens[0].children));
    const kinds = tokens[0].children.map((c) => c.kind);
    assert.deepEqual(kinds, ["text", "code", "text", "link"]);
    const codeTok = tokens[0].children[1];
    assert.deepEqual(codeTok, { kind: "code", text: "/releases" });
    const linkTok = tokens[0].children[3];
    assert.equal(linkTok?.kind, "link");
    if (linkTok?.kind === "link") {
      assert.equal(linkTok.label, "Compare");
      assert.equal(linkTok.href, "/compare");
    }
  }
});

test("sharedDisabledReason deduplicates identical Compare checklist warnings and preserves distinct ones", () => {
  const repeated = [
    { levelId: "managed_vertex", label: "Vertex Search", short: "Level 1", order: 1, disabledReason: "Replay plays recordings only: choose a golden question (question_id)." },
    { levelId: "rag_engine", label: "RAG Engine", short: "Level 2", order: 2, disabledReason: "Replay plays recordings only: choose a golden question (question_id)." },
  ];
  assert.equal(
    sharedDisabledReason(repeated),
    "Replay plays recordings only: choose a golden question (question_id).",
  );

  const distinct = [
    { levelId: "managed_vertex", label: "Vertex Search", short: "Level 1", order: 1, disabledReason: null },
    { levelId: "level4_agentic", label: "Agentic ADK", short: "Level 8", order: 8, disabledReason: "Level 8 (ADK agent) requires a Gemini synthesis model" },
  ];
  assert.equal(sharedDisabledReason(distinct), null);
});

test("maskUploadScopeSummary redacts upl_* workspace IDs and customer PDF filenames in SafeMode", () => {
  assert.equal(
    maskUploadScopeSummary("upl_858f18ea4e3d", ["Merger_TermSheet.pdf", "Pricing_Exhibit.pdf"], false),
    "upl_858f18ea4e3d (Merger_TermSheet.pdf, Pricing_Exhibit.pdf)",
  );
  assert.equal(
    maskUploadScopeSummary("upl_858f18ea4e3d", ["Merger_TermSheet.pdf", "Pricing_Exhibit.pdf"], true),
    "upl_•••••••••••• (2 uploaded PDFs — masked)",
  );
  assert.equal(
    maskUploadScopeSummary("upl_858f18ea4e3d", ["Single.pdf"], true),
    "upl_•••••••••••• (1 uploaded PDF — masked)",
  );
});
