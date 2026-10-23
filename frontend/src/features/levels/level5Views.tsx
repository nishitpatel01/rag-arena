import React, { useState } from "react";
import { ChevronDown, ChevronRight, FileSearch, Sparkles } from "lucide-react";
import type { Citation } from "../../types/arena";
import { CodeBlock, stringifyJson } from "../../components/ui/CodeBlock";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { maskNode, useSafeMode } from "../../app/SafeMode";
import { formatMs } from "../../lib/format";
import {
  MODEL_OVER_SEGMENTS_MODE,
  SUMMARY_MODE,
  referenceCitationIndex,
  resultPages,
  segmentCount,
  synthesizeModeLabel,
  topRelevance,
  truncateToken,
  type ManagedResult,
  type ManagedRetrieveView,
  type ManagedSynthesizeView,
} from "./level5Payloads";

const sectionTitle = "text-xs font-semibold uppercase tracking-wide text-ink-3";
const cell = "px-2 py-1";
const score = (v: number | null): string => (v === null ? "—" : v.toFixed(3));

const SegmentList: React.FC<{ readonly result: ManagedResult; readonly id: string }> = ({ result, id }) => (
  <ol id={id} className="flex flex-col gap-1.5" aria-label={`Extractive segments of ${result.docName ?? result.documentId}`}>
    {result.segments.length === 0 && <li className="text-xs text-ink-3">No extractive segments returned for this document.</li>}
    {result.segments.map((s, i) => (
      <li key={i} className="rounded-md border border-line bg-surface-0 p-2">
        <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="font-mono text-ink-2">segment {i + 1}</span>
          <Pill tone="info" mono>{s.pageNumber !== null ? `p.${s.pageNumber}` : "page not reported"}</Pill>
          <span className="font-mono text-ink-2">relevance_score {score(s.relevanceScore)}</span>
        </div>
        <p className="max-h-44 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-1" tabIndex={0} aria-label={`Segment ${i + 1} text`}>{s.content}</p>
      </li>
    ))}
    {result.invalidSegments > 0 && (
      <li className="text-xs text-clay" role="status">Warning: {result.invalidSegments} segment(s) did not match the contract or exceeded the display bound (see raw payload).</li>
    )}
  </ol>
);

