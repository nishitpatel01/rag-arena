"""Level 2: BigQuery Hybrid (VECTOR_SEARCH + SEARCH() fused by RRF k=60) + Discovery Engine Ranking API.

Stages ``query_prep → embed_query → retrieve → rerank → synthesize`` are emitted from inside this engine at the
moment they start/end (spec P1); ``evaluate`` is owned by the evaluator. Live/record only (replay is served by
``ReplayRAGEngine``). Every failure — including a Ranking API error — surfaces as ``stage_failed`` and propagates
to ``engine_error`` (spec P2). There is no heuristic or lexical fallback ranker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token, get_bigquery_client, get_genai_client
from app.core.models import DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient, RankRecord, make_record
from app.core.telemetry import StageEmitter
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level1_naive import _row_bbox
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, build_lexical_terms

RRF_K = 60
VECTOR_K = 50
LEXICAL_K = 50
RANKING_SERVICE = "Discovery Engine Ranking API"
_EMBEDDING_PREVIEW_DIMS = 4
_MAX_CONTEXT_CHUNK_CHARS = 6000
_MAX_CANDIDATES_IN_PAYLOAD = 50

# Deterministic procurement/cloud acronym map (data, not logic): every applied expansion is listed in the payload.
ACRONYM_EXPANSIONS: tuple[tuple[str, str], ...] = (
    ("CLIN", "Contract Line Item Number"),
    ("SLA", "Service Level Agreement"),
    ("RTO", "Recovery Time Objective"),
    ("RPO", "Recovery Point Objective"),
    ("QASP", "Quality Assurance Surveillance Plan"),
    ("PEP", "Policy Enforcement Point"),
    ("SOW", "Statement of Work"),
    ("RFP", "Request for Proposal"),
    ("MSA", "Master Services Agreement"),
    ("ZTA", "Zero Trust Architecture"),
    ("VPC", "Virtual Private Cloud"),
    ("VPN", "Virtual Private Network"),
    ("BGP", "Border Gateway Protocol"),
    ("ASN", "Autonomous System Number"),
    ("KMS", "Key Management Service"),
    ("GKE", "Google Kubernetes Engine"),
)
_ACRONYM_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (acr, full, re.compile(rf"(?<![A-Za-z0-9]){re.escape(acr)}(?![A-Za-z])")) for acr, full in ACRONYM_EXPANSIONS
)


@dataclass(frozen=True)
class PreparedQuery:
    original: str
    prepared: str
    expansions: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "original_query": self.original,
            "prepared_query": self.prepared,
            "expansions": [{"term": t, "expansion": e} for t, e in self.expansions],
        }


def prepare_query(query: str) -> PreparedQuery:
    """Expand upper-case procurement acronyms (case-sensitive, whole token; ``CLIN-0004-AB`` counts as CLIN)."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query) > 4000:
        raise ValueError("query exceeds 4000 character bound")
    original = " ".join(query.split())
    found = tuple((acr, full) for acr, full, pattern in _ACRONYM_PATTERNS if pattern.search(original))
    if not found:
        return PreparedQuery(original=original, prepared=original, expansions=())
    glossary = "; ".join(f"{acr} = {full}" for acr, full in found)
    return PreparedQuery(original=original, prepared=f"{original} ({glossary})", expansions=found)


def expand_procurement_acronyms(query: str) -> str:
    """Prepared query text (kept for the Level 3 legacy body until Phase 3)."""
    return prepare_query(query).prepared


@dataclass(frozen=True)
class HybridCandidate:
    """One fused RRF row: the chunk (score = rrf_score) plus its measured per-leg ranks."""

    chunk: DocumentChunk
    rrf_rank: int
    rrf_score: float
    vector_rank: int | None
    lexical_rank: int | None
    vector_distance: float | None
    lexical_score: int | None

    def summary(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "doc_name": self.chunk.doc_name,
            "page_number": self.chunk.page_number,
            "section_id": self.chunk.section_id,
            "vector_rank": self.vector_rank,
            "lexical_rank": self.lexical_rank,
            "vector_distance": self.vector_distance,
            "rrf_score": self.rrf_score,
        }


