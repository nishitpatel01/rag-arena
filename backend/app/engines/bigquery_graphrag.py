"""Level 9: BigQuery GraphRAG (Property Graph · ISO GQL, ``bigquery_graphrag``) — $0/mo standing live.

Combines Level 8 Multimodal Hybrid RRF retrieval with a real BigQuery Property Graph ISO GQL
``GRAPH_TABLE`` traversal stage (``graph_expand``):

  1. ``query_prep`` — acronym expansion + table/diagram/amendment element intent.
  2. ``embed_query`` — ``MULTIMODAL_EMBEDDING_MODEL`` 3072-d query vector.
  3. ``retrieve`` — BigQuery multimodal RRF (``VECTOR_SEARCH`` + ``SEARCH()``) with ``@caller_groups`` RLS.
  4. ``graph_expand`` — Parameterized BigQuery ISO GQL ``GRAPH_TABLE`` over ``procurement_graph`` traversing
     ``ContractDoc -[:SUPERSEDES_DOC]->{1,3} ContractDoc`` and ``ContractDoc -[:AMENDS_SECTION]-> ContractSection``
     seeded by retrieved documents/sections, merging newly discovered superseding amendment chunks (ACL-filtered).
  5. ``rerank`` — Discovery Engine Ranking API over hybrid + graph-expanded candidates.
  6. ``synthesize`` — Authority-headed synthesis (latest superseding amendments first).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time
from typing import Any

from app.core.acl import CallerAccessContext, acl_refusal_answer, acl_stage_metadata
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.core.models import DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient
from app.core.telemetry import StageEmitter
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level1_naive import _row_bbox
from app.engines.level2_hybrid import HybridCandidate, RerankedCandidate
from app.engines.level3_multimodal import (
    L3_INSTRUCTION,
    L3PreparedQuery,
    Level3MultimodalEngine,
    MultimodalSearch,
    build_authority_context,
    candidate_summary,
    dedupe_heading_stubs,
    prepare_l3_query,
)
from app.ingestion.bq_graph import DEFAULT_GRAPH_NAME, build_graph_expand_gql

LAYOUT_SOURCE = "bigquery_graphrag"
GRAPH_SERVICE = "BigQuery Property Graph · ISO GQL (GRAPH_TABLE)"
_SECTION_TOKEN_RE = re.compile(r"\b(?:CLIN|SEC|SLA|DATA-SLA|AMD|ZTA|CTRL)-[A-Za-z0-9.\-]+\b", re.IGNORECASE)
_MAX_SEED_DOCS = 24
_MAX_SEED_SECTIONS = 24
_MAX_GRAPH_CHUNKS = 12


@dataclass(frozen=True)
class GraphHopEdge:
    """One traversed BigQuery Property Graph edge recorded in ``graph_expand`` telemetry."""

    edge_type: str
    source_doc: str
    target_label: str
    hop_count: int
    amendment_number: int
    effective_date: str | None
    chunk_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_type": self.edge_type,
            "source_doc": self.source_doc,
            "target_label": self.target_label,
            "hop_count": self.hop_count,
            "amendment_number": self.amendment_number,
            "effective_date": self.effective_date,
            "chunk_id": self.chunk_id,
        }


def extract_graph_seeds(
    candidates: list[HybridCandidate],
    query: str,
) -> tuple[list[str], list[str]]:
    """Extract deterministic bounded ``(seed_doc_keys, seed_section_keys)`` from hybrid candidates and query clauses."""
    doc_keys: list[str] = []
    seen_docs: set[str] = set()
    section_keys: list[str] = []
    seen_secs: set[str] = set()
    solicitations: list[str] = []
    seen_sols: set[str] = set()

    for cand in candidates:
        ch = cand.chunk
        sol = (ch.solicitation_id or "").strip()
        if not sol or sol.startswith("upl_"):
            continue
        if sol not in seen_sols:
            seen_sols.add(sol)
            solicitations.append(sol)
        dkey = f"{sol}::{ch.doc_name}"
        if dkey not in seen_docs and len(doc_keys) < _MAX_SEED_DOCS:
            seen_docs.add(dkey)
            doc_keys.append(dkey)
        if ch.section_id and ch.section_id.strip():
            skey = f"{sol}::{ch.section_id.strip().upper()}"
            if skey not in seen_secs and len(section_keys) < _MAX_SEED_SECTIONS:
                seen_secs.add(skey)
                section_keys.append(skey)

    query_sections = [m.group(0).upper() for m in _SECTION_TOKEN_RE.finditer(query or "")]
    for sol in solicitations[:4]:
        for sec in query_sections:
            skey = f"{sol}::{sec}"
            if skey not in seen_secs and len(section_keys) < _MAX_SEED_SECTIONS:
                seen_secs.add(skey)
                section_keys.append(skey)
    return doc_keys, section_keys


def run_graph_expand_job(
    bq_client: Any,
    sql: str,
    seed_doc_keys: list[str],
    seed_section_keys: list[str],
    timeout_seconds: int,
    *,
    caller_groups: tuple[str, ...] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Execute the parameterized BigQuery Property Graph ISO GQL ``GRAPH_TABLE`` query in a worker thread."""
    from google.cloud import bigquery

    params: list[Any] = [
        bigquery.ArrayQueryParameter("seed_doc_keys", "STRING", list(seed_doc_keys)),
        bigquery.ArrayQueryParameter("seed_section_keys", "STRING", list(seed_section_keys)),
    ]
    if caller_groups:
        params.append(bigquery.ArrayQueryParameter("caller_groups", "STRING", list(caller_groups)))
    job = bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = list(job.result(timeout=timeout_seconds))
    stats = {
        "job_id": str(getattr(job, "job_id", "") or ""),
        "bytes_processed": int(getattr(job, "total_bytes_processed", 0) or 0),
        "slot_millis": int(getattr(job, "slot_millis", 0) or 0),
        "cache_hit": bool(getattr(job, "cache_hit", False)),
    }
    return rows, stats


