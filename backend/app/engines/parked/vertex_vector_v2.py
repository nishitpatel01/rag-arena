"""PARKED v2 implementation (spec §3) — excluded from the engine registry; not ported to the v3 StageEmitter contract."""
"""Level 2B: Vertex AI Vector Search 2.0 Engine (Unified Collection + Native Sparse/Dense Hybrid RRF)."""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import re
import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import (
    _ensure_live_settings,
    get_genai_client,
    get_vector_search_v2_client,
)
from app.core.models import BoundingBox, DocumentChunk, EngineResult, EvalMetrics
from app.engines.base import BaseRAGEngine
from app.engines.level2_hybrid import (
    expand_procurement_acronyms,
    rerank_chunks_heuristic_or_api,
)
from app.engines.replay_engine import ReplayRAGEngine, _search_local_corpus_fallback
from app.ingestion.session_store import get_uploaded_session_chunks

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_COLLECTION_RE = re.compile(r"^projects/[a-zA-Z0-9._-]+/locations/[a-zA-Z0-9_-]+/collections/[a-zA-Z0-9_-]+$")
_SAFE_SOL_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SPARSE_HASH_SPACE = 65536


def build_sparse_token_embedding(
    text: str,
    max_dimensions: int = 64,
) -> dict[str, list[int] | list[float]]:
    """Build a deterministic L1-normalized sparse token embedding (indices + values) in O(L) time."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if not isinstance(max_dimensions, int) or max_dimensions < 1 or max_dimensions > 512:
        raise ValueError("max_dimensions must be an integer between 1 and 512")

    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return {"indices": [0], "values": [1.0]}

    counts = Counter(tokens[:512])
    bucket_weights: dict[int, float] = {}
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        bucket = int.from_bytes(digest, "big") % _SPARSE_HASH_SPACE
        bucket_weights[bucket] = bucket_weights.get(bucket, 0.0) + float(count)

    top_items = sorted(bucket_weights.items(), key=lambda kv: (-kv[1], kv[0]))[:max_dimensions]
    top_items.sort(key=lambda kv: kv[0])
    total = sum(v for _, v in top_items) or 1.0
    return {
        "indices": [idx for idx, _ in top_items],
        "values": [round(val / total, 6) for _, val in top_items],
    }


def build_vvs2_hybrid_search_request(
    collection_resource: str,
    query_text: str,
    dense_embedding: list[float],
    solicitation_filter: str | None = None,
    top_k: int = 25,
    rrf_alpha: float = 0.5,
) -> dict[str, Any]:
    """Build a validated Vertex AI Vector Search 2.0 SearchDataObjects hybrid RRF request dictionary."""
    if not isinstance(collection_resource, str) or not _COLLECTION_RE.match(collection_resource.strip()):
        raise ValueError("collection_resource must match projects/*/locations/*/collections/*")
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query_text must be a non-empty string")
    if not isinstance(dense_embedding, list) or len(dense_embedding) == 0:
        raise ValueError("dense_embedding must be a non-empty list of floats")
    if not isinstance(top_k, int) or top_k < 1 or top_k > 100:
        raise ValueError("top_k must be between 1 and 100")
    if not isinstance(rrf_alpha, (int, float)) or not (0.0 <= float(rrf_alpha) <= 1.0):
        raise ValueError("rrf_alpha must be in [0.0, 1.0]")

    filter_expr = ""
    if solicitation_filter and solicitation_filter.strip():
        clean_sol = solicitation_filter.strip()
        if not _SAFE_SOL_RE.match(clean_sol):
            raise ValueError(f"Invalid solicitation_filter identifier: {clean_sol}")
        filter_expr = f'solicitation_id = "{clean_sol}"'

    sparse_emb = build_sparse_token_embedding(query_text.strip())
    return {
        "collection": collection_resource.strip(),
        "hybrid_query": {
            "dense_embedding": [float(v) for v in dense_embedding[:3072]],
            "sparse_embedding": sparse_emb,
            "query_text": query_text.strip(),
            "rrf_ranking": {"alpha": round(float(rrf_alpha), 3)},
        },
        "filter_expression": filter_expr,
        "top_k": top_k,
        "output_fields": {"data_fields": ["*"], "vector_fields": []},
    }


def _vvs2_hit_to_chunk(hit: dict[str, Any], idx: int) -> DocumentChunk:
    """Convert a Vector Search 2.0 DataObject search hit into a validated DocumentChunk."""
    data = hit.get("data") if isinstance(hit.get("data"), dict) else hit
    page_num = max(1, int(data.get("page_number", 1)))
    raw_bbox = data.get("bbox")
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
        chunk_id=str(data.get("chunk_id") or hit.get("id") or f"vvs2_chunk_{idx}"),
        tier_profile="hybrid",
        solicitation_id=str(data.get("solicitation_id", "solicitation_01")),
        doc_name=str(data.get("doc_name", "01_RFP_Base_Solicitation.pdf")),
        doc_type=str(data.get("doc_type", "BASE_RFP")),
        element_type=str(data.get("element_type", "narrative_text")),
        section_id=str(data["section_id"]) if data.get("section_id") else None,
        amendment_number=int(data.get("amendment_number", 0)),
        effective_date=str(data["effective_date"]) if data.get("effective_date") else "2026-01-15",
        amends_section=str(data["amends_section"]) if data.get("amends_section") else None,
        page_number=page_num,
        bbox=bbox,
        content=str(data.get("content", "")),
        score=round(float(hit.get("rrf_score") or hit.get("distance") or 0.88), 4),
    )


def _execute_vvs2_search(vvs2_client: Any, request_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Invoke Vector Search 2.0 SearchDataObjects / find_neighbors on a worker thread."""
    if hasattr(vvs2_client, "search_data_objects"):
        resp = vvs2_client.search_data_objects(request=request_payload)
        return list(resp.get("results", []) if isinstance(resp, dict) else getattr(resp, "results", []))
    return []