@dataclass(frozen=True)
class RerankedCandidate:
    candidate: HybridCandidate
    rerank_rank: int
    rerank_score: float

    def summary(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "chunk_id": c.chunk.chunk_id,
            "doc_name": c.chunk.doc_name,
            "page_number": c.chunk.page_number,
            "rrf_rank": c.rrf_rank,
            "rrf_score": c.rrf_score,
            "rerank_rank": self.rerank_rank,
            "rerank_score": self.rerank_score,
        }


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: Any, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def row_to_candidate(row: Any, rrf_rank: int, tier_profile: str = "hybrid") -> HybridCandidate:
    """Convert one RRF result row; NULL vector/lexical ranks mean that leg did not return the chunk."""
    page_num = int(row.page_number)
    effective = getattr(row, "effective_date", None)
    rrf_score = round(float(row.rrf_score), 6)
    chunk = DocumentChunk(
        chunk_id=str(row.chunk_id),
        tier_profile=tier_profile,
        solicitation_id=str(row.solicitation_id),
        doc_name=str(row.doc_name),
        doc_type=str(row.doc_type),
        element_type=str(row.element_type),
        section_id=str(row.section_id) if getattr(row, "section_id", None) else None,
        amendment_number=int(getattr(row, "amendment_number", 0) or 0),
        effective_date=str(effective) if effective else None,
        amends_section=str(row.amends_section) if getattr(row, "amends_section", None) else None,
        page_number=page_num,
        bbox=_row_bbox(row, page_num),
        content=str(row.content),
        score=rrf_score,
    )
    return HybridCandidate(
        chunk=chunk,
        rrf_rank=rrf_rank,
        rrf_score=rrf_score,
        vector_rank=_opt_int(getattr(row, "vector_rank", None)),
        lexical_rank=_opt_int(getattr(row, "lexical_rank", None)),
        vector_distance=_opt_float(getattr(row, "vector_distance", None)),
        lexical_score=_opt_int(getattr(row, "lexical_score", None)),
    )


_MAX_INDEX_REASONS = 8


def _index_stats(raw: Any) -> dict[str, Any] | None:
    """Keep BigQuery's ``indexUsageMode`` and reason code/message/index name verbatim; drop ``baseTable`` (it echoes
    the project id)."""
    if not isinstance(raw, dict):
        return None
    reasons = raw.get("indexUnusedReasons") if isinstance(raw.get("indexUnusedReasons"), list) else []
    return {
        "index_usage_mode": raw.get("indexUsageMode"),
        "index_unused_reasons": [
            {k: r.get(k) for k in ("code", "message", "indexName") if k in r}
            for r in reasons[:_MAX_INDEX_REASONS]
            if isinstance(r, dict)
        ],
    }


def _index_usage(job: Any) -> dict[str, Any]:
    """BigQuery-reported index usage for SEARCH() and VECTOR_SEARCH (None when the job statistics omit it)."""
    stats = getattr(job, "_properties", {}).get("statistics", {}).get("query", {})
    stats = stats if isinstance(stats, dict) else {}
    return {"search": _index_stats(stats.get("searchStatistics")), "vector_search": _index_stats(stats.get("vectorSearchStatistics"))}


def run_hybrid_query_job(
    bq_client: Any,
    sql: str,
    embedding_vec: list[float],
    lexical_terms: tuple[str, ...],
    solicitation_filter: str | None,
    timeout_seconds: int,
) -> tuple[list[Any], dict[str, Any]]:
    """Execute the hybrid RRF statement (worker thread); return rows and measured job statistics."""
    from google.cloud import bigquery

    params: list[Any] = [
        bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", embedding_vec),
        bigquery.ScalarQueryParameter("lexical_query", "STRING", " ".join(lexical_terms)),
        bigquery.ArrayQueryParameter("lexical_terms", "STRING", list(lexical_terms)),
    ]
    if solicitation_filter:
        params.append(bigquery.ScalarQueryParameter("solicitation_id", "STRING", solicitation_filter.strip()))
    job = bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = list(job.result(timeout=timeout_seconds))
    stats = {
        "job_id": str(getattr(job, "job_id", "") or ""),
        "bytes_processed": int(getattr(job, "total_bytes_processed", 0) or 0),
        "slot_millis": int(getattr(job, "slot_millis", 0) or 0),
        "cache_hit": bool(getattr(job, "cache_hit", False)),
        "index_usage": _index_usage(job),
    }
    return rows, stats


def rank_title(chunk: DocumentChunk) -> str:
    return f"{chunk.doc_name} {chunk.section_id}" if chunk.section_id else chunk.doc_name


