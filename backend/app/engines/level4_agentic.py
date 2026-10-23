"""Level 4: Agentic RAG with Google ADK (real ``LlmAgent`` + ``InMemoryRunner``; tools over real data).

Stages (spec §3), all emitted from inside this engine at the moment they happen (spec P1):

* ``query_prep`` — Level 3 acronym expansions + element intent; handed to the agent as context.
* ``agent_turn`` — ONE stage spanning the ADK loop. Every ADK event is forwarded immediately as an ``agent_turn`` SSE
  event (``kind`` model | tool_call | tool_result, measured ``duration_ms``, tokens from ``usage_metadata``). Tools:
  ``search_procurement_corpus`` (Level 3 retrieve + rerank), ``check_amendments`` (BigQuery ``amends_section``),
  ``calculate_contract_pricing`` (AST-safe), ``verify_citations`` (claims ↔ cited chunks; one repair turn required).
* ``synthesize`` — the ADK final response (no extra model call): model, tokens summed over turns, answer.
* ``evaluate`` — owned by the evaluator; the judge sees every chunk the tools actually returned (deduped).

Fail loud (spec P2): ADK/tool infrastructure errors, max turns, max tool calls per turn, timeout, a final answer
without a tool-returned citation → ``stage_failed(agent_turn)`` → ``engine_error``. There is no procedural fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.core.models import AgentStep, Citation, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, rank_cost_usd
from app.core.telemetry import BaseStageEmitter, StageEmitter
from app.engines.agent_ledger import AgentLedger
from app.engines.agent_runtime import (
    MODEL_CALL_ATTEMPTS,
    agent_instruction,
    AgentLimits,
    AgentOutcome,
    AgentProtocolError,
    build_agent,
    ensure_agent_model,
    run_agent,
)
from app.engines.agent_tools import TOOL_NAMES, ToolDeps, build_tools
from app.engines.agent_verify import check_claims
from app.engines.base import BaseRAGEngine
from app.engines.level2_hybrid import ACRONYM_EXPANSIONS
from app.engines.level3_multimodal import ELEMENT_INTENT_KEYWORDS, L3PreparedQuery, Level3MultimodalEngine, prepare_l3_query

FINAL_ANSWER_SOURCE = "adk_final_response"
MAX_CITATIONS = 10
MAX_JUDGE_CHUNKS = 24
DEADLINE_MARGIN_SECONDS = 2.0
_CITED_LINE_RE = re.compile(r"^[ \t>*_`]*CITED_CHUNK_IDS\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ID_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass(frozen=True)
class ResolvedCitations:
    answer: str
    chunks: tuple[DocumentChunk, ...]
    rejected_ids: tuple[str, ...]
    source: str  # "cited_chunk_ids_line" | "inline_chunk_ids"


def resolve_citations(final_text: str, ledger: AgentLedger) -> ResolvedCitations:
    """Citations = chunk ids the agent lists that a tool really returned this run (others are rejected, reported)."""
    matches = list(_CITED_LINE_RE.finditer(final_text))
    if matches:
        listed = [t.strip("`[]()'\"") for t in _ID_SPLIT_RE.split(matches[-1].group(1)) if t.strip("`[]()'\"")]
        answer = _CITED_LINE_RE.sub("", final_text).strip()
        source = "cited_chunk_ids_line"
    else:
        listed = [c.chunk_id for c in ledger.chunks if c.chunk_id in final_text]
        answer, source = final_text.strip(), "inline_chunk_ids"
    listed = list(dict.fromkeys(listed))
    valid = tuple(ledger.chunk(i) for i in listed if ledger.chunk(i) is not None)
    rejected = tuple(i for i in listed if ledger.chunk(i) is None)
    if not valid:
        raise AgentProtocolError(
            f"final answer cites no chunk returned by a tool this run (listed: {listed[:10]}; source: {source})"
        )
    if not answer:
        raise AgentProtocolError("final answer is empty after removing the CITED_CHUNK_IDS line")
    return ResolvedCitations(answer=answer, chunks=valid[:MAX_CITATIONS], rejected_ids=rejected, source=source)


def verification_summary(ledger: AgentLedger) -> dict[str, Any]:
    """unsupported_claims_before/after + repair_turns from the agent's own verify_citations calls."""
    reports = ledger.verifications
    if not reports:
        return {"verify_calls": 0, "unsupported_claims_before": None, "unsupported_claims_after": None, "repair_turns": 0}
    repairs = sum(1 for prev in reports[:-1] if prev.unsupported or prev.unknown_chunk_ids)
    return {
        "verify_calls": len(reports),
        "unsupported_claims_before": len(reports[0].unsupported),
        "unsupported_claims_after": len(reports[-1].unsupported),
        "unknown_chunk_ids_before": len(reports[0].unknown_chunk_ids),
        "unknown_chunk_ids_after": len(reports[-1].unknown_chunk_ids),
        "unsupported_before_list": [c.text for c in reports[0].unsupported][:20],
        "unsupported_after_list": [c.text for c in reports[-1].unsupported][:20],
        "repair_turns": repairs,
    }


