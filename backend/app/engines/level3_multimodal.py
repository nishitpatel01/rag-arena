"""Level 3: Layout-Aware Multimodal RAG (Document AI Layout Parser + Gemini vision + gemini-embedding-2 multimodal).

Stages ``query_prep → embed_query → retrieve → rerank → synthesize`` are emitted from inside this engine at the
moment they start/end (spec P1); ``evaluate`` is owned by the evaluator. Live/record only (replay is served by
``ReplayRAGEngine``); every failure surfaces as ``stage_failed`` → ``engine_error`` (spec P2).

* ``query_prep`` — Level 2 acronym expansions + ``element_intent`` (table / diagram / amendment keywords found).
* ``embed_query`` — ``MULTIMODAL_EMBEDDING_MODEL`` (RETRIEVAL_QUERY side) — same space as the ingested
  ``embedding_multimodal`` vectors (text chunks text-only; diagram chunks text + page image).
* ``retrieve`` — one RRF statement: ``VECTOR_SEARCH`` on ``embedding_multimodal`` + ``SEARCH(content)`` over tier
  ``multimodal`` (optional ``solicitation_id`` pre-filter); per candidate ``element_type``, ``layout_source``,
  ``amendment_number``, ``amends_section``.
* ``rerank`` — Discovery Engine Ranking API (Level 2 contract; no fallback ranker).
* ``synthesize`` — context ordered by authority (latest amendment first) with headers such as
  ``[AMENDMENT 0001 · effective 2026-02-01 · SUPERSEDES SEC-4.2]`` and element tags ``[TABLE]`` /
  ``[DIAGRAM transcription]``; payload ``authority_headers`` + ``element_mix``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.core.models import DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient
from app.core.telemetry import StageEmitter
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level2_hybrid import (
    ACRONYM_EXPANSIONS,
    LEXICAL_K,
    RRF_K,
    VECTOR_K,
    HybridCandidate,
    Level2HybridEngine,
    PreparedQuery,
    RerankedCandidate,
    prepare_query,
    row_to_candidate,
    run_hybrid_query_job,
)
from app.engines.synthesis import SYNTHESIS_INSTRUCTION
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, build_lexical_terms

EMBEDDING_COLUMN = "embedding_multimodal"
_MAX_CONTEXT_CHUNK_CHARS = 6000
_MAX_CANDIDATES_IN_PAYLOAD = 50
_EMBEDDING_PREVIEW_DIMS = 4
# Deterministic intent keywords (data, not logic): every match is listed in the query_prep payload.
ELEMENT_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("table", ("table", "rate", "rates", "price", "pricing", "cost", "clin", "matrix", "tier", "tiers", "hourly",
               "labor", "credit", "credits", "volume", "schedule", "penalty", "percentage", "fee")),
    ("diagram", ("diagram", "topology", "architecture", "flow", "component", "components", "connected", "connects",
                 "failover", "exhibit", "pep", "control plane", "network", "pipeline", "medallion")),
    ("amendment", ("amendment", "amendments", "amend", "amended", "supersede", "supersedes", "superseded",
                   "revised", "revision", "current", "latest", "effective", "updated", "change", "changed")),
)
L3_INSTRUCTION = (
    SYNTHESIS_INSTRUCTION
    + " Each chunk starts with an authority header. When an [AMENDMENT …] chunk SUPERSEDES a section, the amendment's"
    " terms govern over the base document for that section — state the superseded value and the governing value."
    " [TABLE] chunks are Markdown tables (read values by row and column header). [DIAGRAM transcription] chunks list"
    " the components and connections a vision model transcribed from the diagram image."
)
_ELEMENT_TAGS = {"table": "[TABLE]", "diagram": "[DIAGRAM transcription]"}


@dataclass(frozen=True)
class L3PreparedQuery:
    base: PreparedQuery
    element_intent: tuple[str, ...]
    intent_matches: tuple[tuple[str, tuple[str, ...]], ...]

    def payload(self) -> dict[str, Any]:
        return {
            **self.base.payload(),
            "element_intent": list(self.element_intent),
            "intent_matches": {intent: list(words) for intent, words in self.intent_matches},
        }


def detect_element_intent(query: str) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """Intents whose keywords occur as whole words/phrases in the query (case-insensitive), in table order."""
    lowered = " ".join(query.lower().split())
    matches: list[tuple[str, tuple[str, ...]]] = []
    for intent, words in ELEMENT_INTENT_KEYWORDS:
        hit = tuple(w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", lowered))
        if hit:
            matches.append((intent, hit))
    return tuple(i for i, _ in matches), tuple(matches)


def prepare_l3_query(query: str) -> L3PreparedQuery:
    base = prepare_query(query)
    intents, matches = detect_element_intent(base.original)
    return L3PreparedQuery(base=base, element_intent=intents, intent_matches=matches)


def _split_sections(value: str | None) -> tuple[str, ...]:
    return tuple(s.strip() for s in (value or "").split(",") if s.strip())


def authority_order(chunks: list[DocumentChunk]) -> list[DocumentChunk]:
    """Latest amendment first (amendment_number desc, then effective_date desc), then base chunks in rerank order."""
    amendments = [c for c in chunks if c.amendment_number > 0]
    base = [c for c in chunks if c.amendment_number <= 0]
    amendments.sort(key=lambda c: (c.amendment_number, c.effective_date or ""), reverse=True)
    return amendments + base


def authority_header(chunk: DocumentChunk, superseded_by: tuple[int, ...] = ()) -> str:
    """``[AMENDMENT 0001 · effective 2026-02-01 · SUPERSEDES SEC-4.2]`` / ``[BASE · TECHNICAL_EXHIBIT · effective …]``."""
    eff = f" · effective {chunk.effective_date}" if chunk.effective_date else ""
    if chunk.amendment_number > 0:
        targets = ",".join(_split_sections(chunk.amends_section)) or "UNSPECIFIED SECTION"
        head = f"[AMENDMENT {chunk.amendment_number:04d}{eff} · SUPERSEDES {targets}]"
    else:
        head = f"[BASE · {chunk.doc_type}{eff}]"
        if superseded_by:
            head = head[:-1] + " · SUPERSEDED BY AMENDMENT " + ", ".join(f"{n:04d}" for n in superseded_by) + "]"
    tag = _ELEMENT_TAGS.get(chunk.element_type, "[TEXT]")
    section = chunk.section_id or "GENERAL"
    return f"{head} {tag} {chunk.doc_name} · p.{chunk.page_number} · {section}"


def build_authority_context(chunks: list[DocumentChunk]) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """(context block, authority_headers payload list, element_mix) for the synthesis prompt."""
    ordered = authority_order(chunks)
    amended: dict[str, list[int]] = {}
    for c in ordered:
        for section in _split_sections(c.amends_section) if c.amendment_number > 0 else ():
            amended.setdefault(section, []).append(c.amendment_number)
    blocks: list[str] = []
    headers: list[dict[str, Any]] = []
    mix: dict[str, int] = {}
    for c in ordered:
        superseded_by = tuple(sorted(set(amended.get(c.section_id or "", [])), reverse=True)) if c.amendment_number <= 0 else ()
        header = authority_header(c, superseded_by)
        blocks.append(f"{header}\n{c.content[:_MAX_CONTEXT_CHUNK_CHARS]}")
        headers.append({
            "chunk_id": c.chunk_id, "header": header, "amendment_number": c.amendment_number,
            "effective_date": c.effective_date,
            "supersedes": list(_split_sections(c.amends_section)) if c.amendment_number > 0 else [],
            "superseded_by": list(superseded_by), "element_type": c.element_type, "layout_source": c.layout_source,
        })
        mix[c.element_type] = mix.get(c.element_type, 0) + 1
    return "\n\n".join(blocks), headers, mix


def format_chunk_with_authority_header(chunk: DocumentChunk) -> str:
    """One chunk with its authority header (public helper; used by tests and the Level 4 tool body)."""
    if not isinstance(chunk, DocumentChunk):
        raise TypeError("chunk must be a DocumentChunk instance")
    if not chunk.content.strip():
        raise ValueError("chunk.content must be a non-empty string")
    return f"{authority_header(chunk)}\n{chunk.content}"


def candidate_summary(c: HybridCandidate) -> dict[str, Any]:
    return {
        **c.summary(),
        "element_type": c.chunk.element_type, "layout_source": c.chunk.layout_source, "bbox_source": c.chunk.bbox_source,
        "amendment_number": c.chunk.amendment_number, "amends_section": c.chunk.amends_section,
        "effective_date": c.chunk.effective_date, "doc_type": c.chunk.doc_type,
    }


def row_to_l3_candidate(row: Any, rrf_rank: int) -> HybridCandidate:
    base = row_to_candidate(row, rrf_rank, tier_profile="multimodal")
    chunk = replace(
        base.chunk,
        layout_source=str(getattr(row, "layout_source", "") or ""),
        bbox_source=str(getattr(row, "bbox_source", "") or ""),
    )
    return replace(base, chunk=chunk)


class Level3MultimodalEngine(Level2HybridEngine):
    """Level 3: hybrid RRF over layout-aware multimodal chunks + Ranking API + authority-headed synthesis."""

    engine_id: str = "level3_multimodal"
    engine_label: str = "Level 3: Layout-Aware Multimodal RAG"
    tier_profile: str = "multimodal"
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
    ) -> None:
        super().__init__(settings=settings, synthesis_model=synthesis_model, ranking_client=ranking_client)

    async def _query_prep_l3(self, query: str, emitter: StageEmitter) -> L3PreparedQuery:
        async with self.stage(
            emitter, "query_prep", label="Query preparation (acronyms + element intent)", service="Engine (local)",
            request={
                "query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS],
                "intent_keywords": {i: list(w) for i, w in ELEMENT_INTENT_KEYWORDS},
            },
        ) as ctx:
            prepared = prepare_l3_query(query)
            ctx.payload = prepared.payload()
        return prepared

    def _retrieve_request(self, vector: list[float], terms: tuple[str, ...], solicitation_filter: str | None) -> tuple[str, dict[str, Any]]:
        sql = build_hybrid_rrf_sql(
            self.settings.gcp_project_id, self.settings.bq_dataset, self.settings.bq_table, rrf_k=RRF_K,
            top_k=self.settings.hybrid_candidate_k, include_solicitation_filter=bool(solicitation_filter),
            embedding_column=EMBEDDING_COLUMN, tier_profile=self.tier_profile, vector_k=VECTOR_K, lexical_k=LEXICAL_K,
        )
        params: dict[str, Any] = {
            "query_embedding": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
            "lexical_query": " ".join(terms), "lexical_terms": list(terms),
        }
        if solicitation_filter:
            params["solicitation_id"] = solicitation_filter
        return sql, params

    async def _retrieve(
        self, vector: list[float], original_query: str, solicitation_filter: str | None, bq_client: Any, emitter: StageEmitter
    ) -> list[HybridCandidate]:
        terms = build_lexical_terms(original_query)
        sql, params = self._retrieve_request(vector, terms, solicitation_filter)
        k = self.settings.hybrid_candidate_k
        async with self.stage(
            emitter, "retrieve", label="BigQuery VECTOR_SEARCH(embedding_multimodal) + SEARCH() → RRF", service="BigQuery",
            request={"sql": sql, "params": params, "candidate_k": k, "embedding_column": EMBEDDING_COLUMN,
                     "tier_profile": self.tier_profile, "pre_filter": {"solicitation_id": solicitation_filter}},
        ) as ctx:
            rows, stats = await self.blocking(
                run_hybrid_query_job, bq_client, sql, vector, terms, solicitation_filter, self.settings.query_timeout_seconds
            )
            candidates = [row_to_l3_candidate(r, idx) for idx, r in enumerate(rows[:k], start=1)]
            if not candidates:
                raise RuntimeError("multimodal retrieval returned 0 rows for tier 'multimodal' — run the Level 3 ingestion job")
            ctx.payload = self._retrieve_payload(rows, candidates, stats)
        return candidates

    def _retrieve_payload(self, rows: list[Any], candidates: list[HybridCandidate], stats: dict[str, Any]) -> dict[str, Any]:
        def count(attr: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for c in candidates:
                key = str(getattr(c.chunk, attr) or "")
                out[key] = out.get(key, 0) + 1
            return out

        return {
            "rows_returned": len(rows), "candidate_k": self.settings.hybrid_candidate_k, **stats,
            "rrf_k": RRF_K, "vector_k": VECTOR_K, "lexical_k": LEXICAL_K, "embedding_column": EMBEDDING_COLUMN,
            "with_both_ranks": sum(1 for c in candidates if c.vector_rank and c.lexical_rank),
            "element_types": count("element_type"), "layout_sources": count("layout_source"),
            "amendment_candidates": sum(1 for c in candidates if c.chunk.amendment_number > 0),
            "candidates": [candidate_summary(c) for c in candidates[:_MAX_CANDIDATES_IN_PAYLOAD]],
        }

    async def _synthesize_l3(
        self, query: str, chunks: list[DocumentChunk], emitter: StageEmitter, embedding_tokens: int
    ) -> tuple[str, TokenUsage]:
        context_block, headers, mix = build_authority_context(chunks)
        return await synthesize_stage(
            self, self.synthesis_model, query, context_block, len(chunks), emitter, embedding_tokens,
            instruction=L3_INSTRUCTION, embedding_model=self.settings.multimodal_embedding_model,
            extra_payload={"authority_headers": headers, "element_mix": mix, "context_order": "latest amendment first, then rerank order"},
        )

    async def run_query(
        self, query: str, *, question_id: str | None, solicitation_filter: str | None, emitter: StageEmitter
    ) -> EngineResult:
        """Execute Level 3 live: prep → multimodal embed → RRF(embedding_multimodal) → rerank → authority synthesis."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("Level3MultimodalEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        start = time.perf_counter()
        genai_client = get_genai_client(self.settings)
        bq_client = get_bigquery_client(self.settings)
        prepared = await self._query_prep_l3(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(
            self, prepared.base.prepared, genai_client, emitter, model=self.settings.multimodal_embedding_model
        )
        candidates = await self._retrieve(vector, prepared.base.original, solicitation_filter, bq_client, emitter)
        ranked: list[RerankedCandidate] = await self._rerank(prepared.base.original, candidates, emitter)
        top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.settings.rerank_top_n]]
        answer, usage = await self._synthesize_l3(cleaned, top, emitter, embedding_tokens)
        rank_cost = rank_cost_usd(rank_billed_counts(len(candidates)))
        sources = "+".join(sorted({c.layout_source for c in top if c.layout_source})) or "documentai_layout"
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer=answer,
            citations=self.chunks_to_citations(top, max_citations=len(top)), retrieved_chunks=top, agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + rank_cost, 8),
            stage_log=self.stage_log_of(emitter), usage=usage,
            flags=EngineFlags(reranker_used=True, adk_runner_used=False, layout_source=sources),
        )


__all__ = [
    "ELEMENT_INTENT_KEYWORDS",
    "EMBEDDING_COLUMN",
    "L3_INSTRUCTION",
    "Level3MultimodalEngine",
    "authority_header",
    "authority_order",
    "build_authority_context",
    "candidate_summary",
    "detect_element_intent",
    "format_chunk_with_authority_header",
    "prepare_l3_query",
    "row_to_l3_candidate",
]
