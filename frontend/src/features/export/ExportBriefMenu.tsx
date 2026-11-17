import React, { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Download, FileSpreadsheet, FileText, Printer } from "lucide-react";
import { useAppData } from "../../app/AppDataProvider";
import { useSafeMode } from "../../app/SafeMode";
import type { IdentitySource } from "../../lib/safeMode";
import type { LeaderboardGroup } from "../../pages/leaderboardModel";
import type { EngineTimeline } from "../../types/arena";
import type { ScoreRow } from "../compare/compareModel";
import {
  buildExecutiveCsv,
  buildExecutiveMarkdown,
  fromCompareRows,
  fromLeaderboardGroups,
  summarizeRecommendation,
  type ExecutiveBriefModel,
  type FromCompareMeta,
  type FromLeaderboardMeta,
} from "./executiveBrief";

export interface ExportBriefLeaderboardInput extends FromLeaderboardMeta {
  readonly groups: readonly LeaderboardGroup[];
}

export interface ExportBriefCompareInput extends FromCompareMeta {
  readonly rows: readonly ScoreRow[];
  readonly timelines?: Readonly<Record<string, EngineTimeline>>;
}

export interface ExportBriefMenuProps {
  /** Pre-built brief model, OR pass `leaderboard` / `compare` directly for 1-line mounting. */
  readonly brief?: ExecutiveBriefModel;
  readonly leaderboard?: ExportBriefLeaderboardInput;
  readonly compare?: ExportBriefCompareInput;
  readonly filenamePrefix?: string;
  readonly health?: IdentitySource | null;
  readonly disabled?: boolean;
}

function useOptionalHealth(explicit?: IdentitySource | null): {
  readonly health: IdentitySource | null;
  readonly demoMode: string | null;
} {
  let ctxHealth: IdentitySource | null = null;
  let ctxDemoMode: string | null = null;
  try {
    const appData = useAppData();
    ctxHealth = appData.health;
    ctxDemoMode = appData.health?.demo_mode ?? null;
  } catch {
    ctxHealth = null;
    ctxDemoMode = null;
  }
  return {
    health: explicit !== undefined ? explicit : ctxHealth,
    demoMode: ctxDemoMode,
  };
}

function sanitizeFilenameSlug(raw: string): string {
  const cleaned = raw
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return cleaned || "rag-arena-executive-brief";
}

function triggerBlobDownload(content: string, mimeType: string, filename: string): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Self-contained One-Click Executive Decision Brief Export control (`.md`, `.csv`, `window.print()`).
 * Also renders a `hidden print:block` executive summary banner that appears only on printed / saved PDFs.
 *
 * Mount snippet for `LeaderboardPage.tsx`:
 * ```tsx
 * <ExportBriefMenu
 *   leaderboard={{ groups, source, scope, questionId, generatedAt: data?.generated_at }}
 * />
 * ```
 *
 * Mount snippet for `ComparePage.tsx`:
 * ```tsx
 * <ExportBriefMenu
 *   compare={{
 *     rows,
 *     timelines: stream.state.timelines,
 *     runId: stream.state.runId,
 *     questionId: effectiveQid,
 *     query: question.query,
 *     synthesisModel: models.selection?.synthesis,
 *     judgeModel: models.selection?.judge,
 *   }}
 * />
 * ```
 */
