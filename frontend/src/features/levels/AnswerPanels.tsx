import React from "react";
import type { Citation, EngineTimeline } from "../../types/arena";
import { EmptyState } from "../../components/ui/EmptyState";
import { KeyValue } from "../../components/ui/KeyValue";
import { Pill } from "../../components/ui/Pill";
import { MiniMarkdown } from "../../components/ui/MiniMarkdown";
import { formatMs, formatPct, formatUsd } from "../../lib/format";

export interface AnswerPanelProps {
  readonly timeline: EngineTimeline | undefined;
  readonly running: boolean;
  readonly selectedCitation: Citation | null;
  readonly onSelectCitation: (c: Citation) => void;
}

export const AnswerPanel: React.FC<AnswerPanelProps> = ({ timeline, running, selectedCitation, onSelectCitation }) => {
  const answer = timeline?.answer ?? null;
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

export interface MetricsPanelProps {
  readonly timeline: EngineTimeline | undefined;
  readonly running: boolean;
}

export const MetricsPanel: React.FC<MetricsPanelProps> = ({ timeline, running }) => {
  const answer = timeline?.answer ?? null;
  const ev = timeline?.evaluation ?? null;
  if (!answer && !ev) {
    return <EmptyState compact title={running ? "Judge pending…" : "No metrics"} description="Scores come only from eval_complete (LLM judge)." />;
  }
  return (
    <div className="flex flex-col gap-3">
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
            <Pill tone="info" mono>judge: {ev.judgeModel}</Pill>
            <Pill tone={ev.referenceAnswerUsed ? "ok" : "neutral"}>
              reference answer {ev.referenceAnswerUsed ? "used" : "not used"}
            </Pill>
            <Pill tone="neutral" mono>{formatMs(ev.durationMs)}</Pill>
          </div>
        </>
      ) : (
        <p className="text-xs text-ink-3">{running ? "Waiting for eval_complete…" : "No evaluation received."}</p>
      )}
      {answer && (
        <KeyValue
          label="Cost and latency"
          items={[
            ["latency", formatMs(answer.latencyMs)],
            ["cost", formatUsd(answer.costUsd)],
            ["prompt tokens", answer.usage.prompt_tokens.toLocaleString()],
            ["output tokens", answer.usage.output_tokens.toLocaleString()],
            ["embedding tokens", answer.usage.embedding_tokens.toLocaleString()],
          ]}
        />
      )}
    </div>
  );
};