def row_to_graph_candidate(row: Any, rank: int) -> tuple[HybridCandidate, GraphHopEdge]:
    """Convert one GQL ``GRAPH_TABLE`` joined chunk row into a ``HybridCandidate`` + ``GraphHopEdge``."""
    page = max(1, int(getattr(row, "page_number", 1) or 1))
    eff = getattr(row, "effective_date", None)
    amend_num = int(getattr(row, "amendment_number", 0) or 0)
    hop_count = max(1, int(getattr(row, "hop_count", 1) or 1))
    # Prioritize graph-discovered superseding amendments alongside top RRF seeds before cross-encoder reranking.
    graph_rrf = round(0.032 / hop_count, 6)
    chunk = DocumentChunk(
        chunk_id=str(row.chunk_id),
        tier_profile="multimodal",
        solicitation_id=str(row.solicitation_id),
        doc_name=str(row.doc_name),
        doc_type=str(row.doc_type),
        element_type=str(row.element_type),
        section_id=str(row.section_id) if getattr(row, "section_id", None) else None,
        amendment_number=amend_num,
        effective_date=str(eff) if eff else None,
        amends_section=str(row.amends_section) if getattr(row, "amends_section", None) else None,
        page_number=page,
        bbox=_row_bbox(row, page),
        content=str(row.content),
        score=graph_rrf,
        layout_source=str(getattr(row, "layout_source", "") or LAYOUT_SOURCE),
        bbox_source=str(getattr(row, "bbox_source", "") or "page"),
    )
    cand = HybridCandidate(
        chunk=chunk,
        rrf_rank=rank,
        rrf_score=graph_rrf,
        vector_rank=None,
        lexical_rank=None,
        vector_distance=None,
        lexical_score=None,
    )
    edge = GraphHopEdge(
        edge_type=str(getattr(row, "edge_type", "AMENDS_SECTION") or "AMENDS_SECTION"),
        source_doc=chunk.doc_name,
        target_label=str(getattr(row, "target_label", "") or chunk.amends_section or ""),
        hop_count=hop_count,
        amendment_number=amend_num,
        effective_date=chunk.effective_date,
        chunk_id=chunk.chunk_id,
    )
    return cand, edge


def merge_graph_candidates(
    seed_candidates: list[HybridCandidate],
    graph_candidates: list[HybridCandidate],
    *,
    max_total: int = 50,
) -> tuple[list[HybridCandidate], int]:
    """Merge GQL-discovered amendment chunks ahead of lower-ranked seeds, deduplicating by ``chunk_id``."""
    seen: set[str] = {c.chunk.chunk_id for c in seed_candidates}
    added: list[HybridCandidate] = []
    for gc in graph_candidates:
        if gc.chunk.chunk_id not in seen:
            seen.add(gc.chunk.chunk_id)
            added.append(gc)
    if not added:
        return seed_candidates[:max_total], 0
    head_keep = max(0, max_total - len(added))
    merged = list(seed_candidates[:head_keep]) + added
    return merged, len(added)