export const ExportBriefMenu: React.FC<ExportBriefMenuProps> = ({
  brief,
  leaderboard,
  compare,
  filenamePrefix,
  health: explicitHealth,
  disabled = false,
}) => {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const { enabled: safeMode, mask } = useSafeMode();
  const { health, demoMode } = useOptionalHealth(explicitHealth);

  const resolvedModel = useMemo<ExecutiveBriefModel>(() => {
    if (brief) return brief;
    if (leaderboard) {
      return fromLeaderboardGroups(leaderboard.groups, {
        ...leaderboard,
        demoMode: leaderboard.demoMode ?? demoMode,
      });
    }
    if (compare) {
      return fromCompareRows(compare.rows, {
        ...compare,
        demoMode: compare.demoMode ?? demoMode,
      });
    }
    return fromLeaderboardGroups([], { demoMode });
  }, [brief, leaderboard, compare, demoMode]);

  const redactOpts = useMemo(
    () => ({ safeMode, health, mask }),
    [safeMode, health, mask],
  );

  const printRecommendation = useMemo(
    () => summarizeRecommendation(resolvedModel, redactOpts),
    [resolvedModel, redactOpts],
  );

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const dateStamp = resolvedModel.generatedAt.slice(0, 10).replace(/[^0-9-]/g, "") || "brief";
  const prefix = sanitizeFilenameSlug(
    filenamePrefix ??
      (resolvedModel.sourceKind === "compare" ? "rag-arena-battle-brief" : "rag-arena-cohort-brief"),
  );

  const onDownloadMarkdown = (): void => {
    const md = buildExecutiveMarkdown(resolvedModel, redactOpts);
    triggerBlobDownload(md, "text/markdown;charset=utf-8", `${prefix}-${dateStamp}.md`);
    setOpen(false);
  };

  const onDownloadCsv = (): void => {
    const csv = buildExecutiveCsv(resolvedModel, redactOpts);
    triggerBlobDownload(csv, "text/csv;charset=utf-8", `${prefix}-${dateStamp}.csv`);
    setOpen(false);
  };

  const onPrintPdf = (): void => {
    setOpen(false);
    if (typeof window !== "undefined" && typeof window.print === "function") {
      window.setTimeout(() => window.print(), 20);
    }
  };

  return (
    <>
      <div
        ref={containerRef}
        className="relative inline-block"
        data-print-hide="true"
        data-testid="export-brief-menu"
      >
        <button
          type="button"
          disabled={disabled}
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
          data-testid="export-brief-trigger"
          className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2.5 py-1 text-xs font-medium text-ink-1 transition-colors hover:border-ink-3 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Download className="h-3.5 w-3.5" aria-hidden />
          <span>Export Brief</span>
          <ChevronDown className="h-3.5 w-3.5 text-ink-3" aria-hidden />
        </button>

        {open && (
          <div
            role="menu"
            aria-label="Export executive decision brief"
            className="absolute right-0 z-30 mt-1.5 w-64 rounded-lg border border-line bg-surface-1 p-1 shadow-lg"
          >
            <button
              type="button"
              role="menuitem"
              onClick={onDownloadMarkdown}
              data-testid="export-brief-md"
              className="flex w-full items-start gap-2 rounded-md px-2.5 py-2 text-left text-xs text-ink-1 transition-colors hover:bg-surface-2"
            >
              <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />
              <span className="flex flex-col">
                <span className="font-medium">Download Markdown Brief (.md)</span>
                <span className="text-ink-3">Recommendation, split costs, GCP stack &amp; TCO</span>
              </span>
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={onDownloadCsv}
              data-testid="export-brief-csv"
              className="flex w-full items-start gap-2 rounded-md px-2.5 py-2 text-left text-xs text-ink-1 transition-colors hover:bg-surface-2"
            >
              <FileSpreadsheet className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />
              <span className="flex flex-col">
                <span className="font-medium">Download CSV Metrics (.csv)</span>
                <span className="text-ink-3">Spreadsheet-safe QA, latency &amp; Engine/Judge $</span>
              </span>
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={onPrintPdf}
              data-testid="export-brief-print"
              className="flex w-full items-start gap-2 rounded-md px-2.5 py-2 text-left text-xs text-ink-1 transition-colors hover:bg-surface-2"
            >
              <Printer className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />
              <span className="flex flex-col">
                <span className="font-medium">Print / Save PDF Brief</span>
                <span className="text-ink-3">Clean report layout without navigation chrome</span>
              </span>
            </button>
          </div>
        )}
      </div>

      <section
        aria-label="Printed executive decision summary"
        data-print-only="true"
        className="hidden rounded-md border border-line bg-surface-0 p-3 text-xs text-ink-1 print:block"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-1.5">
          <strong className="text-sm font-semibold">{resolvedModel.title}</strong>
          <span className="font-mono text-xs text-ink-2">
            {resolvedModel.generatedAt.slice(0, 19).replace("T", " ")} UTC
            {safeMode ? " · SafeMode Redacted" : ""}
          </span>
        </div>
        <p className="mt-1.5 leading-relaxed text-ink-1">{printRecommendation}</p>
        <p className="mt-1 text-xs text-ink-2">
          TCO Posture: Levels 1–6 run at $0/mo serverless idle (BigQuery / Vertex AI Search / RagManagedDb); Levels 7–8
          (AlloyDB ScaNN &amp; Vector Search 2.0) provide dedicated sub-second ANN clusters for high-volume workloads.
        </p>
      </section>
    </>
  );
};