/** Level 5 retrieve: results table (doc · page · relevance score) with expandable extractive segments. */
export const ManagedRetrieveDetail: React.FC<{ readonly view: ManagedRetrieveView; readonly requestShownAbove?: boolean }> = ({ view, requestShownAbove = false }) => {
  const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set(view.results.slice(0, 1).map((r) => r.rank)));
  const { mask } = useSafeMode();
  const toggle = (rank: number): void =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(rank)) next.delete(rank);
      else next.add(rank);
      return next;
    });
  const allOpen = view.results.length > 0 && view.results.every((r) => open.has(r.rank));
  return (
    <section aria-label="Vertex AI Search results" className="flex min-w-0 flex-col gap-2" data-testid="managed-retrieve">
      <h4 className={sectionTitle}>Vertex AI Search results</h4>
      <KeyValue
        columns={2}
        label="Search response summary"
        items={[
          ["results returned", view.results.length.toLocaleString()],
          ["extractive segments", segmentCount(view).toLocaleString()],
          ["total_size", view.totalSize === null ? "—" : view.totalSize.toLocaleString()],
          ["api_ms", view.apiMs === null ? "—" : formatMs(view.apiMs)],
          ["attribution_token", truncateToken(view.attributionToken)],
        ]}
      />
      {view.invalidRows > 0 && (
        <p className="text-xs text-clay" role="status">Warning: {view.invalidRows} result row(s) did not match the contract and are not shown (see raw payload).</p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="text-xs font-medium text-moss underline disabled:opacity-60"
          disabled={view.results.length === 0}
          onClick={() => setOpen(allOpen ? new Set() : new Set(view.results.map((r) => r.rank)))}
        >
          {allOpen ? "Collapse all segments" : "Expand all segments"}
        </button>
      </div>
      <div className="max-h-[32rem] w-full overflow-auto rounded-md border border-line">
        <table className="w-full border-collapse text-xs">
          <caption className="sr-only">Search results in response order with page and best extractive-segment relevance score</caption>
          <thead className="sticky top-0 bg-surface-2 text-ink-3">
            <tr>
              <th scope="col" className={`${cell} text-right font-semibold`}>#</th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Document · title</th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Page</th>
              <th scope="col" className={`${cell} whitespace-nowrap text-right font-semibold`}>Top relevance</th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Segments</th>
            </tr>
          </thead>
          <tbody>
            {view.results.length === 0 && (
              <tr>
                <td colSpan={5} className={`${cell} text-ink-3`}>The search returned no results.</td>
              </tr>
            )}
            {view.results.map((r) => {
              const expanded = open.has(r.rank);
              const segId = `managed-segments-${r.rank}`;
              return (
                <React.Fragment key={`${r.rank}-${r.documentId}`}>
                  <tr className="border-t border-line align-top">
                    <td className={`${cell} text-right font-mono tabular-nums text-ink-1`}>{r.rank}</td>
                    <td className={`${cell} text-ink-1`}>
                      <div className="flex min-w-[12rem] flex-col">
                        <span className="break-words">{maskNode(r.docName ?? r.documentId, mask)}</span>
                        {r.title && r.title !== r.docName && <span className="break-words text-ink-3">{maskNode(r.title, mask)}</span>}
                      </div>
                    </td>
                    <td className={`${cell} whitespace-nowrap font-mono text-ink-1`}>{resultPages(r)}</td>
                    <td className={`${cell} text-right font-mono tabular-nums text-ink-1`}>{score(topRelevance(r))}</td>
                    <td className={cell}>
                      <button
                        type="button"
                        aria-expanded={expanded}
                        aria-controls={segId}
                        onClick={() => toggle(r.rank)}
                        className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5 text-ink-2 hover:bg-surface-2"
                      >
                        {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
                        {r.segments.length} segment{r.segments.length === 1 ? "" : "s"}
                        <span className="sr-only">{expanded ? " (collapse)" : " (expand)"} for result {r.rank}</span>
                      </button>
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="bg-surface-1">
                      <td />
                      <td colSpan={4} className={`${cell} pb-2`}>
                        <SegmentList result={r} id={segId} />
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {view.request && !requestShownAbove && <CodeBlock label="request (search body sent to SearchService.search)" language="json" code={stringifyJson(view.request)} />}
      {view.request && requestShownAbove && (
        <p className="text-xs text-ink-3">The search body sent to SearchService.search is shown under “Request sent” below (identical to the payload's request body).</p>
      )}
    </section>
  );
};

/** Badge naming which synthesis path produced the answer. */
export const SynthModeBadge: React.FC<{ readonly view: ManagedSynthesizeView; readonly labelFor?: (id: string | null) => string | null }> = ({ view, labelFor }) => {
  const isSummary = view.mode === SUMMARY_MODE;
  const Icon = isSummary ? Sparkles : FileSearch;
  return (
    <Pill tone={view.mode === null ? "warn" : isSummary ? "active" : "info"} icon={<Icon className="h-3 w-3 shrink-0" aria-hidden />} title={`synthesize payload mode: ${view.rawMode ?? view.mode ?? "not reported"}`}>
      {synthesizeModeLabel(view, labelFor)}
    </Pill>
  );
};

export interface ManagedSynthesizeDetailProps {
  readonly view: ManagedSynthesizeView;
  readonly citations?: readonly Citation[];
  readonly onSelectCitation?: (c: Citation) => void;
  readonly labelFor?: (id: string | null) => string | null;
}

/** Level 5 synthesize: mode badge, summary with numbered references → citations, skipped reasons verbatim. */
export const ManagedSynthesizeDetail: React.FC<ManagedSynthesizeDetailProps> = ({ view, citations = [], onSelectCitation, labelFor }) => (
  <section aria-label="Level 5 synthesis" className="flex min-w-0 flex-col gap-2" data-testid="managed-synthesize">
    <div className="flex flex-wrap items-center gap-1.5">
      <h4 className={sectionTitle}>Synthesis mode</h4>
      <SynthModeBadge view={view} labelFor={labelFor} />
      {view.modelVersionRequested && <Pill tone="neutral" mono title="summarySpec.modelSpec.version sent with the search request">model_version requested: {view.modelVersionRequested}</Pill>}
      {view.modelVersion && <Pill tone="neutral" mono title="model version reported for the summary">model_version{view.modelVersionRequested ? " used" : ""}: {view.modelVersion}</Pill>}
    </div>
    {view.mode === MODEL_OVER_SEGMENTS_MODE && (
      <p className="text-xs text-ink-2">The extractive segments returned by Vertex AI Search were sent to the selected catalog model; the managed summary was not requested.</p>
    )}
    {view.skippedReasons.length > 0 && (
      <div role="status" className="rounded-md border border-clay p-2 text-xs" data-testid="summary-skipped">
        <p className="font-semibold text-clay">Vertex AI Search skipped the summary — reasons (verbatim):</p>
        <ul className="mt-1 list-disc pl-5 font-mono text-ink-1">
          {view.skippedReasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </div>
    )}
    {view.mode === SUMMARY_MODE && (
      <>
        <h4 className={sectionTitle}>Summary (verbatim from Vertex AI Search)</h4>
        {view.summary ? (
          <p className="whitespace-pre-wrap break-words rounded-md border border-line bg-surface-0 p-2 text-xs leading-relaxed text-ink-1" data-testid="managed-summary">
            {view.summary}
          </p>
        ) : (
          <p className="text-xs text-ink-3">No summary text in the payload.</p>
        )}
        <h4 className={sectionTitle}>References ({view.references.length})</h4>
        {view.references.length === 0 ? (
          <p className="text-xs text-clay">The summary returned no references.</p>
        ) : (
          <ol className="flex flex-col gap-1" data-testid="summary-references">
            {view.references.map((ref) => {
              const idx = referenceCitationIndex(ref, citations);
              const label = `${ref.docName ?? ref.documentId ?? "?"}${ref.pageNumber !== null ? ` · p.${ref.pageNumber}` : ""}`;
              return (
                <li key={ref.index} className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="font-mono text-ink-2">[{ref.index}]</span>
                  {idx >= 0 && onSelectCitation ? (
                    <button
                      type="button"
                      aria-haspopup="dialog"
                      onClick={() => onSelectCitation(citations[idx])}
                      className="rounded-md border border-line px-1.5 py-0.5 text-left text-slate underline-offset-2 hover:bg-surface-2 hover:underline"
                      title={`Open citation [${idx + 1}] in the document viewer`}
                    >
                      {label} → citation [{idx + 1}]
                    </button>
                  ) : (
                    <span className="text-ink-1">
                      {label} <span className="text-ink-3">(not among the answer citations)</span>
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        )}
        {view.invalidReferences > 0 && (
          <p className="text-xs text-clay" role="status">Warning: {view.invalidReferences} reference(s) did not match the contract (see raw payload).</p>
        )}
      </>
    )}
  </section>
);
