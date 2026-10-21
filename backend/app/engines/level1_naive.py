"""Level 1: Naive Vector RAG — BigQuery VECTOR_SEARCH (COSINE, top-5) over 360-char windows + gemini-4-argon.

Every stage (``query_prep → embed_query → retrieve → synthesize``) is emitted from inside this engine
at the moment it starts/ends (spec P1). Replay is served by ``ReplayRAGEngine``; this class runs only
in ``live``/``record`` mode and fails loud on any SDK error (spec P2).
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.embeddings import QUERY_TASK_TYPE, embed_config, format_query_for_embedding, single_vector
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.pricing import cost_from_usage
from app.core.telemetry import StageEmitter
from app.engines.base import BaseRAGEngine
from app.ingestion.bq_indexer import _validate_identifier

_TOP_K = 5
_EMBEDDING_PREVIEW_DIMS = 4
_MAX_CONTEXT_CHUNK_CHARS = 2400


def build_vector_search_sql(
    project_id: str,
    dataset_id: str,
    table_id: str = "document_chunks",
    top_k: int = 5,
    include_solicitation_filter: bool = False,
) -> str:
    """Build parameterized BigQuery VECTOR_SEARCH SQL for pre-filtered dense COSINE retrieval on naive chunks."""
    proj = _validate_identifier(project_id, "project_id")
    dataset = _validate_identifier(dataset_id, "dataset_id")
    table = _validate_identifier(table_id, "table_id")
    if not isinstance(top_k, int) or top_k < 1 or top_k > 100:
        raise ValueError("top_k must be an integer between 1 and 100")

    fqn = f"{proj}.{dataset}.{table}"
    sol_predicate = " AND solicitation_id = @solicitation_id" if include_solicitation_filter else ""
    base_subquery = f"(SELECT * FROM `{fqn}` WHERE tier_profile = 'naive'{sol_predicate})"
    return f"""SELECT
  base.chunk_id,
  base.solicitation_id,
  base.doc_name,
  base.doc_type,
  base.element_type,
  base.section_id,
  base.amendment_number,
  base.effective_date,
  base.amends_section,
  base.page_number,
  base.bbox,
  base.content,
  (1.0 - distance) AS similarity_score
