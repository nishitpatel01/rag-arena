import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { X } from "lucide-react";
import type { Citation } from "../types/arena";
import {
  ZOOM_LEVELS,
  clampPage,
  dpiForZoom,
  fetchHighlights,
  highlightLabel,
  highlightSource,
  highlightsUrl,
  pageImageUrl,
  type HighlightResponse,
  type ZoomLevel,
} from "../lib/documents";
import { citationRegion, layoutSourceLabel, transcriptionTitle } from "../lib/elements";
import { KeyValue } from "./ui/KeyValue";
import { ElementTypeBadge, LayoutSourceBadge } from "./ElementBadges";
import { useSafeMode } from "../app/SafeMode";
import { PageCanvas } from "./PageCanvas";

const boxText = (b: { x0: number; y0: number; x1: number; y1: number }): string =>
  `[${b.x0.toFixed(3)}, ${b.y0.toFixed(3)}, ${b.x1.toFixed(3)}, ${b.y1.toFixed(3)}]`;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Highlights for a citation's cited page (cached per URL); error text is surfaced verbatim. */
export function useCitationHighlights(citation: Citation | null): { readonly data: HighlightResponse | null; readonly error: string | null } {
  const [data, setData] = useState<HighlightResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const url = citation ? highlightsUrl(citation, citation.page_number) : null;
  useEffect(() => {
    setData(null);
    setError(null);
    if (!url) return undefined;
    const controller = new AbortController();
    fetchHighlights(url, controller.signal)
      .then((body) => {
        if (!controller.signal.aborted) setData(body);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
      });
    return () => controller.abort();
  }, [url]);
  return { data, error };
}

export interface DocumentDrawerProps {
  readonly citation: Citation | null;
  readonly open: boolean;
  readonly onClose: () => void;
}

/** Right-side slide-over dialog with the real cited page, honest highlight provenance, page nav and zoom. */
export const DocumentDrawer: React.FC<DocumentDrawerProps> = ({ citation, open, onClose }) => {
  if (!open || !citation) return null;
  return <DrawerBody key={citation.chunk_id} citation={citation} onClose={onClose} />;
};

function useFocusTrap(ref: React.RefObject<HTMLElement>, onClose: () => void): void {
  useEffect(() => {
    const restoreTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !ref.current) return;
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const inside = ref.current.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      restoreTo?.focus();
    };
  }, [ref, onClose]);
}

