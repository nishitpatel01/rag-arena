import React from "react";
import { ArrowDown, ArrowUp, Equal, GitMerge, Sparkles, Spline, Type } from "lucide-react";
import type { JsonObject } from "../../types/arena";
import { DataTable } from "../../components/ui/DataTable";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill, type PillTone } from "../../components/ui/Pill";
import { formatBytes } from "../../lib/format";
import {
  CATEGORY_LABEL,
  categorizeCandidate,
  countCategories,
  diffWords,
  maxRerankScore,
  movedIntoTop,
  parseHybridRetrieve,
  parseQueryPrep,
  parseRerank,
  rankMovement,
  rerankSummaryLine,
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

export const QueryPrepDetail: React.FC<{ readonly view: QueryPrepView }> = ({ view }) => {
  const ops = diffWords(view.originalQuery, view.preparedQuery);
  const changed = ops.some((o) => o.kind !== "same");
  return (
    <section aria-label="Query preparation" className="flex min-w-0 flex-col gap-2" data-testid="query-prep-detail">
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

export const HybridRetrieveDetail: React.FC<{ readonly view: HybridRetrieveView }> = ({ view }) => {
  const counts = countCategories(view.candidates);
  const rows = view.candidates.map((c, i) => ({ c, rrfRank: i + 1, category: categorizeCandidate(c) }));
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
      <InvalidRowsNote count={view.invalidRows} what="candidate rows" />
      <div className="max-h-96 overflow-y-auto">
        <DataTable
          caption="Hybrid retrieval candidates ranked by reciprocal rank fusion score"
          rows={rows}
          rowKey={(r) => r.c.chunkId}
          emptyText="The retrieve stage returned no candidates."
          columns={[
            { key: "rank", header: "RRF #", render: (r) => r.rrfRank, align: "right", mono: true },
            {
              key: "doc",
              header: "Document · page",
              render: (r) => (
                <span title={r.c.chunkId}>
                  {r.c.docName ?? r.c.chunkId}
                  {r.c.pageNumber !== null ? ` · p.${r.c.pageNumber}` : ""}
                </span>
              ),
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

export const RerankDetail: React.FC<{ readonly view: RerankView }> = ({ view }) => {
  const cutoff = topCutoff(view);
  const max = maxRerankScore(view) ?? 1;
  return (
    <section aria-label="Rerank before and after" className="flex min-w-0 flex-col gap-2" data-testid="rerank-detail">
      <h4 className={sectionTitle}>Rerank — before (RRF) → after (Ranking API)</h4>
      <p className="break-words font-mono text-xs text-ink-1" data-testid="rerank-summary">{rerankSummaryLine(view)}</p>
      <InvalidRowsNote count={view.invalidRows} what="result rows" />
      <DataTable
        caption="Rerank results: RRF rank before, Ranking API rank after, with movement and score"
        rows={view.results}
        rowKey={(r) => r.chunkId}
        emptyText="The Ranking API returned no results."
        columns={[
          { key: "after", header: "Rerank #", render: (r) => r.rerankRank, align: "right", mono: true },
          { key: "before", header: "RRF #", render: (r) => r.rrfRank, align: "right", mono: true },
          { key: "move", header: "Movement", render: (r) => <MovementIndicator rrfRank={r.rrfRank} rerankRank={r.rerankRank} /> },
          {
            key: "doc",
            header: "Document · page",
            render: (r) => (
              <span title={r.chunkId}>
                {r.docName ?? r.chunkId}
                {r.pageNumber !== null ? ` · p.${r.pageNumber}` : ""}
              </span>
            ),
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
// Dispatch
// ---------------------------------------------------------------------------

/** Payload keys rendered by a dedicated view (omitted from the generic code blocks; still in raw JSON). */
export const DEDICATED_KEYS: Readonly<Record<string, readonly string[]>> = {
  query_prep: ["expansions"],
  retrieve: ["candidates"],
  rerank: ["results"],
};

/** Dedicated renderer for a stage payload, or null when the stage/payload has none (generic view only). */
export function dedicatedStageView(stageId: string, payload: JsonObject | null): React.ReactElement | null {
  if (stageId === "query_prep") {
    const v = parseQueryPrep(payload);
    return v ? <QueryPrepDetail view={v} /> : null;
  }
  if (stageId === "retrieve") {
    const v = parseHybridRetrieve(payload);
    return v ? <HybridRetrieveDetail view={v} /> : null;
  }
  if (stageId === "rerank") {
    const v = parseRerank(payload);
    return v ? <RerankDetail view={v} /> : null;
  }
  return null;
}