FROM VECTOR_SEARCH(
  {base_subquery},
  'embedding',
  (SELECT @query_embedding AS embedding),
  top_k => {top_k},
  distance_type => 'COSINE'
)
ORDER BY distance ASC
LIMIT {top_k};
"""


def _row_bbox(row: Any, page_num: int) -> BoundingBox:
    """Read the STRUCT bbox from a row; a NULL bbox maps to the full page (geometry only, never a score)."""
    bbox_raw = getattr(row, "bbox", None)
    if isinstance(bbox_raw, dict) and bbox_raw:
        return BoundingBox(
            page=int(bbox_raw.get("page", page_num)),
            x0=float(bbox_raw["x0"]),
            y0=float(bbox_raw["y0"]),
            x1=float(bbox_raw["x1"]),
            y1=float(bbox_raw["y1"]),
        )
    return BoundingBox(page=page_num, x0=0.0, y0=0.0, x1=1.0, y1=1.0)


def _row_to_document_chunk(row: Any) -> DocumentChunk:
    """Convert a BigQuery VECTOR_SEARCH row into a validated DocumentChunk (score = measured similarity)."""
    page_num = int(row.page_number)
    effective = getattr(row, "effective_date", None)
    return DocumentChunk(
        chunk_id=str(row.chunk_id),
        tier_profile="naive",
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
        score=round(float(row.similarity_score), 4),
    )


def _run_vector_search_job(
    bq_client: Any,
    sql: str,
    embedding_vec: list[float],
    solicitation_filter: str | None,
    timeout_seconds: int,
) -> tuple[list[Any], dict[str, Any]]:
    """Execute the VECTOR_SEARCH query in a worker thread; return rows and measured job statistics."""
    from google.cloud import bigquery

    params: list[Any] = [bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", embedding_vec)]
    if solicitation_filter:
        params.append(bigquery.ScalarQueryParameter("solicitation_id", "STRING", solicitation_filter.strip()))
    job = bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = list(job.result(timeout=timeout_seconds))
    stats = {
        "job_id": str(getattr(job, "job_id", "") or ""),
        "bytes_processed": int(getattr(job, "total_bytes_processed", 0) or 0),
        "slot_millis": int(getattr(job, "slot_millis", 0) or 0),
        "cache_hit": bool(getattr(job, "cache_hit", False)),
    }
    return rows, stats


def _embedding_token_count(embed_response: Any) -> int | None:
    """Read ``embeddings[0].statistics.token_count`` when the SDK returns it (Vertex AI does)."""
    embeddings = getattr(embed_response, "embeddings", None) or []
    stats = getattr(embeddings[0], "statistics", None) if embeddings else None
    count = getattr(stats, "token_count", None) if stats is not None else None
    return int(count) if isinstance(count, (int, float)) else None


def _usage_from_response(gen_resp: Any, model: str, embedding_tokens: int, embedding_model: str) -> TokenUsage:
    """Build TokenUsage strictly from ``response.usage_metadata`` (fail loud when absent)."""
    meta = getattr(gen_resp, "usage_metadata", None)
    if meta is None:
        raise ValueError("generate_content response carries no usage_metadata; cannot compute measured cost")
    return TokenUsage(
        prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
        thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
        embedding_tokens=embedding_tokens,
        model=model,
        embedding_model=embedding_model,
    )


class Level1NaiveEngine(BaseRAGEngine):
    """Level 1 Naive Vector RAG Engine: 360-char sliding windows + dense COSINE VECTOR_SEARCH."""

    engine_id: str = "level1_naive"
    engine_label: str = "Level 1: Naive Vector RAG"
    tier_profile: str = "naive"
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "synthesize", "evaluate")

    def __init__(self, settings: ArenaSettings | None = None) -> None:
        super().__init__(settings=settings)

    async def _query_prep(self, query: str, emitter: StageEmitter) -> str:
        async with self.stage(
            emitter, "query_prep", label="Query preparation", service="Engine (local)", request={"query": query}
        ) as ctx:
            prepared = " ".join(query.split())
            ctx.payload = {"original_query": query, "prepared_query": prepared, "expansions": []}
        return prepared

    async def _embed_query(self, prepared: str, genai_client: Any, emitter: StageEmitter) -> tuple[list[float], int]:
        """Embed the query in the same gemini-embedding-2 space as the ingested documents (RETRIEVAL_QUERY side)."""
        model = self.settings.text_embedding_model
        contents = format_query_for_embedding(prepared)
        async with self.stage(
            emitter,
            "embed_query",
            label="Query embedding",
            service=f"Vertex AI · {model}",
            request={"model": model, "contents": contents, "task_type": QUERY_TASK_TYPE, "method": "models.embed_content"},
        ) as ctx:
            resp = await self.blocking(
                genai_client.models.embed_content, model=model, contents=contents, config=embed_config(QUERY_TASK_TYPE)
            )
            vector = single_vector(resp)
            token_count = _embedding_token_count(resp)
            ctx.payload = {"model": model, "dimensions": len(vector), "token_count": token_count, "task_type": QUERY_TASK_TYPE}
        return vector, token_count or 0

    async def _retrieve(
        self, vector: list[float], solicitation_filter: str | None, bq_client: Any, emitter: StageEmitter
    ) -> list[DocumentChunk]:
        sql = build_vector_search_sql(
            project_id=self.settings.gcp_project_id,
            dataset_id=self.settings.bq_dataset,
            table_id=self.settings.bq_table,
            top_k=_TOP_K,
            include_solicitation_filter=bool(solicitation_filter),
        )
        params: dict[str, Any] = {
            "query_embedding": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
        }
        if solicitation_filter:
            params["solicitation_id"] = solicitation_filter
        async with self.stage(
            emitter,
            "retrieve",
            label="BigQuery VECTOR_SEARCH",
            service="BigQuery",
            request={"sql": sql, "params": params, "top_k": _TOP_K},
        ) as ctx:
            rows, stats = await self.blocking(
                _run_vector_search_job, bq_client, sql, vector, solicitation_filter, self.settings.query_timeout_seconds
            )
            chunks = [_row_to_document_chunk(r) for r in rows[:_TOP_K]]
            ctx.payload = {
                "sql": sql,
                "params": params,
                "rows_returned": len(rows),
                "top_k": _TOP_K,
                "scores": [c.score for c in chunks],
                "chunk_ids": [c.chunk_id for c in chunks],
                **stats,
            }
        return chunks

    async def _synthesize(
        self, query: str, chunks: list[DocumentChunk], genai_client: Any, emitter: StageEmitter, embedding_tokens: int
    ) -> tuple[str, TokenUsage]:
        model = self.settings.gemini_fast_model
        thinking = self.settings.gemini_thinking_level.upper()
        context_block = "\n\n".join(
            f"[{c.doc_name} p.{c.page_number}]: {c.content[:_MAX_CONTEXT_CHUNK_CHARS]}" for c in chunks
        )
        prompt = (
            "Answer the procurement question using ONLY the retrieved context chunks below.\n\n"
            f"Context:\n{context_block}\n\nQuestion: {query}"
        )
        async with self.stage(
            emitter,
            "synthesize",
            label="Answer synthesis",
            service=f"Vertex AI · {model}",
            request={"model": model, "thinking_level": thinking, "prompt": prompt, "context_chunks": len(chunks)},
        ) as ctx:
            resp = await self.blocking(
                genai_client.models.generate_content,
                model=model,
                contents=prompt,
                config={"thinking_config": {"thinking_level": thinking}},
            )
            answer = str(getattr(resp, "text", "") or "").strip()
            if not answer:
                raise ValueError("generate_content returned an empty answer")
            usage = _usage_from_response(resp, model, embedding_tokens, self.settings.text_embedding_model)
            ctx.payload = {
                "model": model,
                "thinking_level": thinking,
                "prompt_tokens": usage.prompt_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
                "answer": answer,
            }
        return answer, usage

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Execute Level 1 live: prep → embed → VECTOR_SEARCH → synthesize, emitting each stage as it runs."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "Level1NaiveEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        start = time.perf_counter()
        genai_client = get_genai_client(self.settings)
        bq_client = get_bigquery_client(self.settings)

        prepared = await self._query_prep(cleaned, emitter)
        vector, embedding_tokens = await self._embed_query(prepared, genai_client, emitter)
        chunks = await self._retrieve(vector, solicitation_filter, bq_client, emitter)
        answer, usage = await self._synthesize(cleaned, chunks, genai_client, emitter, embedding_tokens)

        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer,
            citations=self.chunks_to_citations(chunks, max_citations=_TOP_K),
            retrieved_chunks=chunks,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=cost_from_usage(usage, self.settings.gemini_fast_model),
            stage_log=self.stage_log_of(emitter),
            usage=usage,
            flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source="pymupdf"),
        )
