"""Level 8: Vertex AI Vector Search 2.0 (``vertex_vector_v2``) — ScaNN ANN + Sparse Token RRF + Ranking API.

Executes live hybrid search over ``vectorsearch.googleapis.com/v1beta`` ``dataObjects:batchSearch`` combining:
  1. Dense ``gemini-embedding-2`` (3072-d COSINE ScaNN ANN index), and
  2. Deterministic CRC32 sublinear-TF sparse token vectors
via server-side Reciprocal Rank Fusion (``combine.ranker.rrf``), followed by Vertex Ranking API cross-encoder
reranking and catalog-selected LLM synthesis.

Replay is served by ``ReplayRAGEngine`` from recorded ephemeral benchmark runs; live/record execution requires an
active ephemeral collection (``scripts/ephemeral_vvs2.sh --up``) and fails loud if unconfigured or empty (spec P2).
"""

from __future__ import annotations

from dataclasses import replace
import json
import time
from typing import Any

from app.core import gcp_clients
from app.core.config import ArenaSettings
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient, ranking_url, redact_project
from app.core.telemetry import StageEmitter
from app.core.vvs2_client import Vvs2Client, Vvs2Resources, batch_search_body
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level2_hybrid import (
    ACRONYM_EXPANSIONS,
    HybridCandidate,
    PreparedQuery,
    RerankedCandidate,
    apply_rank_scores,
    build_rank_records,
    build_ranking_client,
    prepare_query,
)

LAYOUT_SOURCE = "vertex_vector_v2"
RETRIEVE_SERVICE = "Vertex AI Vector Search 2.0 (REST v1beta · ScaNN + Sparse RRF)"
_EMBEDDING_PREVIEW_DIMS = 4
_MAX_CONTEXT_CHUNK_CHARS = 2400


