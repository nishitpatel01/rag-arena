import React from "react";
import { Maximize2 } from "lucide-react";
import type { Citation } from "../types/arena";
import { THUMB_DPI, highlightLabel, highlightSource, pageImageUrl } from "../lib/documents";
import { citationRegion, transcriptionTitle } from "../lib/elements";
import { ElementTypeBadge, LayoutSourceBadge } from "./ElementBadges";
import { useCitationHighlights } from "./DocumentDrawer";
import { EmptyState } from "./ui/EmptyState";
import { PageCanvas } from "./PageCanvas";

export interface DocumentViewerProps {
  readonly citation: Citation | null;
  /** Opens the full slide-over viewer for the selected citation. */
  readonly onOpen: () => void;
}

/** Side-panel summary of the selected citation: real page thumbnail with highlights + "Open viewer". */
export const DocumentViewer: React.FC<DocumentViewerProps> = ({ citation, onOpen }) => {
  const hl = useCitationHighlights(citation);
  if (!citation) {
    return (
      <EmptyState
        title="No citation selected"
        description="Click a citation under the answer to open the real source page with the cited excerpt highlighted."
      />
    );
  }
  const source = highlightSource(citation.page_number, citation.page_number, hl.data, hl.error);
  const region = citationRegion(citation);
  const transcription = transcriptionTitle(citation);
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-1">
          {citation.doc_name} · p.{citation.page_number}
        </h3>
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
        onClick={onOpen}
        aria-haspopup="dialog"
        aria-label={`Open viewer for ${citation.doc_name} page ${citation.page_number}`}
        className="block w-full rounded-md text-left hover:opacity-90"
      >
        <PageCanvas
          compact
          imageUrl={pageImageUrl(citation, citation.page_number, THUMB_DPI)}
          alt={`Thumbnail of ${citation.doc_name} page ${citation.page_number}`}
          aspectHint={hl.data ? hl.data.page_width / hl.data.page_height : null}
          source={source}
          rects={hl.data?.rects ?? []}
          storedBbox={citation.bbox}
          region={region}
        />
      </button>
      <p className={`text-xs ${source === "text_match" ? "text-moss" : source === "lookup_failed" ? "text-err" : source === "stored_bbox" ? "text-clay" : "text-ink-3"}`}>
        {highlightLabel(source, region?.label ?? null)}
      </p>
      <button
        type="button"
        onClick={onOpen}
        aria-haspopup="dialog"
        className="inline-flex items-center justify-center gap-1.5 rounded-md border border-moss px-3 py-1.5 text-xs font-medium text-moss hover:bg-surface-2"
      >
        <Maximize2 size={14} aria-hidden /> Open viewer
      </button>
      {transcription && <p className="text-xs font-semibold text-ink-2">{transcription}</p>}
      <blockquote className="line-clamp-4 rounded-md border-l-4 border-ochre bg-surface-0 p-2 font-mono text-xs leading-relaxed text-ink-1">
        “{citation.excerpt}”
      </blockquote>
    </div>
  );
};
