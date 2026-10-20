"""Level 2: BigQuery Hybrid Search (VECTOR_SEARCH + SEARCH RRF) + Discovery Engine Ranking API.

v2 implementation — to be rebuilt in Phase 2. The live body runs under a single measured ``legacy`` stage
(see ``app.engines._legacy``); it is disabled by default (``ENABLED_LEVELS``).
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client, get_genai_client, get_rank_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult
from app.core.pricing import cost_from_usage
from app.core.telemetry import StageEmitter
from app.engines._legacy import run_legacy_body, usage_from_generate_response
from app.engines.base import BaseRAGEngine
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, sanitize_search_query

_ACRONYM_MAP: dict[str, str] = {
    "CLIN": "Contract Line Item Number",
    "SLA": "Service Level Agreement",
    "RTO": "Recovery Time Objective",
    "RPO": "Recovery Point Objective",
    "QASP": "Quality Assurance Surveillance Plan",
    "PEP": "Policy Enforcement Point",
    "MSA": "Master Services Agreement",
    "SOW": "Statement of Work",
}

_IDENTIFIER_RE = re.compile(
    r"\b(?:CLIN-\d{4}(?:-[A-Z]{2})?|SEC-\d+\.\d+(?:\.\d+)?|AMD-\d{4}(?:-[A-Z]+)?|Q\d+|A\d+)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def expand_procurement_acronyms(query: str) -> str:
    """Deterministically expand federal/enterprise procurement acronyms for hybrid retrieval."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query) > 4000:
        raise ValueError("query exceeds 4000 character bound")

    expansions: list[str] = []
    upper_query = query.upper()
    for acronym, full_term in _ACRONYM_MAP.items():
        if re.search(rf"\b{re.escape(acronym)}\b", upper_query):
            expansions.append(f"{acronym} ({full_term})")

    if not expansions:
        return query.strip()
    return f"{query.strip()} [Expanded Terms: {'; '.join(expansions)}]"


def _clone_chunk_with_score(chunk: DocumentChunk, new_score: float) -> DocumentChunk:
    """Return an immutable copy of DocumentChunk with an updated relevance score."""
    return DocumentChunk(
        chunk_id=chunk.chunk_id,
        tier_profile=chunk.tier_profile,
        solicitation_id=chunk.solicitation_id,
        doc_name=chunk.doc_name,
        doc_type=chunk.doc_type,
        element_type=chunk.element_type,
        section_id=chunk.section_id,
        amendment_number=chunk.amendment_number,
        effective_date=chunk.effective_date,
        amends_section=chunk.amends_section,
        page_number=chunk.page_number,
        bbox=chunk.bbox,
        content=chunk.content,
        score=round(float(new_score), 4),
    )