def final_answer_check(answer: str, cited: tuple[DocumentChunk, ...], ledger: AgentLedger) -> dict[str, Any]:
    """Engine-side re-check of the FINAL answer with the same verifier (measurement only; the answer is not edited)."""
    texts = [f"{c.content}\n{c.section_id or ''}\n{c.amends_section or ''}" for c in cited]
    checks = check_claims(answer, texts, ledger.calc_values)
    unsupported = [c.claim.text for c in checks if not c.supported_by]
    return {"checked_claims": len(checks), "unsupported_claims": len(unsupported), "unsupported_list": unsupported[:20]}


def user_message(prepared: L3PreparedQuery) -> str:
    lines = [f"Question: {prepared.base.original}"]
    if prepared.base.expansions:
        lines.append("Acronyms: " + "; ".join(f"{t} = {e}" for t, e in prepared.base.expansions))
    if prepared.element_intent:
        lines.append("Element intent (keywords found): " + ", ".join(prepared.element_intent))
    return "\n".join(lines)


def _tool_calls_by_name(ledger: AgentLedger) -> dict[str, int]:
    counts = {name: 0 for name in TOOL_NAMES}
    for record in ledger.tool_records:
        counts[record.tool_name] = counts.get(record.tool_name, 0) + 1
    return counts


def _layout_source(chunks: tuple[DocumentChunk, ...]) -> str:
    sources = sorted({c.layout_source for c in chunks if c.layout_source in ("documentai_layout", "gemini_vision")})
    return "+".join(sources)


