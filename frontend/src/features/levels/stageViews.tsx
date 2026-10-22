import React, { useState } from "react";
import { ArrowDown, ArrowUp, Equal, GitMerge, Sparkles, Spline, Type } from "lucide-react";
import type { JsonObject } from "../../types/arena";
import { ElementFilterChips, ElementMetaCell, ElementTypeBadge } from "../../components/ElementBadges";
import { countElementTypes, matchesElementFilter, type ElementFilter, type ElementMeta } from "../../lib/elements";
import { elementMixSegments, parseElementIntent, parseSynthesizeL3, type ElementMix, type SynthesizeL3View } from "./level3Payloads";
import { DataTable } from "../../components/ui/DataTable";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { formatBytes } from "../../lib/format";
import {
  CATEGORY_LABEL,
  anyElementMeta,
  categorizeCandidate,
  countCategories,
  diffWords,
  indexUsageLines,
  joinElementMeta,
  maxRerankScore,
  movedIntoTop,
  parseHybridRetrieve,
  parseQueryPrep,
  parseRerank,
  rankCountsLine,
  rankMovement,
  rerankBillingLine,
  rerankSummaryLine,
  retrieveMetaByChunk,
  scoreBarFraction,
  topCutoff,
  type CandidateCategory,
  type HybridRetrieveView,
  type QueryPrepView,
  type RerankView,
} from "./stagePayloads";

const sectionTitle = "text-xs font-semibold uppercase tracking-wide text-ink-3";

const InvalidRowsNote: React.FC<{ readonly count: number; readonly what: string }> = ({ count, what }) =>
  count > 0 ? (
    <p className="text-xs text-clay" role="status">
      Warning: {count} {what} in the payload did not match the contract and are not shown (see raw payload).
    </p>
  ) : null;

// ---------------------------------------------------------------------------
// query_prep
// ---------------------------------------------------------------------------

export const QueryPrepDetail: React.FC<{ readonly view: QueryPrepView; readonly elementIntent?: readonly string[] | null }> = ({ view, elementIntent = null }) => {
  const ops = diffWords(view.originalQuery, view.preparedQuery);
  const changed = ops.some((o) => o.kind !== "same");
  return (
    <section aria-label="Query preparation" className="flex min-w-0 flex-col gap-2" data-testid="query-prep-detail">
      {elementIntent !== null && (
        <div className="flex flex-wrap items-center gap-1.5" data-testid="element-intent">
          <h4 className={sectionTitle}>Element intent</h4>
          {elementIntent.length === 0 ? (
            <span className="text-xs text-ink-3">none detected (no element-type pre-filter)</span>
          ) : (
            elementIntent.map((t) => <ElementTypeBadge key={t} elementType={t} />)
          )}
        </div>
      )}
      <h4 className={sectionTitle}>Expansions</h4>
      <DataTable
        caption="Acronym expansions applied to the query"
        rows={view.expansions}
        rowKey={(r, i) => `${r.term}-${i}`}
        emptyText="No expansions applied (prepared query equals the original)."
        columns={[
          { key: "term", header: "Term", render: (r) => r.term, mono: true },
          { key: "arrow", header: <span className="sr-only">expands to</span>, render: () => <span aria-hidden>→</span> },
          { key: "expansion", header: "Expansion", render: (r) => r.expansion },
        ]}
      />
      <InvalidRowsNote count={view.invalidRows} what="expansion rows" />
      <h4 className={sectionTitle}>Original → prepared query</h4>
      <div className="grid gap-2 md:grid-cols-2">
        <div className="rounded-md border border-line bg-surface-0 p-2">
          <p className="mb-1 text-xs text-ink-3">original_query</p>
          <p className="break-words font-mono text-xs text-ink-1">
            {ops.map((o, i) =>
              o.kind === "added" ? null : o.kind === "removed" ? (
                <del key={i} className="rounded-sm border border-dashed border-clay px-0.5 text-clay" title="removed in prepared query">
                  {o.text}
                </del>
              ) : (
                <span key={i}>{o.text}</span>
              )
            )}
          </p>
        </div>
        <div className="rounded-md border border-line bg-surface-0 p-2">
          <p className="mb-1 text-xs text-ink-3">prepared_query {changed ? "(added text highlighted)" : "(unchanged)"}</p>
          <p className="break-words font-mono text-xs text-ink-1" data-testid="prepared-query">
            {ops.map((o, i) =>
              o.kind === "removed" ? null : o.kind === "added" ? (
                <ins key={i} className="rounded-sm bg-surface-2 px-0.5 font-semibold text-moss no-underline outline outline-1 outline-moss" title="added by query preparation">
                  <span className="sr-only">added: </span>
                  {o.text}
                </ins>
              ) : (
                <span key={i}>{o.text}</span>
              )
            )}
          </p>
        </div>
      </div>
    </section>
  );
};

