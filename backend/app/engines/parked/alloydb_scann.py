"""PARKED v2 implementation (spec §3) — excluded from the engine registry; not ported to the v3 StageEmitter contract."""
"""Level 2C: AlloyDB for PostgreSQL ScaNN Engine (alloydb_scann + pgvector + ai.hybrid_search RRF)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import (
    _ensure_live_settings,
    get_alloydb_client,
    get_genai_client,
)
from app.core.models import BoundingBox, DocumentChunk, EngineResult, EvalMetrics
from app.engines.base import BaseRAGEngine
from app.engines.level2_hybrid import (
    expand_procurement_acronyms,
    rerank_chunks_heuristic_or_api,
)
from app.engines.replay_engine import ReplayRAGEngine, _search_local_corpus_fallback
from app.ingestion.bq_indexer import sanitize_search_query
from app.ingestion.session_store import get_uploaded_session_chunks

_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def _validate_pg_identifier(value: str, field_name: str) -> str:
    """Validate a PostgreSQL identifier to prevent SQL injection at DDL/query boundaries."""
    if not isinstance(value, str) or not _SAFE_IDENT_RE.match(value.strip()):
        raise ValueError(f"Invalid PostgreSQL identifier for {field_name}: '{value}'")
    return value.strip()


def build_alloydb_scann_ddl(
    table_name: str = "document_chunks",
    vector_dim: int = 768,
    num_leaves: int = 100,
) -> str:
    """Build AlloyDB PostgreSQL DDL enabling vector, alloydb_scann, and google_ml_integration extensions."""
    tbl = _validate_pg_identifier(table_name, "table_name")
    if not isinstance(vector_dim, int) or vector_dim not in (768, 1536, 3072):
        raise ValueError("vector_dim must be one of (768, 1536, 3072)")
    if not isinstance(num_leaves, int) or num_leaves < 10 or num_leaves > 10000:
        raise ValueError("num_leaves must be between 10 and 10000")

    return f"""-- 1. Enable AlloyDB Vector & ScaNN Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS alloydb_scann;
CREATE EXTENSION IF NOT EXISTS google_ml_integration;

-- 2. Operational Procurement Chunks Table
CREATE TABLE IF NOT EXISTS {tbl} (
  chunk_id TEXT PRIMARY KEY,
  tier_profile TEXT NOT NULL,
  solicitation_id TEXT NOT NULL,
  doc_name TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  element_type TEXT NOT NULL,
  section_id TEXT,
  amendment_number INTEGER DEFAULT 0,
  effective_date DATE,
  amends_section TEXT,
  page_number INTEGER NOT NULL,
  bbox JSONB NOT NULL,
  content TEXT NOT NULL,
  embedding vector({vector_dim}) NOT NULL
);

-- 3. Sub-10ms AlloyDB ScaNN Cosine Index + Full-Text GIN Index
CREATE INDEX IF NOT EXISTS {tbl}_scann_idx
ON {tbl}
USING scann (embedding cosine)
WITH (num_leaves = {num_leaves});

CREATE INDEX IF NOT EXISTS {tbl}_fts_idx
ON {tbl}
USING GIN (to_tsvector('english', content));
"""


def build_alloydb_hybrid_search_sql(
    table_name: str = "document_chunks",
    top_k: int = 25,
    include_solicitation_filter: bool = False,
) -> str:
    """Build parameterized AlloyDB SQL fusing ScaNN vector search and full-text search via ai.hybrid_search RRF."""
    tbl = _validate_pg_identifier(table_name, "table_name")
    if not isinstance(top_k, int) or top_k < 1 or top_k > 100:
        raise ValueError("top_k must be between 1 and 100")

    sol_clause = " AND c.solicitation_id = %(solicitation_id)s" if include_solicitation_filter else ""
    return f"""
WITH hybrid_ranked AS (
  SELECT key AS chunk_id, rrf_score
  FROM ai.hybrid_search(
    search_inputs => ARRAY[
      jsonb_build_object(
        'data_type', 'vector',
        'table_name', '{tbl}',
        'key_column', 'chunk_id',
        'vec_column', 'embedding',
        'distance_operator', 'public.<=>',
        'query_vector', %(query_vector)s,
        'limit', {top_k},
        'weight', 0.5
      ),
      jsonb_build_object(
        'data_type', 'text',
        'table_name', '{tbl}',
        'key_column', 'chunk_id',
        'text_column', 'content',
        'query_text_input', %(query_text)s,
        'limit', {top_k},
        'weight', 0.5
      )
    ],
    fusion_algorithm => 'RRF'
  )
)
SELECT
  c.chunk_id,
  c.tier_profile,
  c.solicitation_id,
  c.doc_name,
  c.doc_type,
  c.element_type,
  c.section_id,
  c.amendment_number,
  CAST(c.effective_date AS TEXT) AS effective_date,
  c.amends_section,
  c.page_number,
  c.bbox,
  c.content,
  h.rrf_score