def rerank_chunks_heuristic_or_api(
    query: str,
    chunks: list[DocumentChunk],
    top_n: int = 5,
    settings: ArenaSettings | None = None,
) -> list[DocumentChunk]:
    """Rerank with the Discovery Engine Ranking API when ``settings`` is live (errors propagate — spec P2);
    the deterministic lexical+identifier heuristic is used ONLY when ``settings`` is None (offline unit tests)."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(chunks, list):
        raise TypeError("chunks must be a list of DocumentChunk instances")
    if not isinstance(top_n, int) or top_n < 1 or top_n > 50:
        raise ValueError("top_n must be an integer between 1 and 50")
    if not chunks:
        return []

    if settings is not None:
        if not settings.is_live:
            raise RuntimeError(
                "Ranking API rerank requires DEMO_MODE=live|record; pass settings=None for the offline heuristic"
            )
        rank_client = get_rank_client(settings)
        from google.cloud import discoveryengine_v1 as discoveryengine

        ranking_config = (
            f"projects/{settings.gcp_project_id}/locations/global/rankingConfigs/default_ranking_config"
        )
        records = [
            discoveryengine.RankingRecord(
                id=c.chunk_id,
                title=f"{c.doc_name} ({c.section_id or ''})",
                content=c.content[:1500],
            )
            for c in chunks[:50]
        ]
        request = discoveryengine.RankRequest(
            ranking_config=ranking_config,
            model=settings.reranker_model,
            top_n=top_n,
            query=query,
            records=records,
        )
        response = rank_client.rank(
            request=request,
            timeout=min(6.0, float(settings.query_timeout_seconds)),
        )
        by_id = {c.chunk_id: c for c in chunks}
        live_ranked: list[DocumentChunk] = []
        for rec in response.records[:top_n]:
            if rec.id in by_id:
                live_ranked.append(_clone_chunk_with_score(by_id[rec.id], float(rec.score)))
        if not live_ranked:
            raise RuntimeError("Discovery Engine Ranking API returned no records matching the candidate chunk ids")
        return live_ranked

    # Deterministic lexical + exact clause/CLIN identifier cross-encoder heuristic
    exact_ids = {m.upper() for m in _IDENTIFIER_RE.findall(query)}
    q_words = set(_WORD_RE.findall(query.lower()))
    reranked: list[DocumentChunk] = []

    for chunk in chunks[:50]:
        if not isinstance(chunk, DocumentChunk):
            raise TypeError("Every element in chunks must be a DocumentChunk instance")
        content_upper = chunk.content.upper()
        sec_upper = (chunk.section_id or "").upper()
        id_hits = sum(1 for eid in exact_ids if eid in content_upper or eid == sec_upper)
        c_words = set(_WORD_RE.findall(chunk.content.lower()))
        lexical_overlap = len(q_words & c_words) / max(len(q_words), 1)
        table_boost = 0.05 if chunk.element_type == "table" and exact_ids else 0.0
        combined = min(0.99, float(chunk.score) * 0.5 + lexical_overlap * 0.25 + id_hits * 0.25 + table_boost)
        reranked.append(_clone_chunk_with_score(chunk, combined))

    reranked.sort(key=lambda c: c.score, reverse=True)
    return reranked[:top_n]


def _hybrid_row_to_chunk(row: Any) -> DocumentChunk:
    """Convert a BigQuery Hybrid RRF result row into a validated DocumentChunk."""
    bbox_raw = getattr(row, "bbox", None) or {}
    page_num = int(getattr(row, "page_number", 1) or 1)
    bbox = BoundingBox(
        page=int(bbox_raw.get("page", page_num)) if isinstance(bbox_raw, dict) else page_num,
        x0=float(bbox_raw.get("x0", 0.08)) if isinstance(bbox_raw, dict) else 0.08,
        y0=float(bbox_raw.get("y0", 0.20)) if isinstance(bbox_raw, dict) else 0.20,
        x1=float(bbox_raw.get("x1", 0.92)) if isinstance(bbox_raw, dict) else 0.92,
        y1=float(bbox_raw.get("y1", 0.60)) if isinstance(bbox_raw, dict) else 0.60,
    )
    return DocumentChunk(
        chunk_id=str(row.chunk_id),
        tier_profile="hybrid",
        solicitation_id=str(row.solicitation_id),
        doc_name=str(row.doc_name),
        doc_type=str(row.doc_type),
        element_type=str(row.element_type),
        section_id=str(row.section_id) if getattr(row, "section_id", None) else None,
        amendment_number=int(getattr(row, "amendment_number", 0) or 0),
        effective_date=str(getattr(row, "effective_date", "2026-01-15") or "2026-01-15"),
        amends_section=str(row.amends_section) if getattr(row, "amends_section", None) else None,
        page_number=page_num,
        bbox=bbox,
        content=str(row.content),
        score=round(float(getattr(row, "rrf_score", 0.80) or 0.80), 4),
    )


def _query_bigquery_hybrid_rows(
    bq_client: Any,
    sql: str,
    embedding_vec: list[float],
    tier_profile: str,
    safe_search_text: str,
    solicitation_filter: str | None,
    timeout_seconds: int,
) -> list[Any]:
    """Execute synchronous BigQuery Hybrid RRF query inside a worker thread."""
    from google.cloud import bigquery

    params: list[Any] = [
        bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", embedding_vec),
        bigquery.ScalarQueryParameter("tier_profile", "STRING", tier_profile),
        bigquery.ScalarQueryParameter("query_text", "STRING", safe_search_text),
    ]
    if solicitation_filter:
        params.append(
            bigquery.ScalarQueryParameter("solicitation_id", "STRING", solicitation_filter.strip())
        )
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return list(bq_client.query(sql, job_config=job_config).result(timeout=timeout_seconds))


class Level2HybridEngine(BaseRAGEngine):
    """Level 2 Hybrid Search + Reranker Engine: BigQuery VECTOR_SEARCH + SEARCH() RRF + Ranking API."""

    engine_id: str = "level2_hybrid"
    engine_label: str = "Level 2: BigQuery Hybrid + Reranker"
    tier_profile: str = "hybrid"
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(self, settings: ArenaSettings | None = None) -> None:
        super().__init__(settings=settings)

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Run the v2 hybrid body under one measured ``legacy`` stage (live/record only)."""
        cleaned_query = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("Level2HybridEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        return await run_legacy_body(
            self,
            emitter,
            phase="Phase 2",
            query=cleaned_query,
            body=lambda: self._legacy_live(cleaned_query, solicitation_filter),
        )

    async def _legacy_live(self, cleaned_query: str, solicitation_filter: str | None) -> EngineResult:
        start_ts = time.perf_counter()
        expanded_query = expand_procurement_acronyms(cleaned_query)
        genai_client = get_genai_client(self.settings)
        bq_client = get_bigquery_client(self.settings)
        emb_resp = await self.blocking(
            genai_client.models.embed_content,
            model=self.settings.text_embedding_model,
            contents=expanded_query,
        )
        embedding_vec = list(emb_resp.embeddings[0].values)
        sql = build_hybrid_rrf_sql(
            project_id=self.settings.gcp_project_id,
            dataset_id=self.settings.bq_dataset,
            table_id=self.settings.bq_table,
            rrf_k=60,
            top_k=50,
            include_solicitation_filter=bool(solicitation_filter),
        )
        rows = await self.blocking(
            _query_bigquery_hybrid_rows,
            bq_client,
            sql,
            embedding_vec,
            "hybrid",
            sanitize_search_query(cleaned_query),
            solicitation_filter,
            self.settings.query_timeout_seconds,
        )
        candidate_chunks = [_hybrid_row_to_chunk(r) for r in rows[:50]]
        reranked_chunks = await self.blocking(
            rerank_chunks_heuristic_or_api,
            query=cleaned_query,
            chunks=candidate_chunks,
            top_n=5,
            settings=self.settings,
        )
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id} | p.{c.page_number}]: {c.content}" for c in reranked_chunks
        )
        prompt = (
            "You are a procurement analyst. Answer the question citing exact CLINs, clauses, and document names.\n\n"
            f"Reranked Hybrid Context:\n{context_block}\n\nQuestion: {cleaned_query}"
        )
        gen_resp = await self.blocking(
            genai_client.models.generate_content,
            model=self.settings.gemini_fast_model,
            contents=prompt,
            config={"thinking_config": {"thinking_level": self.settings.gemini_thinking_level.upper()}},
        )
        answer_text = str(getattr(gen_resp, "text", "") or "").strip()
        usage = usage_from_generate_response(gen_resp, self.settings.gemini_fast_model, 0, self.settings.text_embedding_model)
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer_text,
            citations=self.chunks_to_citations(reranked_chunks, max_citations=5),
            retrieved_chunks=reranked_chunks,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start_ts) * 1000)),
            cost_usd=cost_from_usage(usage, self.settings.gemini_fast_model),
            usage=usage,
            flags=EngineFlags(reranker_used=bool(reranked_chunks), adk_runner_used=False, layout_source="pymupdf"),
        )