class Level4AgenticEngine(BaseRAGEngine):
    """Level 4: Google ADK agent (Gemini) choosing among four real-data tools."""

    engine_id: str = "level4_agentic"
    engine_label: str = "Level 4: Agentic RAG (Google ADK)"
    tier_profile: str = "multimodal"
    stages: tuple[str, ...] = ("query_prep", "agent_turn", "synthesize", "evaluate")

    def __init__(self, settings: ArenaSettings | None = None, *, synthesis_model: ModelSpec | None = None, retriever: Level3MultimodalEngine | None = None) -> None:
        super().__init__(settings=settings)
        if synthesis_model is not None and not isinstance(synthesis_model, ModelSpec):
            raise TypeError("synthesis_model must be a ModelSpec or None")
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(self.settings, self.settings.gemini_fast_model)
        ensure_agent_model(self.synthesis_model)
        self._retriever = retriever or Level3MultimodalEngine(settings=self.settings, synthesis_model=None)

    def limits(self, budget_seconds: float) -> AgentLimits:
        return AgentLimits(
            max_turns=self.settings.agent_max_turns,
            max_tool_calls_per_turn=self.settings.agent_max_tool_calls_per_turn,
            budget_seconds=max(1.0, budget_seconds),
        )

    async def _query_prep(self, query: str, emitter: StageEmitter) -> L3PreparedQuery:
        async with self.stage(
            emitter, "query_prep", label="Query preparation (acronyms + element intent)", service="Engine (local)",
            request={"query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS],
                     "intent_keywords": {i: list(w) for i, w in ELEMENT_INTENT_KEYWORDS}},
        ) as ctx:
            prepared = prepare_l3_query(query)
            ctx.payload = {**prepared.payload(), "handed_to_agent": user_message(prepared)}
        return prepared

    def _agent_request(self, message: str, limits: AgentLimits) -> dict[str, Any]:
        return {
            "framework": "google-adk", "agent": "LlmAgent", "runner": "InMemoryRunner", "method": "runner.run_async",
            "model": self.synthesis_model.id, "provider": self.synthesis_model.provider, "location": self.synthesis_model.location,
            "thinking_level": self.settings.agent_thinking_level.upper(), "instruction": agent_instruction(limits.max_tool_calls_per_turn),
            "tools": list(TOOL_NAMES), "user_message": message, "max_turns": limits.max_turns,
            "max_tool_calls_per_turn": limits.max_tool_calls_per_turn, "timeout_seconds": round(limits.budget_seconds, 1),
            "model_call_timeout_seconds": self.settings.agent_model_call_timeout_seconds, "model_call_attempts": MODEL_CALL_ATTEMPTS,
            "max_output_tokens_per_turn": self.settings.synthesis_max_tokens,
        }

    def _agent_payload(self, outcome: AgentOutcome, ledger: AgentLedger, cites: ResolvedCitations) -> dict[str, Any]:
        usage = ledger.usage
        return {
            "turns": len(outcome.turns), "agent_turn_events": outcome.agent_turn_events,
            "tool_calls": len(ledger.tool_records), "tool_calls_by_name": _tool_calls_by_name(ledger),
            "tool_errors": sum(1 for r in ledger.tool_records if r.status == "error"),
            **verification_summary(ledger),
            "final_answer_check": final_answer_check(cites.answer, cites.chunks, ledger),
            "final_answer_source": FINAL_ANSWER_SOURCE, "final_turn_index": outcome.final_turn_index,
            "tokens": outcome.tokens().to_dict(),
            "citations": [c.chunk_id for c in cites.chunks], "citation_source": cites.source,
            "rejected_citation_ids": list(cites.rejected_ids), "tool_chunks_returned": len(ledger.chunks),
            "retrieval_usage": {"embedding_tokens": usage.embedding_tokens, "rank_billed_counts": usage.rank_billed_counts,
                                "bigquery_bytes": usage.bigquery_bytes, "bigquery_jobs": usage.bigquery_jobs, "search_calls": usage.search_calls},
            "calculator_values": list(ledger.calc_values),
        }

    async def _agent_turn(self, prepared: L3PreparedQuery, deps: ToolDeps, emitter: BaseStageEmitter, deadline: float) -> tuple[AgentOutcome, ResolvedCitations]:
        message = user_message(prepared)
        limits = self.limits(deadline - time.monotonic())
        async with self.stage(
            emitter, "agent_turn", label="ADK agent loop (tools)", service=f"Google ADK · Vertex AI · {self.synthesis_model.label}",
            request=self._agent_request(message, limits),
        ) as ctx:
            agent = build_agent(self.synthesis_model, self.settings, build_tools(deps))
            outcome = await run_agent(agent, message, limits=limits, ledger=deps.ledger, emitter=emitter)
            cites = resolve_citations(outcome.final_text, deps.ledger)
            ctx.payload = self._agent_payload(outcome, deps.ledger, cites)
        return outcome, cites

    async def _synthesize(self, outcome: AgentOutcome, answer: str, emitter: StageEmitter) -> TokenUsage:
        spec, tokens = self.synthesis_model, outcome.tokens()
        final = next(t for t in outcome.turns if t.turn_index == outcome.final_turn_index)
        async with self.stage(
            emitter, "synthesize", label="Final response (ADK agent)", service=f"Google ADK · Vertex AI · {spec.label}",
            request={"model": spec.id, "provider": spec.provider, "source": FINAL_ANSWER_SOURCE, "final_turn_index": final.turn_index,
                     "note": "no extra model call — the answer is the ADK runner's final response (measured in agent_turn)"},
        ) as ctx:
            ctx.payload = {
                "model": spec.id, "provider": spec.provider, "thinking_level": self.settings.agent_thinking_level.upper(),
                "prompt_tokens": tokens.prompt, "output_tokens": tokens.output, "thinking_tokens": tokens.thinking,
                "turns": len(outcome.turns), "final_turn": {"turn_index": final.turn_index, "duration_ms": final.duration_ms, "tokens": final.tokens.to_dict()},
                "final_answer_source": FINAL_ANSWER_SOURCE, "answer": answer,
            }
        return TokenUsage(prompt_tokens=tokens.prompt, output_tokens=tokens.output, thinking_tokens=tokens.thinking,
                          model=spec.id, embedding_model=self.settings.multimodal_embedding_model)

    def _result(self, cites: ResolvedCitations, ledger: AgentLedger, usage: TokenUsage, start: float, emitter: StageEmitter) -> EngineResult:
        usage = TokenUsage(prompt_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens, thinking_tokens=usage.thinking_tokens,
                           embedding_tokens=ledger.usage.embedding_tokens, model=usage.model, embedding_model=usage.embedding_model)
        steps = [AgentStep(step_number=i, tool_name=r.tool_name, tool_args=r.args, result_summary=r.summary, latency_ms=r.duration_ms)
                 for i, r in enumerate(ledger.tool_records, start=1)]
        citations: list[Citation] = self.chunks_to_citations(list(cites.chunks), max_citations=len(cites.chunks))
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer=cites.answer, citations=citations,
            retrieved_chunks=list(ledger.chunks[:MAX_JUDGE_CHUNKS]), agent_steps=steps,
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + rank_cost_usd(ledger.usage.rank_billed_counts), 8),
            stage_log=self.stage_log_of(emitter), usage=usage,
            flags=EngineFlags(reranker_used=ledger.usage.rank_billed_counts > 0, adk_runner_used=True, layout_source=_layout_source(ledger.chunks)),
        )

    async def run_query(self, query: str, *, question_id: str | None, solicitation_filter: str | None, emitter: StageEmitter) -> EngineResult:
        """prep → ADK agent loop (agent_turn events live) → final response; live/record only."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("Level4AgenticEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        if solicitation_filter:
            raise ValueError("Level 4 chooses solicitation filters itself (tool arguments); solicitation_filter is not supported")
        if not isinstance(emitter, BaseStageEmitter):
            raise TypeError("Level 4 needs a BaseStageEmitter (agent_turn events)")
        start, deadline = time.perf_counter(), time.monotonic() + self.settings.query_timeout_seconds - DEADLINE_MARGIN_SECONDS
        ledger = AgentLedger(run_id=emitter.run_id)
        deps = ToolDeps(settings=self.settings, retriever=self._retriever, genai_client=get_genai_client(self.settings),
                        bq_client=get_bigquery_client(self.settings), ledger=ledger)
        prepared = await self._query_prep(cleaned, emitter)
        outcome, cites = await self._agent_turn(prepared, deps, emitter, deadline)
        usage = await self._synthesize(outcome, cites.answer, emitter)
        return self._result(cites, ledger, usage, start, emitter)


__all__ = [
    "FINAL_ANSWER_SOURCE", "Level4AgenticEngine", "ResolvedCitations", "final_answer_check", "resolve_citations",
    "user_message", "verification_summary",
]
