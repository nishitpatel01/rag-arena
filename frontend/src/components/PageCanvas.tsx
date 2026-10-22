import React, { useEffect, useState } from "react";
import type { BoundingBox } from "../types/arena";
import { useSafeMode } from "../app/SafeMode";
import { fetchPageImage, rectToPercent, type HighlightSource, type NormalizedRect } from "../lib/documents";
import type { CitationRegion } from "../lib/elements";

export interface PageCanvasProps {
  /** URL of /api/v1/documents/page for the page to show. */
  readonly imageUrl: string;
  readonly alt: string;
  /** Initial aspect ratio (width / height) before the image loads, e.g. from highlights page_width/height. */
  readonly aspectHint: number | null;
  readonly source: HighlightSource;
  readonly rects: readonly NormalizedRect[];
  readonly storedBbox: BoundingBox | null;
  /**
   * Level 3 table / diagram region (Document AI / Gemini vision). Drawn whenever the cited page is shown
   * (with or without a text match) and replaces the generic "stored chunk bbox" fallback.
   */
  readonly region?: CitationRegion | null;
  readonly onPageCount?: (count: number) => void;
  readonly compact?: boolean;
}

const MATCH_STYLE: React.CSSProperties = {
  border: "2px solid var(--accent-ochre)",
  backgroundColor: "color-mix(in srgb, var(--accent-ochre) 26%, transparent)",
  boxShadow: "0 0 0 1px var(--accent-moss)",
};
const STORED_STYLE: React.CSSProperties = {
  border: "2px dashed var(--accent-slate)",
  backgroundColor: "color-mix(in srgb, var(--accent-slate) 10%, transparent)",
};
const REGION_STYLE: React.CSSProperties = {
  border: "2px dashed var(--accent-clay)",
  backgroundColor: "color-mix(in srgb, var(--accent-clay) 8%, transparent)",
};

/** Real rendered page (PNG from the backend) with absolutely positioned highlight overlays in % units. */
export const PageCanvas: React.FC<PageCanvasProps> = ({ imageUrl, alt, aspectHint, source, rects, storedBbox, region = null, onPageCount, compact = false }) => {
  const [image, setImage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [aspect, setAspect] = useState<number | null>(aspectHint);
  const { mask } = useSafeMode();

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setImage(null);
    setError(null);
    fetchPageImage(imageUrl, controller.signal)
      .then((img) => {
        objectUrl = img.objectUrl;
        if (controller.signal.aborted) {
          URL.revokeObjectURL(img.objectUrl);
          return;
        }
        setImage(img.objectUrl);
        if (img.pageCount !== null) onPageCount?.(img.pageCount);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // onPageCount is a stable setter from the parent; refetch only when the URL changes.
  }, [imageUrl]);

  useEffect(() => {
    if (aspectHint && !image) setAspect(aspectHint);
  }, [aspectHint, image]);

  const showMatch = source === "text_match";
  const onCitedPage = source !== "other_page" && source !== "loading";
  const regionBox = region && onCitedPage ? rectToPercent(region.box) : null;
  const showStored = !regionBox && (source === "stored_bbox" || source === "lookup_failed");
  const stored = showStored ? rectToPercent(storedBbox) : null;

  return (
    <div
      className="relative w-full overflow-hidden rounded-md border border-line bg-surface-0"
      style={{ aspectRatio: aspect ? String(aspect) : "612 / 792" }}
      data-highlight-source={source}
      data-region={regionBox && region ? region.kind : undefined}
    >
      {image && (
        <img
          src={image}
          alt={alt}
          className="block h-full w-full select-none"
          draggable={false}
          onLoad={(e) => {
            const el = e.currentTarget;
            if (el.naturalWidth > 0 && el.naturalHeight > 0) setAspect(el.naturalWidth / el.naturalHeight);
          }}
        />
      )}
      {!image && !error && (
        <p className="absolute inset-0 flex items-center justify-center text-xs text-ink-3" role="status">
          Rendering page…
        </p>
      )}
      {error && (
        <div role="alert" className="absolute inset-0 overflow-auto p-3 text-xs">
          <p className="font-semibold text-err">Page image failed to load</p>
          <p className="mt-1 break-words font-mono text-ink-1">{mask(error)}</p>
        </div>
      )}
      {image && showMatch &&
        rects.map((r, i) => {
          const box = rectToPercent(r);
          return box ? <div key={i} aria-hidden className="pointer-events-none absolute rounded-sm" style={{ ...box, ...MATCH_STYLE }} /> : null;
        })}
      {image && regionBox && region && (
        <div aria-hidden className="pointer-events-none absolute rounded-sm" style={{ ...regionBox, ...REGION_STYLE }} data-testid="region-overlay">
          {!compact && (
            <span className="absolute left-0 top-0 -translate-y-full whitespace-nowrap rounded-sm border border-clay bg-surface-1 px-1 font-mono text-xs text-clay">
              {region.label}
            </span>
          )}
        </div>
      )}
      {image && stored && (
        <div aria-hidden className="pointer-events-none absolute rounded-sm" style={{ ...stored, ...STORED_STYLE }}>
          {!compact && (
            <span className="absolute -top-5 left-0 whitespace-nowrap rounded-sm bg-surface-1 px-1 font-mono text-xs text-slate">
              stored chunk bbox
            </span>
          )}
        </div>
      )}
    </div>
  );
};