// ---------------------------------------------------------------------------
// retrieve (hybrid)
// ---------------------------------------------------------------------------

const CATEGORY_TONE: Readonly<Record<CandidateCategory, PillTone>> = {
  both: "active",
  vector: "info",
  lexical: "neutral",
  unranked: "warn",
};

const CATEGORY_ICON: Readonly<Record<CandidateCategory, React.ComponentType<{ className?: string }>>> = {
  both: GitMerge,
  vector: Spline,
  lexical: Type,
  unranked: Equal,
};

export const CategoryBadge: React.FC<{ readonly category: CandidateCategory }> = ({ category }) => {
  const Icon = CATEGORY_ICON[category];
  return (
    <Pill tone={CATEGORY_TONE[category]} icon={<Icon className="h-3 w-3 shrink-0" aria-hidden />}>
      {CATEGORY_LABEL[category]}
    </Pill>
  );
};

const rankText = (r: number | null): string => (r === null ? "—" : `#${r}`);

/** "Document · page" cell; Level 3 rows add element / layout / bbox / amendment badges underneath (keeps the table narrow). */
const DocCell: React.FC<{ readonly chunkId: string; readonly docName: string | null; readonly pageNumber: number | null; readonly meta: ElementMeta | null }> = ({
  chunkId,
  docName,
  pageNumber,
  meta,
}) => (
  <div className="flex min-w-[12rem] flex-col gap-1">
    <span title={chunkId}>
      {docName ?? chunkId}
      {pageNumber !== null ? ` · p.${pageNumber}` : ""}
    </span>
    {meta && <ElementMetaCell meta={meta} />}
  </div>
);

