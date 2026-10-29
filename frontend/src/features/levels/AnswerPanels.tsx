import React from "react";
import type { Citation, EngineTimeline } from "../../types/arena";
import { EmptyState } from "../../components/ui/EmptyState";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { MiniMarkdown } from "../../components/ui/MiniMarkdown";
import { formatMs, formatPct, formatUsd } from "../../lib/format";
import { ELEMENT_LABEL } from "../../lib/elements";
import { AlignLeft, Network, Table2 } from "lucide-react";
import { parseManagedSynthesize } from "./level5Payloads";
import { SynthModeBadge } from "./level5Views";

export interface AnswerPanelProps {
  readonly timeline: EngineTimeline | undefined;
  readonly running: boolean;
  readonly selectedCitation: Citation | null;
  readonly onSelectCitation: (c: Citation) => void;
  /** Catalog label for a model id (Level 5 mode badge). */
  readonly labelFor?: (id: string | null) => string | null;
}

export const AnswerPanel: React.FC<AnswerPanelProps> = ({ timeline, running, selectedCitation, onSelectCitation, labelFor }) => {
  const answer = timeline?.answer ?? null;
  // Level 5: which synthesis path produced this answer (managed summary vs catalog model over segments).
  const managedSynth = parseManagedSynthesize(timeline?.stages.synthesize?.payload ?? null);
  if (timeline?.error && !answer) {
    return (
      <div role="alert" className="rounded-md border border-err p-3 text-xs">
        <p className="font-mono font-semibold text-err">
          {timeline.error.type}
          {timeline.error.failedStage ? ` @ ${timeline.error.failedStage}` : ""}
        </p>
        <p className="mt-1 break-words text-ink-1">{timeline.error.message}</p>
      </div>
    );
  }
  if (!answer) {
    return <EmptyState compact title={running ? "Waiting for answer_complete…" : "No answer yet"} description="The answer appears exactly when the engine returns it." />;
  }
  return (
    <div className="flex flex-col gap-3">
      <MiniMarkdown text={answer.answer} />
      <div className="flex flex-wrap gap-1.5">
        {managedSynth && <SynthModeBadge view={managedSynth} labelFor={labelFor} />}
        <Pill tone={answer.flags.reranker_used ? "ok" : "neutral"}>reranker {answer.flags.reranker_used ? "used" : "not used"}</Pill>
        <Pill tone={answer.flags.adk_runner_used ? "ok" : "neutral"}>ADK runner {answer.flags.adk_runner_used ? "used" : "not used"}</Pill>
        {answer.flags.layout_source && <Pill tone="info" mono>layout: {answer.flags.layout_source}</Pill>}
      </div>
      <div>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-3">Citations ({answer.citations.length})</h4>
        {answer.citations.length === 0 ? (
          <p className="text-xs text-clay">No citations returned.</p>
        ) : (
          <ol className="flex flex-wrap gap-1.5">
            {answer.citations.map((c, i) => {
              const selected = selectedCitation?.chunk_id === c.chunk_id;
              return (
                <li key={`${c.chunk_id}-${i}`}>
                  <button
                    type="button"
                    aria-pressed={selected}
                    aria-haspopup="dialog"
                    title={`Open ${c.doc_name} p.${c.page_number} in the document viewer`}
                    onClick={() => onSelectCitation(c)}
                    className={`rounded-md border px-2 py-1 text-left text-xs hover:bg-surface-2 ${selected ? "border-slate text-slate" : "border-line text-ink-2"}`}
                  >
                    <span className="font-mono">[{i + 1}]</span> {c.doc_name} p.{c.page_number}
                    {c.section_id ? ` · ${c.section_id}` : ""}
                    <CitationElementTag elementType={c.element_type} layoutSource={c.layout_source ?? null} />
                    {c.bbox_source === "page" && c.element_type !== "table" && c.element_type !== "diagram" && (
                      <span className="ml-1 font-mono text-ink-3" title="bbox_source page: page-level citation from the extractive segment's page number (no region located)">
                        · page-level
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
};

const CITATION_ICON = { table: Table2, diagram: Network, narrative_text: AlignLeft } as const;

/** Inline TABLE / DIAGRAM tag on Level 3 citation chips (shown only when the server reported layout_source). */
const CitationElementTag: React.FC<{ readonly elementType: string; readonly layoutSource: string | null }> = ({ elementType, layoutSource }) => {
  if (!layoutSource || (elementType !== "table" && elementType !== "diagram")) return null;
  const Icon = CITATION_ICON[elementType];
  return (
    <span className="ml-1 inline-flex items-center gap-0.5 font-mono font-semibold text-slate">
      <Icon className="h-3 w-3" aria-hidden />
      {ELEMENT_LABEL[elementType]}
    </span>
  );
};

export interface MetricsPanelProps {
  readonly timeline: EngineTimeline | undefined;
  readonly running: boolean;
  /** `run_started.synthesis_model` (fallback when `answer_complete.usage.model` is absent). */
  readonly runSynthesisModel?: string | null;
  /** Catalog label for a model id (returns the id itself when unknown). */
  readonly labelFor?: (id: string) => string | null;
}

/** "Label (id)" when the catalog knows the id, else the id verbatim. */
function modelText(id: string | null | undefined, labelFor?: (id: string) => string | null): string {
  if (!id) return "—";
  const label = labelFor?.(id) ?? id;
  return label === id ? id : `${label} (${id})`;
}

export const MetricsPanel: React.FC<MetricsPanelProps> = ({ timeline, running, runSynthesisModel, labelFor }) => {
  const answer = timeline?.answer ?? null;
  const ev = timeline?.evaluation ?? null;
  if (!answer && !ev) {
    return <EmptyState compact title={running ? "Judge pending…" : "No metrics"} description="Scores come only from eval_complete (LLM judge)." />;
  }
  const synthesisId = answer?.usage.model ?? runSynthesisModel ?? null;
  return (
    <div className="flex flex-col gap-3">
      <KeyValue
        label="Models used"
        items={[
          ["synthesis model", modelText(synthesisId, labelFor)],
          ["judge model", ev ? modelText(ev.judgeModel, labelFor) : running ? "pending eval_complete" : "—"],
        ]}
      />
      {ev ? (
        <>
          <KeyValue
            label="Judge metrics"
            items={[
              ["groundedness", formatPct(ev.metrics.groundedness)],
              ["qa_relevance", formatPct(ev.metrics.qa_relevance)],
              ["context_precision", formatPct(ev.metrics.context_precision)],
            ]}
          />
          <div className="flex flex-wrap gap-1.5">
            <Pill tone="info" mono>judge: {labelFor?.(ev.judgeModel) ?? ev.judgeModel}{ev.judgeLocation ? ` · ${ev.judgeLocation}` : ""}</Pill>
            {ev.referenceAnswerUsed ? (
              <Pill tone="ok">reference answer used</Pill>
            ) : (
              <Pill tone="warn" title="QA relevance judged directly against the question and retrieved context without a golden reference answer">
                no reference answer
              </Pill>
            )}
            <Pill tone="neutral" mono>{formatMs(ev.durationMs)}</Pill>
          </div>
          {ev.rationale ? (
            <blockquote className="border-l-2 border-slate pl-3 text-xs leading-relaxed text-ink-2">
              <span className="font-semibold text-ink-1">Judge rationale: </span>
              {ev.rationale}
            </blockquote>
          ) : null}
          {ev.inputTokens != null || ev.judgeCostUsd != null ? (
            <KeyValue
              label="Judge usage"
              items={[
                ["judge input tokens", ev.inputTokens != null ? ev.inputTokens.toLocaleString() : "—"],
                ["judge output tokens", ev.outputTokens != null ? ev.outputTokens.toLocaleString() : "—"],
                ["judge cost", ev.judgeCostUsd != null ? formatUsd(ev.judgeCostUsd) : "—"],
              ]}
            />
          ) : null}
        </>
      ) : (
        <p className="text-xs text-ink-3">{running ? "Waiting for eval_complete…" : "No evaluation received."}</p>
      )}
      {answer && (
        <KeyValue
          label="Cost and latency"
          items={[
            ["latency", formatMs(answer.latencyMs)],
            ["engine cost", formatUsd(answer.costUsd)],
            ["run cost (engine + judge)", ev?.runCostUsd != null ? formatUsd(ev.runCostUsd) : "pending judge"],
            ["prompt tokens", answer.usage.prompt_tokens.toLocaleString()],
            ["output tokens", answer.usage.output_tokens.toLocaleString()],
            ["embedding tokens", answer.usage.embedding_tokens.toLocaleString()],
          ]}
        />
      )}
    </div>
  );
};