def build_rank_records(candidates: list[HybridCandidate]) -> list[RankRecord]:
    return [make_record(c.chunk.chunk_id, rank_title(c.chunk), c.chunk.content) for c in candidates]


def build_ranking_client(settings: ArenaSettings) -> RankingClient:
    """REST Ranking API client for the configured project/model (ADC bearer token per call)."""
    budget = float(settings.ranking_timeout_seconds)
    return RankingClient(
        project_id=settings.gcp_project_id,
        model=settings.reranker_model,
        attempt_timeout_seconds=budget,
        budget_seconds=budget,
        token_provider=lambda: get_access_token(settings),
    )


def apply_rank_scores(candidates: list[HybridCandidate], scores: tuple[tuple[str, float], ...]) -> list[RerankedCandidate]:
    """Order candidates by the API's scores (returned order = rerank rank); ids were validated by the client."""
    by_id = {c.chunk.chunk_id: c for c in candidates}
    return [
        RerankedCandidate(candidate=by_id[rid], rerank_rank=idx, rerank_score=round(score, 6))
        for idx, (rid, score) in enumerate(scores, start=1)
    ]


def rank_chunks_live(query: str, chunks: list[DocumentChunk], top_n: int, settings: ArenaSettings) -> list[DocumentChunk]:
    """Rerank chunks with the Ranking API (Level 3 legacy body until Phase 3); errors propagate (no fallback)."""
    if not settings.is_live:
        raise RuntimeError("Ranking API rerank requires DEMO_MODE=live|record")
    if not chunks:
        raise ValueError("rank_chunks_live needs at least one chunk")
    records = [make_record(c.chunk_id, rank_title(c), c.content) for c in chunks[:50]]
    response = build_ranking_client(settings).rank(query, records, top_n=len(records))
    by_id = {c.chunk_id: c for c in chunks}
    return [replace(by_id[rid], score=round(score, 4)) for rid, score in response.scores[:top_n]]