export const HybridRetrieveDetail: React.FC<{ readonly view: HybridRetrieveView }> = ({ view }) => {
  const [filter, setFilter] = useState<ElementFilter>("all");
  const counts = countCategories(view.candidates);
  const withMeta = anyElementMeta(view.candidates);
  const elementCounts = countElementTypes(view.candidates.map((c) => c.meta));
  const allRows = view.candidates.map((c, i) => ({ c, rrfRank: i + 1, category: categorizeCandidate(c) }));
  const rows = withMeta ? allRows.filter((r) => matchesElementFilter(r.c.meta.elementType, filter)) : allRows;
  return (
    <section aria-label="Hybrid retrieval candidates" className="flex min-w-0 flex-col gap-2" data-testid="retrieve-candidates">
      <h4 className={sectionTitle}>RRF candidates</h4>
      <div className="flex flex-wrap items-center gap-1.5" data-testid="retrieve-summary">
        <CategoryBadge category="both" />
        <span className="font-mono text-xs text-ink-1">{counts.both}</span>
        <CategoryBadge category="vector" />
        <span className="font-mono text-xs text-ink-1">{counts.vector}</span>
        <CategoryBadge category="lexical" />
        <span className="font-mono text-xs text-ink-1">{counts.lexical}</span>
        {counts.unranked > 0 && (
          <>
            <CategoryBadge category="unranked" />
            <span className="font-mono text-xs text-ink-1">{counts.unranked}</span>
          </>
        )}
      </div>
      <KeyValue
        columns={2}
        label="Retrieval summary"
        items={[
          ["rows_returned", view.rowsReturned === null ? "—" : view.rowsReturned.toLocaleString()],
          ["candidate_k", view.candidateK === null ? "—" : String(view.candidateK)],
          ["bytes scanned", formatBytes(view.bytesProcessed)],
          ["cache hit", view.cacheHit === null ? "—" : view.cacheHit ? "yes (BigQuery result cache)" : "no"],
          ["rrf_k", view.rrfK === null ? "—" : String(view.rrfK)],
          ["slot_millis", view.slotMillis === null ? "—" : view.slotMillis.toLocaleString()],
          ["job_id", view.jobId ?? "—"],
        ]}
      />
      {rankCountsLine(view) && (
        <p className="font-mono text-xs text-ink-1" data-testid="retrieve-rank-counts">{rankCountsLine(view)}</p>
      )}
      {indexUsageLines(view).length > 0 && (
        <div className="rounded-md border border-line bg-surface-0 p-2" data-testid="retrieve-index-usage">
          <p className="mb-1 text-xs text-ink-3">Index usage (verbatim from BigQuery job statistics)</p>
          <ul className="flex flex-col gap-0.5">
            {indexUsageLines(view).map((line) => (
              <li key={line} className="break-words font-mono text-xs text-ink-1">{line}</li>
            ))}
          </ul>
        </div>
      )}
      <InvalidRowsNote count={view.invalidRows} what="candidate rows" />
      {withMeta && (
        <ElementFilterChips counts={elementCounts} total={view.candidates.length} value={filter} onChange={setFilter} label="Filter retrieve candidates by element type" />
      )}
      <div className="max-h-96 overflow-y-auto">
        <DataTable
          caption="Hybrid retrieval candidates ranked by reciprocal rank fusion score"
          rows={rows}
          rowKey={(r) => r.c.chunkId}
          emptyText={filter === "all" ? "The retrieve stage returned no candidates." : "No candidates of this element type."}
          columns={[
            { key: "rank", header: "RRF #", render: (r) => r.rrfRank, align: "right", mono: true },
            {
              key: "doc",
              header: withMeta ? "Document · page · element" : "Document · page",
              render: (r) => <DocCell chunkId={r.c.chunkId} docName={r.c.docName} pageNumber={r.c.pageNumber} meta={withMeta ? r.c.meta : null} />,
            },
            { key: "section", header: "Section", render: (r) => r.c.sectionId ?? "—", mono: true },
            { key: "vector", header: "Vector rank", render: (r) => rankText(r.c.vectorRank), align: "right", mono: true },
            { key: "lexical", header: "Lexical rank", render: (r) => rankText(r.c.lexicalRank), align: "right", mono: true },
            { key: "rrf", header: "rrf_score", render: (r) => r.c.rrfScore.toFixed(4), align: "right", mono: true },
            { key: "source", header: "Matched by", render: (r) => <CategoryBadge category={r.category} /> },
          ]}
        />
      </div>
    </section>
  );
};

// ---------------------------------------------------------------------------
// rerank
// ---------------------------------------------------------------------------

const MOVE_STYLE = {
  up: { Icon: ArrowUp, cls: "text-moss" },
  down: { Icon: ArrowDown, cls: "text-clay" },
  same: { Icon: Equal, cls: "text-ink-3" },
} as const;

export const MovementIndicator: React.FC<{ readonly rrfRank: number; readonly rerankRank: number }> = ({ rrfRank, rerankRank }) => {
  const m = rankMovement(rrfRank, rerankRank);
  const { Icon, cls } = MOVE_STYLE[m.direction];
  return (
    <span className={`inline-flex items-center gap-0.5 font-mono text-xs font-semibold ${cls}`} title={m.srText}>
      <Icon className="h-3.5 w-3.5" aria-hidden />
      <span aria-hidden>{m.text}</span>
      <span className="sr-only">{m.srText}</span>
    </span>
  );
};