const DrawerBody: React.FC<{ readonly citation: Citation; readonly onClose: () => void }> = ({ citation, onClose }) => {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState(citation.page_number);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [zoom, setZoom] = useState<ZoomLevel>(100);
  const hl = useCitationHighlights(citation);
  const { mask } = useSafeMode();
  useFocusTrap(panelRef, onClose);

  useEffect(() => {
    if (hl.data?.page_count) setPageCount(hl.data.page_count);
  }, [hl.data]);

  const onPageCount = useCallback((n: number) => setPageCount(n), []);
  const source = highlightSource(page, citation.page_number, hl.data, hl.error);
  const aspect = hl.data && page === citation.page_number ? hl.data.page_width / hl.data.page_height : null;
  const go = (delta: number): void => setPage((p) => clampPage(p + delta, pageCount));
  const bboxText = `[${citation.bbox.x0.toFixed(3)}, ${citation.bbox.y0.toFixed(3)}, ${citation.bbox.x1.toFixed(3)}, ${citation.bbox.y1.toFixed(3)}]`;
  // Level 3: table / diagram citations carry a Document AI / Gemini vision region (honestly labelled).
  const region = citationRegion(citation);
  const transcription = transcriptionTitle(citation);
  const regionLabel = region && page === citation.page_number ? region.label : null;

  return (
    <div className="fixed inset-0 z-50" role="presentation">
      <div
        className="absolute inset-0"
        style={{ backgroundColor: "color-mix(in srgb, var(--ink-1) 35%, transparent)" }}
        onClick={onClose}
        aria-hidden
        data-testid="drawer-backdrop"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`drawer-slide-in absolute inset-y-0 right-0 flex ${transcription ? "w-[min(76rem,94vw)]" : "w-[min(56rem,90vw)]"} flex-col border-l border-line bg-surface-1 shadow-2xl`}
      >
        <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <h2 id={titleId} className="truncate text-sm font-semibold text-ink-1">
              {citation.doc_name} · p.{citation.page_number}
            </h2>
            <p className="font-mono text-xs text-slate">
              {citation.solicitation_id} · {citation.section_id || "no section_id"} · {citation.element_type}
            </p>
            {(region || citation.layout_source) && (
              <div className="mt-1 flex flex-wrap gap-1.5">
                <ElementTypeBadge elementType={citation.element_type} />
                {citation.layout_source && <LayoutSourceBadge layoutSource={citation.layout_source} />}
              </div>
            )}
          </div>
          <button
            type="button"
            data-autofocus
            onClick={onClose}
            className="rounded-md border border-line p-1.5 text-ink-2 hover:bg-surface-2"
            aria-label="Close document viewer"
          >
            <X size={16} aria-hidden />
          </button>
        </header>

        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-xs">
          <div className="flex items-center gap-1" role="group" aria-label="Page navigation">
            <button type="button" className="rounded-md border border-line px-2 py-1 text-ink-2 hover:bg-surface-2 disabled:opacity-40" onClick={() => go(-1)} disabled={page <= 1}>
              ‹ Prev
            </button>
            <span className="px-1 font-mono text-ink-1" aria-live="polite">
              p.{page}
              {pageCount ? ` / ${pageCount}` : ""}
            </span>
            <button
              type="button"
              className="rounded-md border border-line px-2 py-1 text-ink-2 hover:bg-surface-2 disabled:opacity-40"
              onClick={() => go(1)}
              disabled={pageCount === null || page >= pageCount}
            >
              Next ›
            </button>
            {page !== citation.page_number && (
              <button type="button" className="ml-1 text-moss underline" onClick={() => setPage(citation.page_number)}>
                back to cited p.{citation.page_number}
              </button>
            )}
          </div>
          <div className="ml-auto flex items-center gap-1" role="group" aria-label="Zoom">
            {ZOOM_LEVELS.map((z) => (
              <button
                key={z}
                type="button"
                aria-pressed={zoom === z}
                onClick={() => setZoom(z)}
                className={`rounded-md border px-2 py-1 font-mono ${zoom === z ? "border-moss text-moss" : "border-line text-ink-2 hover:bg-surface-2"}`}
              >
                {z}%
              </button>
            ))}
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4">
          <p
            className={`shrink-0 text-xs ${source === "text_match" ? "text-moss" : source === "lookup_failed" ? "text-err" : source === "stored_bbox" ? "text-clay" : "text-ink-3"}`}
            role="status"
            data-testid="highlight-label"
          >
            {highlightLabel(source, regionLabel)}
            {source === "other_page" ? ` (p.${citation.page_number}).` : ""}
            {source === "lookup_failed" && hl.error ? <span className="block break-words font-mono">{mask(hl.error)}</span> : null}
          </p>
          <div className={transcription ? "grid shrink-0 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(16rem,22rem)]" : "shrink-0"}>
            <div className="min-w-0 overflow-x-auto pt-5">
              <div style={{ width: `${zoom}%` }} className="min-w-full">
                <PageCanvas
                  imageUrl={pageImageUrl(citation, page, dpiForZoom(zoom))}
                  alt={`${citation.doc_name} page ${page} (rendered from the local corpus)`}
                  aspectHint={aspect}
                  source={source}
                  rects={hl.data?.rects ?? []}
                  storedBbox={citation.bbox}
                  region={region}
                  onPageCount={onPageCount}
                />
              </div>
            </div>
            {transcription && (
              <details open className="min-w-0 rounded-md border border-line bg-surface-0 p-3 text-xs" data-testid="diagram-transcription">
                <summary className="cursor-pointer font-semibold text-ink-1">{transcription}</summary>
                <p className="mt-1 text-ink-3">
                  Stored at ingestion{citation.layout_source ? ` by ${layoutSourceLabel(citation.layout_source)}` : ""}; this is the text the retriever indexed and the model was given.
                </p>
                <p className="mt-2 whitespace-pre-wrap break-words font-mono leading-relaxed text-ink-1">{citation.excerpt}</p>
              </details>
            )}
          </div>
          {!transcription && (
            <blockquote className="shrink-0 rounded-md border-l-4 border-ochre bg-surface-0 p-3 font-mono text-xs leading-relaxed text-ink-1">
              “{citation.excerpt}”
            </blockquote>
          )}
          <div className="shrink-0">
          <KeyValue
            items={[
              ["doc", `${citation.doc_name} · p.${citation.page_number}`],
              ["solicitation", citation.solicitation_id],
              ["section_id", citation.section_id || "—"],
              ["element_type", citation.element_type],
              ...(citation.layout_source ? [["layout_source", citation.layout_source] as const] : []),
              ...(citation.bbox_source ? [["bbox_source", citation.bbox_source] as const] : []),
              ["chunk_id", citation.chunk_id],
              [region ? `region bbox (${region.label})` : "stored bbox", region ? boxText(region.box) : bboxText],
              ["match query", hl.data?.query_used ?? (hl.data ? "none matched" : "—")],
            ]}
          />
          </div>
        </div>
      </div>
    </div>
  );
};
