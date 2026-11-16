import assert from "node:assert/strict";
import { test } from "node:test";
import {
  filterReleases,
  parseDocBlocks,
  parseInlineTokens,
  parseReleasesTab,
  sanitizeDocHref,
  slugifyHeading,
  visibleRouteLinks,
} from "../src/features/releases/releasesModel";
import { maskDeep } from "../src/lib/safeMode";
import type { ReleaseEntry } from "../src/types/arena";

const SAMPLE_RELEASES: readonly ReleaseEntry[] = [
  {
    version: "1.5.0",
    date: "2026-11-16",
    compare_url: "https://github.com/example/rag-arena/compare/v1.4.0...v1.5.0",
    summary: "In-app Releases & Docs tab (/releases) and release-please automation.",
    sections: [
      {
        heading: "Added",
        items: [
          "**Releases & Docs (`/releases`)**: timeline + `ARCHITECTURE.md` reader.\n  - Nested sub-bullet detail.",
        ],
      },
    ],
    route_links: [
      { path: "/workbench", label: "Workbench" },
      { path: "/ingest", label: "Ingest" },
      { path: "/levels/alloydb_scann", label: "L7 AlloyDB ScaNN" },
    ],
  },
  {
    version: "1.4.0",
    date: "2026-11-04",
    compare_url: "",
    summary: "Ephemeral ScaNN benchmarks on AlloyDB and Vertex AI Vector Search 2.0.",
    sections: [{ heading: "Added", items: ["Level 7 `alloydb_scann` and Level 8 `vertex_vector_v2`."] }],
    route_links: [{ path: "/levels/alloydb_scann", label: "L7 AlloyDB ScaNN" }],
  },
];

test("sanitizeDocHref blocks unsafe schemes and maps repo doc links to in-app /releases tabs", () => {
  assert.equal(sanitizeDocHref("javascript:alert(1)"), null);
  assert.equal(sanitizeDocHref("  JaVaScRiPt:void(0) "), null);
  assert.equal(sanitizeDocHref("data:text/html,<script>alert(1)</script>"), null);
  assert.equal(sanitizeDocHref("vbscript:msgbox(1)"), null);

  assert.deepEqual(sanitizeDocHref("#0-design-principles-v3"), {
    href: "#0-design-principles-v3",
    external: false,
    internalRoute: false,
  });
  assert.deepEqual(sanitizeDocHref("/workbench?preset=rag_engine"), {
    href: "/workbench?preset=rag_engine",
    external: false,
    internalRoute: true,
  });
  assert.deepEqual(sanitizeDocHref("docs/ARCHITECTURE.md#3b-level-2-query-path-engineslevel2_hybridpy"), {
    href: "/releases?tab=architecture#3b-level-2-query-path-engineslevel2_hybridpy",
    external: false,
    internalRoute: true,
  });
  assert.deepEqual(sanitizeDocHref("SETUP_GUIDE.md#6-cloud-run"), {
    href: "/releases?tab=setup_guide#6-cloud-run",
    external: false,
    internalRoute: true,
  });
  assert.deepEqual(sanitizeDocHref("CHANGELOG.md"), {
    href: "/releases?tab=timeline",
    external: false,
    internalRoute: true,
  });
  assert.deepEqual(sanitizeDocHref("https://keepachangelog.com/en/1.1.0/"), {
    href: "https://keepachangelog.com/en/1.1.0/",
    external: true,
    internalRoute: false,
  });
});

