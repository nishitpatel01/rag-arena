import React, { useState } from "react";
import { ChevronDown, ChevronRight, FileSearch, Sparkles } from "lucide-react";
import { maskNode, useSafeMode } from "../../app/SafeMode";
import { CodeBlock, stringifyJson } from "../../components/ui/CodeBlock";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { formatMs, formatUsd } from "../../lib/format";
import {
  GROUNDED_MODE,
  MODEL_OVER_CONTEXTS_MODE,
  contextPageLabel,
  ragSynthesizeModeLabel,
  type RagContextRow,
  type RagEngineRetrieveView,
  type RagEngineSynthesizeView,
} from "./level6Payloads";

const sectionTitle = "text-xs font-semibold uppercase tracking-wide text-ink-3";
const cell = "px-2 py-1";
const fmtScore = (v: number | null): string => (v === null ? "—" : v.toFixed(4));
const fmtInt = (v: number | null): string => (v === null ? "—" : v.toLocaleString());

interface ContextRowProps {
  readonly row: RagContextRow;
  readonly expanded: boolean;
  readonly onToggle: (rank: number) => void;
  readonly mask: (text: string) => string;
}

const ContextRow: React.FC<ContextRowProps> = ({ row, expanded, onToggle, mask }) => {
  const detailId = `rag-context-${row.rank}`;
  return (
    <React.Fragment>
      <tr className="border-t border-line align-top">
        <td className={`${cell} text-right font-mono tabular-nums text-ink-1`}>{row.rank}</td>
        <td className={`${cell} text-ink-1`}>
          <div className="flex min-w-[12rem] flex-col">
            <span className="break-words">{maskNode(row.docName ?? row.chunkId, mask)}</span>
            {row.solicitationId && (
              <span className="font-mono text-ink-3">{maskNode(row.solicitationId, mask)}</span>
            )}
          </div>
        </td>
        <td className={`${cell} whitespace-nowrap font-mono text-ink-1`}>{contextPageLabel(row)}</td>
        <td className={`${cell} text-right font-mono tabular-nums text-ink-1`}>{fmtScore(row.score)}</td>
        <td className={cell}>
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailId}
            onClick={() => onToggle(row.rank)}
            className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5 text-ink-2 hover:bg-surface-2"
          >
            {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
            {expanded ? "Hide chunk" : "View chunk"}
            <span className="sr-only"> for context {row.rank}</span>
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-surface-1">
          <td />
          <td colSpan={4} className={`${cell} pb-2`}>
            <div id={detailId} className="flex flex-col gap-1.5 rounded-md border border-line bg-surface-0 p-2">
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span className="font-mono text-ink-2">{maskNode(row.chunkId, mask)}</span>
                {row.pageSource && <Pill tone="info" mono>{row.pageSource}</Pill>}
                {row.uri && <span className="break-all font-mono text-ink-3">{maskNode(row.uri, mask)}</span>}
              </div>
              <p
                className="max-h-44 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-1"
                tabIndex={0}
                aria-label={`Context ${row.rank} text`}
              >
                {row.text}
              </p>
            </div>
          </td>
        </tr>
      )}
    </React.Fragment>
  );
};

export interface RagEngineRetrieveDetailProps {
  readonly view: RagEngineRetrieveView;
  readonly requestShownAbove?: boolean;
}