def _parse_bbox(raw_bbox_json: Any, page_num: int) -> BoundingBox:
    if isinstance(raw_bbox_json, str) and raw_bbox_json.strip():
        try:
            parsed = json.loads(raw_bbox_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed bbox_json in Vector Search 2.0 DataObject: {exc}") from exc
        if isinstance(parsed, dict):
            return BoundingBox(
                page=int(parsed.get("page", page_num)),
                x0=float(parsed.get("x0", 0.0)),
                y0=float(parsed.get("y0", 0.0)),
                x1=float(parsed.get("x1", 1.0)),
                y1=float(parsed.get("y1", 1.0)),
            )
    return BoundingBox(page=page_num, x0=0.0, y0=0.0, x1=1.0, y1=1.0)


def parse_batch_search_candidates(doc: dict[str, Any], *, tier_profile: str = LAYOUT_SOURCE) -> list[HybridCandidate]:
    """Parse ``BatchSearchDataObjectsResponse`` into ``HybridCandidate`` records; fail loud on malformed rows."""
    outer = doc.get("results")
    if not isinstance(outer, list):
        return []
    inner_hits: list[dict[str, Any]] = []
    if outer and isinstance(outer[0], dict) and "results" in outer[0]:
        raw_inner = outer[0].get("results")
        if isinstance(raw_inner, list):
            inner_hits = [item for item in raw_inner if isinstance(item, dict)]
    else:
        inner_hits = [item for item in outer if isinstance(item, dict)]

    candidates: list[HybridCandidate] = []
    for idx, hit in enumerate(inner_hits, start=1):
        data_obj = hit.get("dataObject")
        if not isinstance(data_obj, dict):
            raise ValueError(f"Vector Search 2.0 hit #{idx} missing 'dataObject'")
        data = data_obj.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Vector Search 2.0 hit #{idx} missing 'dataObject.data' (ensure outputFields.dataFields=['*'])")
        chunk_id = str(data.get("chunk_id") or data_obj.get("dataObjectId") or "").strip()
        content = str(data.get("content") or "").strip()
        if not chunk_id or not content:
            raise ValueError(f"Vector Search 2.0 hit #{idx} has empty chunk_id or content")
        page_num = max(1, int(data.get("page_number") or 1))
        meta = hit.get("searchResultMetadata") if isinstance(hit.get("searchResultMetadata"), dict) else {}
        rrf_meta = meta.get("rrfRankerResult") if isinstance(meta.get("rrfRankerResult"), dict) else {}
        rrf_rank = int(rrf_meta.get("rank") or idx)
        raw_score = rrf_meta.get("score") if rrf_meta.get("score") is not None else hit.get("distance")
        score_val = round(float(raw_score or 0.0), 6)
        vec_rank: int | None = None
        vec_dist: float | None = None
        lex_rank: int | None = None
        lex_score: float | None = None
        raw_sds = meta.get("searchDistances")
        if isinstance(raw_sds, list) and raw_sds:
            for sd in raw_sds:
                if not isinstance(sd, dict):
                    continue
                s_idx = int(sd.get("searchIndex", 0) or 0)
                s_rank = int(sd.get("rank") or 0) or None
                s_dist = round(float(sd.get("distance") or 0.0), 6) if sd.get("distance") is not None else None
                if s_idx == 0:
                    vec_rank = s_rank
                    vec_dist = s_dist
                elif s_idx == 1:
                    lex_rank = s_rank
                    lex_score = s_dist
        else:
            vec_rank = idx
            vec_dist = score_val
        effective = str(data.get("effective_date") or "").strip() or None
        amends = str(data.get("amends_section") or "").strip() or None
        section_id = str(data.get("section_id") or "").strip() or None
        chunk = DocumentChunk(
            chunk_id=chunk_id,
            tier_profile=tier_profile,
            solicitation_id=str(data.get("solicitation_id") or ""),
            doc_name=str(data.get("doc_name") or "unknown.pdf"),
            doc_type=str(data.get("doc_type") or "SOLICITATION"),
            element_type=str(data.get("element_type") or "narrative_text"),
            section_id=section_id,
            amendment_number=int(data.get("amendment_number") or 0),
            effective_date=effective,
            amends_section=amends,
            page_number=page_num,
            bbox=_parse_bbox(data.get("bbox_json"), page_num),
            content=content,
            score=score_val,
            layout_source=LAYOUT_SOURCE,
            bbox_source="pdf_text_layer",
        )
        candidates.append(
            HybridCandidate(
                chunk=chunk,
                rrf_rank=rrf_rank,
                rrf_score=score_val,
                vector_rank=vec_rank,
                lexical_rank=lex_rank,
                vector_distance=vec_dist,
                lexical_score=lex_score,
            )
        )
    return candidates


def _redacted_search_request(
    res: Vvs2Resources,
    query_vector: list[float],
    query_text: str,
    top_k: int,
    solicitation_filter: str | None,
) -> dict[str, Any]:
    raw = batch_search_body(
        query_vector=query_vector[:_EMBEDDING_PREVIEW_DIMS],
        query_text=query_text,
        top_k=top_k,
        solicitation_filter=solicitation_filter,
        index_name=res.redacted_index_name,
    )
    raw["collection"] = res.redacted_collection_name
    raw["query_vector_dimensions"] = len(query_vector)
    if "searches" in raw and len(raw["searches"]) > 1 and "sparseVector" in raw["searches"][1].get("vectorSearch", {}):
        sp = raw["searches"][1]["vectorSearch"]["sparseVector"]
        raw["searches"][1]["vectorSearch"]["sparseVector"] = {
            "non_zero_dimensions": len(sp.get("indices", [])),
            "indices_preview": sp.get("indices", [])[:8],
            "values_preview": sp.get("values", [])[:8],
        }
    return raw


class VertexVectorSearchV2Engine(BaseRAGEngine):
    """Level 8: Ephemeral High-QPS Vertex AI Vector Search 2.0 (ScaNN ANN + Sparse RRF + Ranking API)."""

    engine_id: str = "vertex_vector_v2"
    engine_label: str = "Level 8: Vertex AI Vector Search 2.0 (ScaNN + Sparse RRF)"
    tier_profile: str = LAYOUT_SOURCE
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
        vvs2_client: Any | None = None,
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
        self._vvs2_client = vvs2_client

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

    async def _retrieve_candidates(
        self,
        vector: list[float],
        original_query: str,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        *,
        candidate_k: int | None = None,
    ) -> list[HybridCandidate]:
        res = Vvs2Resources.from_settings(self.settings)
        eff_k = int(candidate_k or self.settings.vvs2_top_k)
        client = self._vvs2_client or Vvs2Client(self.settings)
        req_body = batch_search_body(
            query_vector=vector,
            query_text=original_query,
            top_k=eff_k,
            solicitation_filter=solicitation_filter,
            index_name=res.index_name,
        )
        shown_req = _redacted_search_request(res, vector, original_query, eff_k, solicitation_filter)
        async with self.stage(
            emitter,
            "retrieve",
            label="Hybrid ScaNN ANN + sparse RRF (dataObjects:batchSearch)",
            service=RETRIEVE_SERVICE,
            request=shown_req,
        ) as ctx:
            t0 = time.perf_counter()
            doc, attempts = await self.blocking(
                client.request,
                "POST",
                f"{res.collection_name}/dataObjects:batchSearch",
                json_body=req_body,
            )
            api_ms = max(1, int((time.perf_counter() - t0) * 1000))
            candidates = parse_batch_search_candidates(doc, tier_profile=self.tier_profile)
            if not candidates:
                raise RuntimeError(
                    f"Vector Search 2.0 collection '{res.collection_id}' returned 0 candidates — "
                    "run scripts/ephemeral_vvs2.sh --up to load the benchmark corpus or switch to DEMO_MODE=replay"
                )
            ctx.payload = {
                "collection": res.redacted_collection_name,
                "index": res.redacted_index_name,
                "location": res.location,
                "fusion": "RRF(weights=[1.0, 1.0])",
                "candidate_k": eff_k,
                "rows_returned": len(candidates),
                "api_ms": api_ms,
                "attempts": attempts,
                "candidates": [c.summary() for c in candidates],
            }
        return candidates

    async def _rerank(self, query: str, candidates: list[HybridCandidate], emitter: StageEmitter, *, top_n: int | None = None) -> list[RerankedCandidate]:
        eff_top_n = min(int(top_n or self.settings.rerank_top_n), len(candidates))
        client = self._ranking_client or build_ranking_client(self.settings)
        records = build_rank_records(candidates)
        endpoint = redact_project(ranking_url(self.settings.gcp_project_id), self.settings.gcp_project_id)
        async with self.stage(
            emitter,
            "rerank",
            label="Cross-encoder reranking",
            service=f"Discovery Engine Ranking API · {self.settings.reranker_model}",
            request={
                "endpoint": endpoint,
                "model": self.settings.reranker_model,
                "top_n": eff_top_n,
                "records_sent": len(records),
                "query": query,
            },
        ) as ctx:
            response = await self.blocking(client.rank, query, records, len(records))
            ranked = apply_rank_scores(candidates, response.scores)
            counts = rank_billed_counts(len(records))
            ctx.payload = {
                "model": self.settings.reranker_model,
                "candidates_in": len(candidates),
                "candidates_out": eff_top_n,
                "results": [r.summary() for r in ranked],
                "rank_api_ms": response.duration_ms,
                "attempts": response.attempts,
                "records_truncated": sum(1 for r in records if r.content_truncated),
                "billed_counts": counts,
                "cost_usd": rank_cost_usd(counts),
            }
        return ranked

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Execute Level 8 live: query_prep -> embed_query -> VVS2 batchSearch RRF -> Ranking API -> synthesize."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "VertexVectorSearchV2Engine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and (solicitation_filter.startswith("upl_") or solicitation_filter.startswith("upload_")):
            raise ValueError(
                "Upload workspaces (upl_*) are not indexed in ephemeral Vector Search 2.0 benchmark collections; "
                "select the procurement benchmark corpus or run Levels 1–6 on uploads"
            )
        if not self.settings.vvs2_collection.strip():
            raise RuntimeError(
                "VVS2_COLLECTION is not configured — run scripts/ephemeral_vvs2.sh --up or use DEMO_MODE=replay"
            )

        start = time.perf_counter()
        genai_client = gcp_clients.get_genai_client(self.settings)
        prepared = await self._query_prep(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(self, prepared.prepared, genai_client, emitter)
        candidates = await self._retrieve_candidates(vector, prepared.original, solicitation_filter, emitter)
        ranked = await self._rerank(prepared.original, candidates, emitter)
        top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.settings.rerank_top_n]]
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id or 'GENERAL'} | p.{c.page_number}]: {c.content[:_MAX_CONTEXT_CHUNK_CHARS]}"
            for c in top
        )
        answer, usage = await synthesize_stage(
            self,
            self.synthesis_model,
            cleaned,
            context_block,
            len(top),
            emitter,
            embedding_tokens,
            extra_payload={
                "context_source": "Vertex AI Vector Search 2.0 (ScaNN + Sparse RRF)",
                "context_chunks": len(top),
            },
        )
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
            flags=EngineFlags(reranker_used=True, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
        )


__all__ = [
    "LAYOUT_SOURCE",
    "RETRIEVE_SERVICE",
    "VertexVectorSearchV2Engine",
    "parse_batch_search_candidates",
]