test("parseDocBlocks parses headings with deduplicated slugs, tables, code fences, blockquotes, and nested lists", () => {
  const md = [
    "# System Architecture",
    "",
    "> [!TIP] Zero-secret automation on `main`.",
    "",
    "## 0. Design principles (v3)",
    "",
    "| # | Principle | Code |",
    "| :-- | :-: | --: |",
    "| P1 | **Measured** | `telemetry.py` |",
    "",
    "```bash",
    "## comment inside code fence is not a heading",
    "scripts/build_docs_manifest.py --check",
    "```",
    "",
    "- Top item one",
    "  - Nested detail A",
    "  - Nested detail B",
    "- Top item two",
    "",
    "### Handler",
    "### Handler",
  ].join("\n");

  const blocks = parseDocBlocks(md);
  const headings = blocks.filter((b) => b.kind === "heading");
  assert.deepEqual(
    headings.map((h) => ({ level: h.level, id: h.id, text: h.text })),
    [
      { level: 1, id: "system-architecture", text: "System Architecture" },
      { level: 2, id: "0-design-principles-v3", text: "0. Design principles (v3)" },
      { level: 3, id: "handler", text: "Handler" },
      { level: 3, id: "handler-2", text: "Handler" },
    ],
  );

  const table = blocks.find((b) => b.kind === "table");
  assert.ok(table && table.kind === "table");
  assert.deepEqual(table.headers, ["#", "Principle", "Code"]);
  assert.deepEqual(table.alignments, ["left", "center", "right"]);
  assert.deepEqual(table.rows, [["P1", "**Measured**", "`telemetry.py`"]]);

  const code = blocks.find((b) => b.kind === "code");
  assert.ok(code && code.kind === "code");
  assert.equal(code.language, "bash");
  assert.ok(code.code.includes("build_docs_manifest.py --check"));

  const list = blocks.find((b) => b.kind === "list");
  assert.ok(list && list.kind === "list");
  assert.equal(list.items.length, 2);
  assert.equal(list.items[0].text, "Top item one");
  assert.deepEqual(list.items[0].children, ["Nested detail A", "Nested detail B"]);

  assert.equal(slugifyHeading("3b. Level 2 query path (`engines/level2_hybrid.py`)"), "3b-level-2-query-path-engineslevel2_hybridpy");
});

test("parseInlineTokens parses bold, inline code, and links without raw HTML", () => {
  const tokens = parseInlineTokens("See **Level 7** in `alloydb_scann.py` and [Setup](docs/SETUP_GUIDE.md#1-prerequisites) or <script>alert(1)</script>.");
  assert.ok(tokens.some((t) => t.kind === "strong" && t.text === "Level 7"));
  assert.ok(tokens.some((t) => t.kind === "code" && t.text === "alloydb_scann.py"));
  const link = tokens.find((t) => t.kind === "link");
  assert.ok(link && link.kind === "link");
  assert.equal(link.label, "Setup");
  assert.equal(link.href, "/releases?tab=setup_guide#1-prerequisites");
  // Raw <script> stays plain text token (escaped automatically by React JSX).
  assert.ok(tokens.some((t) => t.kind === "text" && t.text.includes("<script>alert(1)</script>")));
});

test("filterReleases, parseReleasesTab, visibleRouteLinks, and SafeMode masking behave deterministically", () => {
  assert.equal(parseReleasesTab("?tab=architecture"), "architecture");
  assert.equal(parseReleasesTab("?tab=setup_guide"), "setup_guide");
  assert.equal(parseReleasesTab("?tab=setup"), "setup_guide");
  assert.equal(parseReleasesTab("?tab=unknown"), "timeline");
  assert.equal(parseReleasesTab(""), "timeline");

  assert.equal(filterReleases(SAMPLE_RELEASES, "").length, 2);
  assert.deepEqual(
    filterReleases(SAMPLE_RELEASES, "release-please").map((r) => r.version),
    ["1.5.0"],
  );
  assert.deepEqual(
    filterReleases(SAMPLE_RELEASES, "vector_v2").map((r) => r.version),
    ["1.4.0"],
  );

  // Replay mode hides /ingest and gates disabled levels; live mode gates un-enabled ephemeral levels.
  const replayLinks = visibleRouteLinks(SAMPLE_RELEASES[0].route_links, "replay", ["level1_naive", "alloydb_scann"]);
  assert.deepEqual(
    replayLinks.map((l) => l.path),
    ["/workbench", "/levels/alloydb_scann"],
  );
  const liveStandingLinks = visibleRouteLinks(SAMPLE_RELEASES[0].route_links, "live", ["level1_naive"]);
  assert.deepEqual(
    liveStandingLinks.map((l) => l.path),
    ["/workbench", "/ingest"],
  );

  const masked = maskDeep(
    { summary: "Deployed in acme-proj-99 with gs://acme-proj-99-uploads and projects/76543/locations/us" },
    { project_id: "acme-proj-99", upload_bucket: "acme-proj-99-uploads", gcs_bucket: "acme-proj-99-corpus" },
  );
  assert.equal(
    masked.summary,
    "Deployed in ${GCP_PROJECT_ID} with gs://${UPLOAD_BUCKET} and projects/${GCP_PROJECT_NUMBER}/locations/us",
  );
});