/** Level 6 retrieve: `:retrieveContexts` chunks table (doc · page span · cosine distance) with expandable text. */
export const RagEngineRetrieveDetail: React.FC<RagEngineRetrieveDetailProps> = ({
  view,
  requestShownAbove = false,
}) => {
  const [open, setOpen] = useState<ReadonlySet<number>>(
    () => new Set(view.contexts.slice(0, 1).map((c) => c.rank))
  );
  const { mask, maskValue } = useSafeMode();
  const toggle = (rank: number): void =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(rank)) next.delete(rank);
      else next.add(rank);
      return next;
    });
  const allOpen = view.contexts.length > 0 && view.contexts.every((c) => open.has(c.rank));
  const summaryItems: (readonly [string, string])[] = [
    ["contexts returned", view.contexts.length.toLocaleString()],
    ["corpus", view.corpus ? mask(view.corpus) : "—"],
    ["location", view.location ?? "—"],
    ["embedding_model", view.embeddingModel ?? "—"],
    ["api_ms", view.apiMs === null ? "—" : formatMs(view.apiMs)],
    ["attempts", view.attempts === null ? "—" : String(view.attempts)],
    ["retrieval_api_fee_usd", view.retrievalApiFeeUsd === null ? "—" : formatUsd(view.retrievalApiFeeUsd)],
    ["solicitation_filter", view.solicitationFilter ? mask(view.solicitationFilter) : "—"],
  ];

  return (
    <section
      aria-label="Vertex AI RAG Engine contexts"
      className="flex min-w-0 flex-col gap-2"
      data-testid="rag-engine-retrieve"
    >
      <h4 className={sectionTitle}>Vertex AI RAG Engine · RetrieveContexts</h4>
      <KeyValue columns={2} label="RetrieveContexts summary" items={summaryItems} />
      {view.invalidRows > 0 && (
        <p className="text-xs text-clay" role="status">
          Warning: {view.invalidRows} context row(s) did not match the contract or exceeded the display bound (see raw payload).
        </p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="text-xs font-medium text-moss underline disabled:opacity-60"
          disabled={view.contexts.length === 0}
          onClick={() => setOpen(allOpen ? new Set() : new Set(view.contexts.map((c) => c.rank)))}
        >
          {allOpen ? "Collapse all chunks" : "Expand all chunks"}
        </button>
      </div>
      <div className="max-h-[32rem] w-full overflow-auto rounded-md border border-line">
        <table className="w-full border-collapse text-xs">
          <caption className="sr-only">
            RagCorpus contexts returned by RetrieveContexts in rank order with page span and cosine distance (lower is closer)
          </caption>
          <thead className="sticky top-0 bg-surface-2 text-ink-3">
            <tr>
              <th scope="col" className={`${cell} text-right font-semibold`}>#</th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Document · scope</th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Page span</th>
              <th
                scope="col"
                title="Cosine distance returned by RetrieveContexts (lower = closer)"
                className={`${cell} whitespace-nowrap text-right font-semibold`}
              >
                Distance
              </th>
              <th scope="col" className={`${cell} text-left font-semibold`}>Chunk</th>
            </tr>
          </thead>
          <tbody>
            {view.contexts.length === 0 && (
              <tr>
                <td colSpan={5} className={`${cell} text-ink-3`}>RetrieveContexts returned no chunks.</td>
              </tr>
            )}
            {view.contexts.map((row) => (
              <ContextRow
                key={`${row.rank}-${row.chunkId}`}
                row={row}
                expanded={open.has(row.rank)}
                onToggle={toggle}
                mask={mask}
              />
            ))}
          </tbody>
        </table>
      </div>
      {view.apiRequest && !requestShownAbove && (
        <CodeBlock
          label="api_request (RetrieveContexts body)"
          language="json"
          code={stringifyJson(maskValue(view.apiRequest))}
        />
      )}
      {view.apiRequest && requestShownAbove && (
        <p className="text-xs text-ink-3">
          The RetrieveContexts request body is shown under “Request sent” below.
        </p>
      )}
    </section>
  );
};

export interface RagSynthModeBadgeProps {
  readonly view: RagEngineSynthesizeView;
  readonly labelFor?: (id: string | null) => string | null;
}

/** Badge naming which Level 6 synthesis path produced the answer (`VertexRagStore` grounded vs catalog LLM). */
export const RagSynthModeBadge: React.FC<RagSynthModeBadgeProps> = ({ view, labelFor }) => {
  const isGrounded = view.mode === GROUNDED_MODE;
  const Icon = isGrounded ? Sparkles : FileSearch;
  return (
    <Pill
      tone={isGrounded ? "active" : "info"}
      icon={<Icon className="h-3 w-3 shrink-0" aria-hidden />}
      title={`Level 6 synthesis mode: ${view.mode}`}
    >
      {ragSynthesizeModeLabel(view, labelFor)}
    </Pill>
  );
};

export interface RagEngineSynthesizeDetailProps {
  readonly view: RagEngineSynthesizeView;
  readonly labelFor?: (id: string | null) => string | null;
}

/** Level 6 synthesize: mode badge + token/grounding breakdown. */
export const RagEngineSynthesizeDetail: React.FC<RagEngineSynthesizeDetailProps> = ({ view, labelFor }) => {
  const { mask } = useSafeMode();
  const isGrounded = view.mode === GROUNDED_MODE;
  const items: (readonly [string, string])[] = [
    ["corpus", view.corpus ? mask(view.corpus) : "—"],
    ["synthesis model", (labelFor ? labelFor(view.model) : view.model) ?? view.model ?? "—"],
    ...(isGrounded && view.underlyingGeminiModel
      ? [["underlying Gemini model", (labelFor ? labelFor(view.underlyingGeminiModel) : null) ?? view.underlyingGeminiModel] as const]
      : []),
    ...(isGrounded
      ? [["grounding_chunks_count", fmtInt(view.groundingChunksCount)] as const]
      : [["context_chunks", fmtInt(view.contextChunks)] as const]),
    ...(view.thinkingLevel ? [["thinking_level", view.thinkingLevel] as const] : []),
    ["prompt_tokens", fmtInt(view.promptTokens)],
    ["output_tokens", fmtInt(view.outputTokens)],
    ...(view.thinkingTokens !== null ? [["thinking_tokens", fmtInt(view.thinkingTokens)] as const] : []),
  ];

  return (
    <section
      aria-label="Level 6 synthesis"
      className="flex min-w-0 flex-col gap-2"
      data-testid="rag-engine-synthesize"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <h4 className={sectionTitle}>Synthesis mode</h4>
        <RagSynthModeBadge view={view} labelFor={labelFor} />
      </div>
      {view.mode === MODEL_OVER_CONTEXTS_MODE ? (
        <p className="text-xs text-ink-2">
          Chunks returned by Vertex AI RAG Engine <code className="font-mono">:retrieveContexts</code> were passed to the selected catalog model for grounded synthesis.
        </p>
      ) : (
        <p className="text-xs text-ink-2">
          Gemini called <code className="font-mono">VertexRagStore</code> natively during <code className="font-mono">generate_content</code> to ground its response directly against the <code className="font-mono">RagCorpus</code>.
        </p>
      )}
      <KeyValue columns={2} label="Level 6 synthesis telemetry" items={items} />
    </section>
  );
};
