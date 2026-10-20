"""Level 3: Layout-Aware Multimodal RAG (Document AI Layout Parser + Gemini vision + gemini-embedding-2).

v2 implementation — to be rebuilt in Phase 3. The live body runs under a single measured ``legacy`` stage
(see ``app.engines._legacy``); it is disabled by default (``ENABLED_LEVELS``). The v2 body never called
Document AI or Gemini vision (audit §A), so ``flags.layout_source`` is honestly reported as ``pymupdf``.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult
from app.core.pricing import cost_from_usage
from app.core.telemetry import StageEmitter
from app.engines._legacy import run_legacy_body, usage_from_generate_response
from app.engines.base import BaseRAGEngine
from app.engines.level2_hybrid import (
    _query_bigquery_hybrid_rows,
    expand_procurement_acronyms,
    rerank_chunks_heuristic_or_api,
)
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, sanitize_search_query


def format_chunk_with_authority_header(chunk: DocumentChunk) -> str:
    """Format a DocumentChunk with explicit structural element and amendment supersession headers."""
    if not isinstance(chunk, DocumentChunk):
        raise TypeError("chunk must be a DocumentChunk instance")
    if not chunk.content.strip():
        raise ValueError("chunk.content must be a non-empty string")

    elem = chunk.element_type.upper()
    sec = chunk.section_id or "GENERAL"
    element_header = f"[ELEMENT: {elem} | DOC: {chunk.doc_name} | SEC: {sec}]"

    if chunk.amendment_number > 0:
        target = chunk.amends_section or "BASE_SOLICITATION"
        amendment_header = f"[AMENDMENT {chunk.amendment_number:04d} - SUPERSEDES {target}]"
        return f"{amendment_header} {element_header}\n{chunk.content}"

    return f"{element_header}\n{chunk.content}"


def _multimodal_row_to_chunk(row: Any) -> DocumentChunk:
    """Convert a BigQuery result row into a multimodal DocumentChunk."""
    bbox_raw = getattr(row, "bbox", None) or {}
    page_num = int(getattr(row, "page_number", 1) or 1)
    bbox = BoundingBox(
        page=int(bbox_raw.get("page", page_num)) if isinstance(bbox_raw, dict) else page_num,
        x0=float(bbox_raw.get("x0", 0.06)) if isinstance(bbox_raw, dict) else 0.06,
        y0=float(bbox_raw.get("y0", 0.14)) if isinstance(bbox_raw, dict) else 0.14,
        x1=float(bbox_raw.get("x1", 0.94)) if isinstance(bbox_raw, dict) else 0.94,
        y1=float(bbox_raw.get("y1", 0.86)) if isinstance(bbox_raw, dict) else 0.86,
    )
    return DocumentChunk(
        chunk_id=str(row.chunk_id),
        tier_profile="multimodal",
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
        score=round(float(getattr(row, "rrf_score", 0.90) or 0.90), 4),
    )


class Level3MultimodalEngine(BaseRAGEngine):
    """Level 3 Layout-Aware Multimodal RAG Engine (v2 body; rebuilt in Phase 3)."""

    engine_id: str = "level3_multimodal"
    engine_label: str = "Level 3: Layout-Aware Multimodal RAG"
    tier_profile: str = "multimodal"
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
        """Run the v2 multimodal body under one measured ``legacy`` stage (live/record only)."""
        cleaned_query = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "Level3MultimodalEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        return await run_legacy_body(
            self,
            emitter,
            phase="Phase 3",
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
            model=self.settings.multimodal_embedding_model,
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
            embedding_column="embedding_multimodal",
        )
        rows = await self.blocking(
            _query_bigquery_hybrid_rows,
            bq_client,
            sql,
            embedding_vec,
            "multimodal",
            sanitize_search_query(cleaned_query),
            solicitation_filter,
            self.settings.query_timeout_seconds,
        )
        candidates = [_multimodal_row_to_chunk(r) for r in rows[:50]]
        reranked = await self.blocking(
            rerank_chunks_heuristic_or_api,
            query=cleaned_query,
            chunks=candidates,
            top_n=5,
            settings=self.settings,
        )
        context_block = "\n\n".join(format_chunk_with_authority_header(c) for c in reranked)
        prompt = (
            "You are a senior contracts analyst. Use the structured context below. Amendments supersede base "
            "clauses. Tables are Markdown; diagrams are described as component topology.\n\n"
            f"Context:\n{context_block}\n\nQuestion: {cleaned_query}"
        )
        gen_resp = await self.blocking(
            genai_client.models.generate_content,
            model=self.settings.gemini_pro_model,
            contents=prompt,
            config={"thinking_config": {"thinking_level": self.settings.gemini_thinking_level.upper()}},
        )
        answer_text = str(getattr(gen_resp, "text", "") or "").strip()
        usage = usage_from_generate_response(
            gen_resp, self.settings.gemini_pro_model, 0, self.settings.multimodal_embedding_model
        )
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer_text,
            citations=self.chunks_to_citations(reranked, max_citations=5),
            retrieved_chunks=reranked,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start_ts) * 1000)),
            cost_usd=cost_from_usage(usage, self.settings.gemini_pro_model),
            usage=usage,
            flags=EngineFlags(reranker_used=bool(reranked), adk_runner_used=False, layout_source="pymupdf"),
        )