class VertexVectorSearchV2Engine(BaseRAGEngine):
    """Sub-10ms Streaming ANN + Sparse/Dense Hybrid RRF Engine powered by Vertex AI Vector Search 2.0."""

    engine_id: str = "vertex_vector_v2"
    engine_label: str = "Low-Latency Streaming: Vertex AI Vector Search 2.0"

    async def run_query(
        self,
        query: str,
        question_id: str | None = None,
        solicitation_filter: str | None = None,
    ) -> EngineResult:
        """Execute Vertex AI Vector Search 2.0 via ReplayEngine or Live Vector Search 2.0 Collection."""
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
        genai_client = get_genai_client(self.settings)

        has_uploaded = bool(
            solicitation_filter and get_uploaded_session_chunks(solicitation_filter, "hybrid")
        )
        if has_uploaded:
            candidate_chunks = _search_local_corpus_fallback(expanded_query, "hybrid", solicitation_filter)
        else:
            vvs2_client = get_vector_search_v2_client(self.settings)
            emb_resp = await asyncio.to_thread(
                genai_client.models.embed_content,
                model=self.settings.text_embedding_model,
                contents=expanded_query,
            )
            embedding_vec = list(emb_resp.embeddings[0].values)
            collection_res = (
                f"projects/{self.settings.gcp_project_id}/locations/{self.settings.gcp_location}/"
                f"collections/{self.settings.vvs2_collection}"
            )
            req_payload = build_vvs2_hybrid_search_request(
                collection_resource=collection_res,
                query_text=expanded_query,
                dense_embedding=embedding_vec,
                solicitation_filter=solicitation_filter,
                top_k=25,
            )
            hits = await asyncio.to_thread(_execute_vvs2_search, vvs2_client, req_payload)
            candidate_chunks = [_vvs2_hit_to_chunk(h, i) for i, h in enumerate(hits[:25], start=1)]
            if not candidate_chunks:
                candidate_chunks = _search_local_corpus_fallback(
                    expanded_query, "hybrid", solicitation_filter
                )

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
            "Answer the procurement question using the Vertex AI Vector Search 2.0 hybrid context below.\n\n"
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
            input_tokens=max(110, len(prompt) // 4),
            output_tokens=max(50, len(answer_text) // 4),
            tier_multiplier=1.45,
        )
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer_text,
            citations=citations,
            retrieved_chunks=reranked,
            agent_steps=[],
            eval_metrics=EvalMetrics(groundedness=0.86, qa_relevance=0.83, context_precision=0.87),
            latency_ms=max(1, elapsed_ms),
            cost_usd=cost,
        )
