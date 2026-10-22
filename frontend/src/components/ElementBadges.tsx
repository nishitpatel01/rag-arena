import React from "react";
import { AlignLeft, Eye, FileText, HelpCircle, Network, ScanText, Stamp, Table2 } from "lucide-react";
import { Pill, type PillTone } from "./ui/Pill";
import {
  amendmentLabel,
  bboxSourceLabel,
  elementFilterOptions,
  elementLabel,
  layoutSourceLabel,
  type ElementCounts,
  type ElementFilter,
  type ElementMeta,
} from "../lib/elements";

type IconT = React.ComponentType<{ className?: string }>;

const ELEMENT_ICON: Readonly<Record<string, IconT>> = { table: Table2, diagram: Network, narrative_text: AlignLeft };
const ELEMENT_TONE: Readonly<Record<string, PillTone>> = { table: "active", diagram: "info", narrative_text: "neutral" };
const SOURCE_ICON: Readonly<Record<string, IconT>> = { documentai_layout: ScanText, gemini_vision: Eye, pymupdf: FileText };

const icon = (Icon: IconT): React.ReactNode => <Icon className="h-3 w-3 shrink-0" aria-hidden />;

/** TABLE / DIAGRAM / TEXT badge (icon + text; unknown types verbatim). */
export const ElementTypeBadge: React.FC<{ readonly elementType: string }> = ({ elementType }) => (
  <Pill tone={ELEMENT_TONE[elementType] ?? "neutral"} icon={icon(ELEMENT_ICON[elementType] ?? HelpCircle)} title={`element_type: ${elementType}`}>
    <span className="sr-only">element type </span>
    {elementLabel(elementType)}
  </Pill>
);

/** "Document AI layout" / "Gemini vision" / "PyMuPDF" badge (unknown sources verbatim). */
export const LayoutSourceBadge: React.FC<{ readonly layoutSource: string }> = ({ layoutSource }) => (
  <Pill tone="neutral" icon={icon(SOURCE_ICON[layoutSource] ?? HelpCircle)} title={`layout_source: ${layoutSource}`}>
    <span className="sr-only">layout source </span>
    {layoutSourceLabel(layoutSource)}
  </Pill>
);

/** "Amendment 0001 · supersedes SEC-4.2"; renders nothing for the base document. */
export const AmendmentChip: React.FC<{ readonly amendmentNumber: number | null; readonly amendsSection: string | null }> = ({ amendmentNumber, amendsSection }) => {
  const text = amendmentLabel(amendmentNumber, amendsSection);
  return text ? (
    <Pill tone="warn" icon={icon(Stamp)} title="amendment_number · amends_section">
      {text}
    </Pill>
  ) : null;
};

/** Table cell: element type + layout source badges, bbox provenance, amendment chip; dash when nothing was reported. */
export const ElementMetaCell: React.FC<{ readonly meta: ElementMeta }> = ({ meta }) => {
  const amendment = amendmentLabel(meta.amendmentNumber, meta.amendsSection);
  if (!meta.elementType && !meta.layoutSource && !meta.bboxSource && !amendment) return <span className="text-xs text-ink-3">—</span>;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {meta.elementType && <ElementTypeBadge elementType={meta.elementType} />}
      {meta.layoutSource && <LayoutSourceBadge layoutSource={meta.layoutSource} />}
      {amendment && <AmendmentChip amendmentNumber={meta.amendmentNumber} amendsSection={meta.amendsSection} />}
      {meta.bboxSource && (
        <span className="text-xs text-ink-3" title={`bbox_source: ${meta.bboxSource}`}>
          bbox: {bboxSourceLabel(meta.bboxSource)}
        </span>
      )}
    </div>
  );
};

export interface ElementFilterChipsProps {
  readonly counts: ElementCounts;
  readonly total: number;
  readonly value: ElementFilter;
  readonly onChange: (f: ElementFilter) => void;
  /** Accessible group name, e.g. "Filter retrieve candidates by element type". */
  readonly label: string;
}

/** Toggle-button group (aria-pressed); each chip shows its label and row count. */
export const ElementFilterChips: React.FC<ElementFilterChipsProps> = ({ counts, total, value, onChange, label }) => (
  <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5" data-testid="element-filter">
    <span className="text-xs text-ink-3" aria-hidden>
      Filter:
    </span>
    {elementFilterOptions(counts, total).map((o) => {
      const pressed = value === o.filter;
      const Icon = o.filter === "all" ? null : ELEMENT_ICON[o.filter] ?? HelpCircle;
      return (
        <button
          key={o.filter}
          type="button"
          aria-pressed={pressed}
          onClick={() => onChange(o.filter)}
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${
            pressed ? "border-moss bg-surface-2 text-moss" : "border-line text-ink-2 hover:bg-surface-2"
          }`}
        >
          {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden />}
          {o.label}
          <span className="font-mono tabular-nums">{o.count}</span>
        </button>
      );
    })}
  </div>
);