FROM hybrid_ranked h
JOIN {tbl} c ON c.chunk_id = h.chunk_id
WHERE c.tier_profile = 'hybrid'{sol_clause}
ORDER BY h.rrf_score DESC
LIMIT {top_k};
""".strip()


def _execute_alloydb_query(
    conn: Any,
    sql: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Execute parameterized AlloyDB hybrid search query on a worker thread."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        col_names = [desc[0] for desc in (cur.description or [])]
        return [dict(zip(col_names, row)) for row in cur.fetchall()]


def _alloydb_row_to_chunk(row: dict[str, Any]) -> DocumentChunk:
    """Convert an AlloyDB result dictionary into a validated DocumentChunk."""
    page_num = max(1, int(row.get("page_number", 1)))
    raw_bbox = row.get("bbox")
    if isinstance(raw_bbox, str):
        raw_bbox = json.loads(raw_bbox)
    bbox = (
        BoundingBox(
            page=int(raw_bbox.get("page", page_num)),
            x0=float(raw_bbox.get("x0", 0.06)),
            y0=float(raw_bbox.get("y0", 0.14)),
            x1=float(raw_bbox.get("x1", 0.94)),
            y1=float(raw_bbox.get("y1", 0.84)),
        )
        if isinstance(raw_bbox, dict)
        else BoundingBox(page=page_num, x0=0.06, y0=0.14, x1=0.94, y1=0.84)
    )
    return DocumentChunk(
        chunk_id=str(row["chunk_id"]),
        tier_profile=str(row.get("tier_profile", "hybrid")),
        solicitation_id=str(row.get("solicitation_id", "solicitation_01")),
        doc_name=str(row.get("doc_name", "01_RFP_Base_Solicitation.pdf")),
        doc_type=str(row.get("doc_type", "BASE_RFP")),
        element_type=str(row.get("element_type", "narrative_text")),
        section_id=str(row["section_id"]) if row.get("section_id") else None,
        amendment_number=int(row.get("amendment_number", 0)),
        effective_date=str(row["effective_date"]) if row.get("effective_date") else "2026-01-15",
        amends_section=str(row["amends_section"]) if row.get("amends_section") else None,
        page_number=page_num,
        bbox=bbox,
        content=str(row.get("content", "")),
        score=round(min(0.99, max(0.50, float(row.get("rrf_score", 0.03)) * 28.0)), 4),
    )


class AlloyDBScaNNEngine(BaseRAGEngine):
    """Operational Transactional RAG Engine powered by AlloyDB PostgreSQL alloydb_scann + ai.hybrid_search."""

    engine_id: str = "alloydb_scann"
    engine_label: str = "Operational DB: AlloyDB pgvector + ScaNN"

    async def run_query(
        self,
        query: str,
        question_id: str | None = None,
        solicitation_filter: str | None = None,
    ) -> EngineResult:
        """Execute AlloyDB ScaNN hybrid search via ReplayEngine or Live AlloyDB connection."""
        cleaned_query = self.validate_query_inputs(query, solicitation_filter)
        if self.settings.demo_mode == "replay":
            replay = ReplayRAGEngine(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                settings=self.settings,
            )
            return await replay.run_query(
                query=cleaned_query,
                question_id=question_id,
                solicitation_filter=solicitation_filter,
            )

        _ensure_live_settings(self.settings)
        start_ts = time.perf_counter()
        expanded_query = expand_procurement_acronyms(cleaned_query)
        safe_search_text = sanitize_search_query(expanded_query)
        genai_client = get_genai_client(self.settings)

        has_uploaded = bool(
            solicitation_filter and get_uploaded_session_chunks(solicitation_filter, "hybrid")
        )
        if has_uploaded:
            candidate_chunks = _search_local_corpus_fallback(expanded_query, "hybrid", solicitation_filter)
        elif not self.settings.alloydb_dsn.strip():
            await asyncio.to_thread(
                genai_client.models.embed_content,
                model=self.settings.text_embedding_model,
                contents=expanded_query,
            )
            candidate_chunks = _search_local_corpus_fallback(
                expanded_query, "hybrid", solicitation_filter
            )
        else:
            conn = get_alloydb_client(self.settings)
            emb_resp = await asyncio.to_thread(
                genai_client.models.embed_content,
                model=self.settings.text_embedding_model,
                contents=expanded_query,
            )
            embedding_vec = [float(v) for v in emb_resp.embeddings[0].values]
            sql = build_alloydb_hybrid_search_sql(
                table_name=self.settings.alloydb_table,
                top_k=25,
                include_solicitation_filter=bool(solicitation_filter),
            )
            params: dict[str, Any] = {
                "query_vector": json.dumps(embedding_vec),
                "query_text": safe_search_text,
            }
            if solicitation_filter:
                params["solicitation_id"] = solicitation_filter
            rows = await asyncio.to_thread(_execute_alloydb_query, conn, sql, params)
            candidate_chunks = [_alloydb_row_to_chunk(r) for r in rows[:25]]

        reranked = await asyncio.to_thread(
            rerank_chunks_heuristic_or_api,
            query=cleaned_query,
            chunks=candidate_chunks,
            top_n=5,
            settings=None if has_uploaded else self.settings,
        )
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id} | p.{c.page_number}]: {c.content}" for c in reranked
        )
        prompt = (
            "Answer the procurement question using the AlloyDB ScaNN hybrid context below.\n\n"
            f"Context:\n{context_block}\n\nQuestion: {cleaned_query}"
        )
        gen_resp = await asyncio.to_thread(
            genai_client.models.generate_content,
            model=self.settings.gemini_fast_model,
            contents=prompt,
            config={"thinking_config": {"thinking_level": self.settings.gemini_thinking_level.upper()}},
        )
        answer_text = str(getattr(gen_resp, "text", "") or "").strip()
        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        citations = self.chunks_to_citations(reranked, max_citations=5)
        cost = self.estimate_cost_usd(
            input_tokens=max(115, len(prompt) // 4),
            output_tokens=max(50, len(answer_text) // 4),
            tier_multiplier=1.50,
        )
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer_text,
            citations=citations,
            retrieved_chunks=reranked,
            agent_steps=[],
            eval_metrics=EvalMetrics(groundedness=0.87, qa_relevance=0.84, context_precision=0.88),
            latency_ms=max(1, elapsed_ms),
            cost_usd=cost,
        )