export const ScoreBar: React.FC<{ readonly score: number; readonly max: number; readonly label: string }> = ({ score, max, label }) => {
  const pct = Math.round(scoreBarFraction(score, max) * 100);
  return (
    <span className="inline-flex min-w-[8rem] items-center gap-2">
      <span className="h-2 w-20 overflow-hidden rounded bg-surface-2" role="meter" aria-valuemin={0} aria-valuemax={Math.max(1, max)} aria-valuenow={score} aria-label={label}>
        <span className="block h-full bg-moss" style={{ width: `${pct}%` }} />
      </span>
      <span className="font-mono text-xs tabular-nums text-ink-1">{score.toFixed(3)}</span>
    </span>
  );
};

export const RerankDetail: React.FC<{ readonly view: RerankView; readonly retrieveMeta?: ReadonlyMap<string, ElementMeta> }> = ({ view, retrieveMeta }) => {
  const [filter, setFilter] = useState<ElementFilter>("all");
  const cutoff = topCutoff(view);
  const max = maxRerankScore(view) ?? 1;
  const joined = view.results.map((r) => ({ ...r, meta: joinElementMeta(r.meta, retrieveMeta?.get(r.chunkId)) }));
  const withMeta = anyElementMeta(joined);
  const rows = withMeta ? joined.filter((r) => matchesElementFilter(r.meta.elementType, filter)) : joined;
  return (
    <section aria-label="Rerank before and after" className="flex min-w-0 flex-col gap-2" data-testid="rerank-detail">
      <h4 className={sectionTitle}>Rerank — before (RRF) → after (Ranking API)</h4>
      <p className="break-words font-mono text-xs text-ink-1" data-testid="rerank-summary">{rerankSummaryLine(view)}</p>
      {rerankBillingLine(view) && (
        <p className="break-words font-mono text-xs text-ink-2" data-testid="rerank-billing">{rerankBillingLine(view)}</p>
      )}
      <InvalidRowsNote count={view.invalidRows} what="result rows" />
      {withMeta && (
        <ElementFilterChips counts={countElementTypes(joined.map((r) => r.meta))} total={joined.length} value={filter} onChange={setFilter} label="Filter rerank results by element type" />
      )}
      <DataTable
        caption="Rerank results: RRF rank before, Ranking API rank after, with movement and score"
        rows={rows}
        rowKey={(r) => r.chunkId}
        emptyText={filter === "all" ? "The Ranking API returned no results." : "No results of this element type."}
        columns={[
          { key: "after", header: "Rerank #", render: (r) => r.rerankRank, align: "right", mono: true },
          { key: "before", header: "RRF #", render: (r) => r.rrfRank, align: "right", mono: true },
          { key: "move", header: "Movement", render: (r) => <MovementIndicator rrfRank={r.rrfRank} rerankRank={r.rerankRank} /> },
          {
            key: "doc",
            header: withMeta ? "Document · page · element" : "Document · page",
            render: (r) => <DocCell chunkId={r.chunkId} docName={r.docName} pageNumber={r.pageNumber} meta={withMeta ? r.meta : null} />,
          },
          { key: "rrf", header: "rrf_score", render: (r) => (r.rrfScore === null ? "—" : r.rrfScore.toFixed(4)), align: "right", mono: true },
          { key: "score", header: "rerank_score", render: (r) => <ScoreBar score={r.rerankScore} max={max} label={`rerank score of ${r.docName ?? r.chunkId}`} /> },
          {
            key: "marker",
            header: <span className="sr-only">Top-{cutoff} marker</span>,
            render: (r) =>
              movedIntoTop(r.rrfRank, r.rerankRank, cutoff) ? (
                <Pill tone="info" icon={<Sparkles className="h-3 w-3 shrink-0" aria-hidden />}>
                  moved into top-{cutoff}
                </Pill>
              ) : null,
          },
        ]}
      />
    </section>
  );
};

// ---------------------------------------------------------------------------
// synthesize (Level 3: authority headers + element mix)
// ---------------------------------------------------------------------------