class BigQueryGraphRAGEngine(Level3MultimodalEngine):
    """Level 9: Standing $0/mo BigQuery Property Graph (ISO GQL) + Multimodal Hybrid RRF + Rerank."""

    engine_id: str = "bigquery_graphrag"
    engine_label: str = "Level 9: BigQuery GraphRAG (Property Graph · GQL)"
    tier_profile: str = "multimodal"
    stages: tuple[str, ...] = (
        "query_prep",
        "embed_query",
        "retrieve",
        "graph_expand",
        "rerank",
        "synthesize",
        "evaluate",
    )

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
    ) -> None:
        super().__init__(settings=settings, synthesis_model=synthesis_model, ranking_client=ranking_client)

    async def _retrieve_seeds(
        self,
        prepared: L3PreparedQuery,
        vector: list[float],
        solicitation_filter: str | None,
        bq_client: Any,
        emitter: StageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
    ) -> list[HybridCandidate]:
        return await self._retrieve(
            vector,
            prepared.base.original,
            solicitation_filter,
            bq_client,
            emitter,
            caller_access=caller_access,
        )

    async def _graph_expand(
        self,
        candidates: list[HybridCandidate],
        query: str,
        solicitation_filter: str | None,
        bq_client: Any,
        emitter: StageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
        max_total: int | None = None,
    ) -> tuple[list[HybridCandidate], list[GraphHopEdge], int]:
        graph_name = (self.settings.bq_graph_name or DEFAULT_GRAPH_NAME).strip()
        if solicitation_filter and (solicitation_filter.startswith("upl_") or solicitation_filter.startswith("upload_")):
            await emitter.skipped(
                "graph_expand",
                reason="Upload workspaces use dynamic multimodal hybrid retrieval (no pre-built corpus property graph)",
            )
            return candidates, [], 0
        seed_docs, seed_secs = extract_graph_seeds(candidates, query)
        if not seed_docs and not seed_secs:
            await emitter.skipped("graph_expand", reason="0 seed document/section keys available for GQL traversal")
            return candidates, [], 0

        restricted = bool(caller_access and caller_access.is_restricted)
        caller_groups = caller_access.effective_groups if restricted and caller_access is not None else None
        gql = build_graph_expand_gql(
            self.settings.gcp_project_id,
            self.settings.bq_dataset,
            self.settings.bq_table,
            graph_name,
            include_acl_filter=restricted,
            limit=_MAX_GRAPH_CHUNKS,
        )
        req_params: dict[str, Any] = {
            "seed_doc_keys": seed_docs,
            "seed_section_keys": seed_secs,
        }
        if caller_groups:
            req_params["caller_groups"] = list(caller_groups)

        async with self.stage(
            emitter,
            "graph_expand",
            label="BigQuery Property Graph expansion (ISO GQL)",
            service=GRAPH_SERVICE,
            request={
                "graph": f"{self.settings.bq_dataset}.{graph_name}",
                "sql": gql,
                "parameters": req_params,
            },
        ) as ctx:
            if caller_groups:
                rows, stats = await self.blocking(
                    run_graph_expand_job,
                    bq_client,
                    gql,
                    seed_docs,
                    seed_secs,
                    self.settings.query_timeout_seconds,
                    caller_groups=caller_groups,
                )
            else:
                rows, stats = await self.blocking(
                    run_graph_expand_job,
                    bq_client,
                    gql,
                    seed_docs,
                    seed_secs,
                    self.settings.query_timeout_seconds,
                )
            parsed = [row_to_graph_candidate(r, idx) for idx, r in enumerate(rows, start=1)]
            graph_cands = [pair[0] for pair in parsed]
            edges = [pair[1] for pair in parsed]
            cap = int(max_total or self.settings.hybrid_candidate_k)
            merged, added_count = merge_graph_candidates(candidates, graph_cands, max_total=cap)
            ctx.payload = {
                "graph_name": f"{self.settings.bq_dataset}.{graph_name}",
                "query_language": "ISO GQL (GRAPH_TABLE)",
                "seed_docs_count": len(seed_docs),
                "seed_sections_count": len(seed_secs),
                "seed_docs": seed_docs[:12],
                "seed_sections": seed_secs[:12],
                "edges_traversed": len(edges),
                "traversed_edges": [e.to_dict() for e in edges],
                "graph_chunks_returned": len(graph_cands),
                "graph_chunks_added": added_count,
                "merged_candidates": len(merged),
                **stats,
                **(
                    {**acl_stage_metadata(self.engine_id, caller_access), "acl_access_denied": len(merged) == 0}
                    if restricted
                    else {}
                ),
            }
        return merged, edges, added_count

    async def _synthesize_graph(
        self,
        query: str,
        top: list[DocumentChunk],
        embedding_tokens: int,
        edges: list[GraphHopEdge],
        added_count: int,
        emitter: StageEmitter,
    ) -> tuple[str, TokenUsage]:
        context_block, headers, mix = build_authority_context(top)
        return await synthesize_stage(
            self,
            self.synthesis_model,
            query,
            context_block,
            len(top),
            emitter,
            embedding_tokens,
            instruction=L3_INSTRUCTION,
            embedding_model=self.settings.multimodal_embedding_model,
            extra_payload={
                "authority_headers": headers,
                "element_mix": mix,
                "context_order": "authority_amendment_first",
                "graph_edges_traversed": len(edges),
                "graph_chunks_added": added_count,
            },
        )

    async def search_graphrag(
        self,
        query: str,
        *,
        solicitation_filter: str | None = None,
        element_types: tuple[str, ...] = (),
        genai_client: Any = None,
        bq_client: Any = None,
        emitter: StageEmitter,
        caller_access: CallerAccessContext | None = None,
        candidate_k: int | None = None,
        top_k: int | None = None,
        rerank: bool = True,
    ) -> MultimodalSearch:
        """Reusable GraphRAG retrieval helper for ComposableRAGEngine & ADK agent search over ``bigquery_graphrag``."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        genai = genai_client or get_genai_client(self.settings)
        bq = bq_client or get_bigquery_client(self.settings)
        prepared = prepare_l3_query(cleaned)
        vector, emb_tokens = await embed_query_stage(
            self, prepared.base.prepared, genai, emitter, model=self.settings.multimodal_embedding_model
        )
        seeds = await self._retrieve_seeds(prepared, vector, solicitation_filter, bq, emitter, caller_access=caller_access)
        merged, _, _ = await self._graph_expand(
            seeds,
            prepared.base.original,
            solicitation_filter,
            bq,
            emitter,
            caller_access=caller_access,
            max_total=candidate_k,
        )
        kept = [c for c in merged if not element_types or c.chunk.element_type in element_types]
        kept, _ = dedupe_heading_stubs(kept)
        eff_top = int(top_k or self.settings.rerank_top_n)
        if rerank and kept:
            ranked = await self._rerank(prepared.base.original, kept, emitter)
            top = tuple(replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[:eff_top])
            billed = rank_billed_counts(len(kept))
        else:
            top = tuple(c.chunk for c in kept[:eff_top])
            billed = 0
        return MultimodalSearch(
            chunks=top,
            candidates=len(merged),
            filtered_candidates=len(kept),
            embedding_tokens=emb_tokens,
            rank_billed_counts=billed,
            stage_records=self.stage_log_of(emitter),
        )

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        """Execute Level 9 live: query_prep -> embed -> BQ multimodal RRF -> GQL graph_expand -> rerank -> synthesize."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "BigQueryGraphRAGEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        start = time.perf_counter()
        genai_client = get_genai_client(self.settings)
        bq_client = get_bigquery_client(self.settings)

        prepared = await self._query_prep_l3(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(
            self, prepared.base.prepared, genai_client, emitter, model=self.settings.multimodal_embedding_model
        )
        seeds = await self._retrieve_seeds(
            prepared, vector, solicitation_filter, bq_client, emitter, caller_access=caller_access
        )
        merged, edges, added_count = await self._graph_expand(
            seeds,
            prepared.base.original,
            solicitation_filter,
            bq_client,
            emitter,
            caller_access=caller_access,
        )
        if not merged and caller_access is not None and caller_access.is_restricted:
            await emitter.skipped("rerank", reason=f"0 authorized chunks after BigQuery RLS + GQL ({caller_access.persona_id})")
            refusal = acl_refusal_answer(caller_access, cleaned)
            async with self.stage(
                emitter,
                "synthesize",
                label="Row-level security refusal (0 authorized chunks)",
                service="ACL Policy (local)",
                request={"query": cleaned, "context_chunks": 0, **acl_stage_metadata(self.engine_id, caller_access)},
            ) as ctx:
                ctx.payload = {
                    "model": self.synthesis_model.id,
                    "answer": refusal,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "acl_access_denied": True,
                    **acl_stage_metadata(self.engine_id, caller_access),
                }
            usage = TokenUsage(
                embedding_tokens=embedding_tokens,
                model=self.synthesis_model.id,
                embedding_model=self.settings.multimodal_embedding_model,
            )
            return EngineResult(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                answer=refusal,
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                cost_usd=round(cost_from_usage(usage, self.synthesis_model.id), 8),
                stage_log=self.stage_log_of(emitter),
                usage=usage,
                flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
            )

        deduped, _ = dedupe_heading_stubs(merged)
        ranked: list[RerankedCandidate] = await self._rerank(prepared.base.original, deduped, emitter)
        top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.settings.rerank_top_n]]
        answer, usage = await self._synthesize_graph(cleaned, top, embedding_tokens, edges, added_count, emitter)
        rank_cost = rank_cost_usd(rank_billed_counts(len(deduped)))
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
    "BigQueryGraphRAGEngine",
    "GRAPH_SERVICE",
    "GraphHopEdge",
    "LAYOUT_SOURCE",
    "extract_graph_seeds",
    "merge_graph_candidates",
    "row_to_graph_candidate",
    "run_graph_expand_job",
]