class Level2HybridEngine(BaseRAGEngine):
    """Level 2: hybrid VECTOR_SEARCH + SEARCH() RRF over contextual-lineage page chunks + Ranking API rerank."""

    engine_id: str = "level2_hybrid"
    engine_label: str = "Level 2: BigQuery Hybrid + Reranker"
    tier_profile: str = "hybrid"
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
    ) -> None:
        super().__init__(settings=settings)
        if synthesis_model is not None and not isinstance(synthesis_model, ModelSpec):
            raise TypeError("synthesis_model must be a ModelSpec or None")
        if ranking_client is not None and not isinstance(ranking_client, RankingClient):
            raise TypeError("ranking_client must be a RankingClient or None")
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(
            self.settings, self.settings.gemini_fast_model
        )
        self._ranking_client = ranking_client

    async def _query_prep(self, query: str, emitter: StageEmitter) -> PreparedQuery:
        async with self.stage(
            emitter,
            "query_prep",
            label="Query preparation (acronym expansion)",
            service="Engine (local)",
            request={"query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS]},
        ) as ctx:
            prepared = prepare_query(query)
            ctx.payload = prepared.payload()
        return prepared

    def _retrieve_request(self, vector: list[float], terms: tuple[str, ...], solicitation_filter: str | None) -> tuple[str, dict[str, Any]]:
        sql = build_hybrid_rrf_sql(
            self.settings.gcp_project_id,
            self.settings.bq_dataset,
            self.settings.bq_table,
            rrf_k=RRF_K,
            top_k=self.settings.hybrid_candidate_k,
            include_solicitation_filter=bool(solicitation_filter),
            tier_profile=self.tier_profile,
            vector_k=VECTOR_K,
            lexical_k=LEXICAL_K,
        )
        params: dict[str, Any] = {
            "query_embedding": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
            "lexical_query": " ".join(terms),
            "lexical_terms": list(terms),
        }
        if solicitation_filter:
            params["solicitation_id"] = solicitation_filter
        return sql, params

    async def _retrieve(
        self, vector: list[float], original_query: str, solicitation_filter: str | None, bq_client: Any, emitter: StageEmitter
    ) -> list[HybridCandidate]:
        terms = build_lexical_terms(original_query)
        sql, params = self._retrieve_request(vector, terms, solicitation_filter)
        candidate_k = self.settings.hybrid_candidate_k
        async with self.stage(
            emitter,
            "retrieve",
            label="BigQuery VECTOR_SEARCH + SEARCH() → RRF",
            service="BigQuery",
            request={"sql": sql, "params": params, "candidate_k": candidate_k},
        ) as ctx:
            rows, stats = await self.blocking(
                run_hybrid_query_job, bq_client, sql, vector, terms, solicitation_filter, self.settings.query_timeout_seconds
            )
            candidates = [row_to_candidate(r, idx) for idx, r in enumerate(rows[:candidate_k], start=1)]
            if not candidates:
                raise RuntimeError("hybrid retrieval returned 0 rows for tier 'hybrid' — run the Level 2 ingestion job")
            ctx.payload = {
                "rows_returned": len(rows),
                "candidate_k": candidate_k,
                **stats,
                "rrf_k": RRF_K,
                "vector_k": VECTOR_K,
                "lexical_k": LEXICAL_K,
                "with_both_ranks": sum(1 for c in candidates if c.vector_rank and c.lexical_rank),
                "candidates": [c.summary() for c in candidates[:_MAX_CANDIDATES_IN_PAYLOAD]],
            }
        return candidates

    async def _rerank(self, query: str, candidates: list[HybridCandidate], emitter: StageEmitter) -> list[RerankedCandidate]:
        client = self._ranking_client or build_ranking_client(self.settings)
        records = build_rank_records(candidates)
        top_n = len(records)
        async with self.stage(
            emitter,
            "rerank",
            label="Semantic rerank (cross-encoder)",
            service=RANKING_SERVICE,
            request={
                "service": RANKING_SERVICE, "model": client.model, "top_n": top_n, "records_sent": len(records),
                "query": query, "method": "rankingConfigs.rank", "ranking_config": "default_ranking_config",
                "record_content_max_chars": max((len(r.content) for r in records), default=0),
                "context_k": self.settings.rerank_top_n,
            },
        ) as ctx:
            response = await self.blocking(client.rank, query, records, top_n)
            ranked = apply_rank_scores(candidates, response.scores)
            counts = rank_billed_counts(len(records))
            ctx.payload = {
                "service_used": RANKING_SERVICE,
                "model": client.model,
                "candidates_in": len(candidates),
                "candidates_out": min(self.settings.rerank_top_n, len(ranked)),
                "results": [r.summary() for r in ranked],
                "rank_api_ms": response.duration_ms,
                "attempts": response.attempts,
                "records_truncated": sum(1 for r in records if r.content_truncated),
                "billed_counts": counts,
                "cost_usd": rank_cost_usd(counts),
            }
        return ranked

    async def _synthesize(
        self, query: str, chunks: list[DocumentChunk], emitter: StageEmitter, embedding_tokens: int
    ) -> tuple[str, TokenUsage]:
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id or 'GENERAL'} | p.{c.page_number}]: {c.content[:_MAX_CONTEXT_CHUNK_CHARS]}"
            for c in chunks
        )
        return await synthesize_stage(self, self.synthesis_model, query, context_block, len(chunks), emitter, embedding_tokens)

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Execute Level 2 live: prep → embed → hybrid RRF → Ranking API → synthesize, emitting each stage."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("Level2HybridEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        start = time.perf_counter()
        genai_client = get_genai_client(self.settings)
        bq_client = get_bigquery_client(self.settings)

        prepared = await self._query_prep(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(self, prepared.prepared, genai_client, emitter)
        candidates = await self._retrieve(vector, prepared.original, solicitation_filter, bq_client, emitter)
        ranked = await self._rerank(prepared.original, candidates, emitter)
        top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.settings.rerank_top_n]]
        answer, usage = await self._synthesize(cleaned, top, emitter, embedding_tokens)
        rank_cost = rank_cost_usd(rank_billed_counts(len(candidates)))
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer,
            citations=self.chunks_to_citations(top, max_citations=len(top)),
            retrieved_chunks=top,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + rank_cost, 8),
            stage_log=self.stage_log_of(emitter),
            usage=usage,
            flags=EngineFlags(reranker_used=True, adk_runner_used=False, layout_source="pymupdf"),
        )


__all__ = [
    "ACRONYM_EXPANSIONS",
    "HybridCandidate",
    "Level2HybridEngine",
    "PreparedQuery",
    "RerankedCandidate",
    "apply_rank_scores",
    "build_rank_records",
    "build_ranking_client",
    "expand_procurement_acronyms",
    "prepare_query",
    "rank_chunks_live",
    "row_to_candidate",
    "run_hybrid_query_job",
]