const MIX_BAR_CLASS: Readonly<Record<string, string>> = { table: "bg-moss", diagram: "bg-slate", narrative_text: "bg-ochre" };

export const ElementMixBar: React.FC<{ readonly mix: ElementMix }> = ({ mix }) => {
  const segments = elementMixSegments(mix);
  const total = segments.reduce((s, x) => s + x.count, 0);
  return (
    <div className="flex flex-col gap-1.5" data-testid="element-mix">
      <div className="flex h-3 w-full max-w-md overflow-hidden rounded border border-line bg-surface-2" aria-hidden>
        {segments.map((s) =>
          s.fraction > 0 ? <span key={s.type} className={`block h-full ${MIX_BAR_CLASS[s.type]}`} style={{ width: `${(s.fraction * 100).toFixed(2)}%` }} /> : null
        )}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-1" aria-label={`Element mix of the ${total} context chunks sent to synthesis`}>
        {segments.map((s) => (
          <li key={s.type} className="inline-flex items-center gap-1.5 text-xs text-ink-1">
            <span className={`inline-block h-2.5 w-2.5 rounded-sm ${MIX_BAR_CLASS[s.type]}`} aria-hidden />
            <span className="font-medium">{s.label}</span>
            <span className="font-mono tabular-nums">{s.count}</span>
            <span className="text-ink-3">({Math.round(s.fraction * 100)}%)</span>
          </li>
        ))}
      </ul>
    </div>
  );
};

export const SynthesizeDetail: React.FC<{ readonly view: SynthesizeL3View }> = ({ view }) => (
  <section aria-label="Synthesis context" className="flex min-w-0 flex-col gap-2" data-testid="synthesize-detail">
    {view.authorityHeaders !== null && (
      <>
        <h4 className={sectionTitle}>Authority headers used</h4>
        {view.authorityHeaders.length === 0 ? (
          <p className="text-xs text-ink-3">The synthesis prompt carried no authority headers.</p>
        ) : (
          <ol className="flex list-decimal flex-col gap-1 pl-5" data-testid="authority-headers">
            {view.authorityHeaders.map((h, i) => (
              <li key={`${i}-${h}`} className="break-words font-mono text-xs text-ink-1">
                {h}
              </li>
            ))}
          </ol>
        )}
      </>
    )}
    {view.elementMix !== null && (
      <>
        <h4 className={sectionTitle}>Element mix sent to synthesis</h4>
        <ElementMixBar mix={view.elementMix} />
      </>
    )}
  </section>
);

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

/** Payload keys rendered by a dedicated view (omitted from the generic code blocks; still in raw JSON). */
export const DEDICATED_KEYS: Readonly<Record<string, readonly string[]>> = {
  query_prep: ["expansions", "element_intent"],
  retrieve: ["candidates"],
  rerank: ["results"],
  synthesize: ["authority_headers", "element_mix"],
};

/** Payloads of other stages of the same run that a dedicated view may join with (e.g. rerank ← retrieve). */
export interface StageViewContext {
  readonly retrievePayload?: JsonObject | null;
}

/** Dedicated renderer for a stage payload, or null when the stage/payload has none (generic view only). */
export function dedicatedStageView(stageId: string, payload: JsonObject | null, ctx: StageViewContext = {}): React.ReactElement | null {
  if (stageId === "query_prep") {
    const v = parseQueryPrep(payload);
    return v ? <QueryPrepDetail view={v} elementIntent={parseElementIntent(payload)} /> : null;
  }
  if (stageId === "synthesize") {
    const v = parseSynthesizeL3(payload);
    return v ? <SynthesizeDetail view={v} /> : null;
  }
  if (stageId === "retrieve") {
    const v = parseHybridRetrieve(payload);
    return v ? <HybridRetrieveDetail view={v} /> : null;
  }
  if (stageId === "rerank") {
    const v = parseRerank(payload);
    return v ? <RerankDetail view={v} retrieveMeta={retrieveMetaByChunk(ctx.retrievePayload ?? null)} /> : null;
  }
  return null;
}
