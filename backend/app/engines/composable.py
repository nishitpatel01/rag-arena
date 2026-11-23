"""Composable RAG pipeline executor (Release 1.3.0).

Executes any validated ``PipelineSpec`` (``IndexProfile`` + ``QuerySlots``) using the same measured stage primitives
as Levels 1–6 (``StageEmitter``, zero local fallbacks, exact published Vertex/Discovery Engine pricing). In
``DEMO_MODE=replay``, presets delegate to ``ReplayRAGEngine`` while custom unrecorded recipes fail loud with
``ReplayUnavailableError``.
"""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from app.core import gcp_clients
from app.core.acl import (
    AclEnforcementUnsupportedError,
    CallerAccessContext,
    acl_refusal_answer,
    acl_stage_metadata,
)
from app.core.config import ArenaSettings
from app.core.model_armor import (
    MAX_SANITIZE_TEXT_CHARS,
    MODEL_ARMOR_SERVICE_LABEL,
    ModelArmorClient,
    build_model_armor_client,
    ensure_model_armor_configured,
    model_armor_cost_usd,
    model_armor_pricing_summary,
)
from app.core.models import AgentStep, Citation, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import (
    PROVIDER_VERTEX_AI_SEARCH,
    PROVIDER_VERTEX_RAG_ENGINE,
    ModelSpec,
    RunModels,
    spec_for_settings_model,
)
from app.core.pipeline_spec import (
    BQ_INDEX_PROFILES,
    SCANN_INDEX_PROFILES,
    PipelineSpec,
    active_query_stages,
    validate_pipeline_models,
)
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd, vais_query_cost_usd
from app.core.rag_engine_client import VertexRagClient
from app.core.ranking import RankingClient
from app.core.telemetry import BaseStageEmitter, StageEmitter
from app.core.vertex_search import DiscoveryEngineClient
from app.engines.agent_ledger import AgentLedger
from app.engines.agent_runtime import (
    MODEL_CALL_ATTEMPTS,
    AgentLimits,
    AgentOutcome,
    agent_instruction_for_tools,
    build_agent,
    ensure_agent_model,
    run_agent,
)
from app.engines.agent_tools import ToolDeps, build_tools
from app.engines.alloydb_scann import LAYOUT_SOURCE as ALLOYDB_LAYOUT_SOURCE, AlloyDBScaNNEngine
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level1_naive import _row_bbox, _run_vector_search_job, build_vector_search_sql
from app.engines.level2_hybrid import (
    ACRONYM_EXPANSIONS,
    RANKING_SERVICE,
    RRF_K,
    HybridCandidate,
    PreparedQuery,
    RerankedCandidate,
    _index_usage,
    apply_rank_scores,
    build_rank_records,
    build_ranking_client,
    prepare_query,
    row_to_candidate,
    run_hybrid_query_job,
)
from app.engines.level3_multimodal import (
    ELEMENT_INTENT_KEYWORDS,
    L3_INSTRUCTION,
    L3PreparedQuery,
    MultimodalSearch,
    build_authority_context,
    candidate_summary,
    prepare_l3_query,
)
from app.engines.level4_agentic import (
    DEADLINE_MARGIN_SECONDS,
    FINAL_ANSWER_SOURCE,
    MAX_JUDGE_CHUNKS,
    ResolvedCitations,
    _layout_source,
    _tool_calls_by_name,
    final_answer_check,
    resolve_citations,
    user_message,
    verification_summary,
)
from app.engines.managed_vertex import ManagedVertexSearchEngine, is_upload_scope
from app.engines.rag_engine import LAYOUT_SOURCE as RAG_ENGINE_LAYOUT_SOURCE, RagEngine, hits_to_chunks
from app.engines.replay_engine import ReplayRAGEngine, ReplayUnavailableError
from app.engines.vais_search import LAYOUT_SOURCE as VAIS_LAYOUT_SOURCE, segment_chunks
from app.engines.vertex_vector_v2 import LAYOUT_SOURCE as VVS2_LAYOUT_SOURCE, VertexVectorSearchV2Engine
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, build_lexical_search_sql, build_lexical_terms
from app.ingestion.uploads import check_workspace_id

_MAX_CONTEXT_CHUNK_CHARS = 2400
_MAX_CANDIDATES_IN_PAYLOAD = 20
_EMBEDDING_PREVIEW_DIMS = 4


class _AdkChunkBlockedError(RuntimeError):
    """Internal control signal raised when Model Armor blocks a tool-retrieved chunk inside an ADK run."""

    def __init__(self, chunk: DocumentChunk, matched_filters: tuple[str, ...], tokens: int, latency_ms: int) -> None:
        super().__init__(f"Model Armor blocked tool chunk {chunk.chunk_id}")
        self.chunk = chunk
        self.matched_filters = matched_filters
        self.tokens = tokens
        self.latency_ms = latency_ms


def run_lexical_query_job(
    bq_client: Any,
    sql: str,
    lexical_terms: tuple[str, ...],
    solicitation_filter: str | None,
    timeout_seconds: int,
    *,
    caller_groups: tuple[str, ...] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Execute a parameterized BigQuery SEARCH() + exact-term query in a worker thread."""
    from google.cloud import bigquery

    params: list[Any] = [
        bigquery.ScalarQueryParameter("lexical_query", "STRING", " ".join(lexical_terms)),
        bigquery.ArrayQueryParameter("lexical_terms", "STRING", list(lexical_terms)),
    ]
    if solicitation_filter:
        params.append(bigquery.ScalarQueryParameter("solicitation_id", "STRING", solicitation_filter.strip()))
    if caller_groups:
        params.append(bigquery.ArrayQueryParameter("caller_groups", "STRING", list(caller_groups)))
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


def _row_to_bq_candidate(row: Any, rank: int, tier_profile: str, mode: str) -> HybridCandidate:
    if mode == "hybrid_rrf":
        base = row_to_candidate(row, rank, tier_profile=tier_profile)
        chunk = replace(
            base.chunk,
            layout_source=str(getattr(row, "layout_source", "") or ""),
            bbox_source=str(getattr(row, "bbox_source", "") or ""),
        )
        return replace(base, chunk=chunk)
    page_num = int(row.page_number)
    effective = getattr(row, "effective_date", None)
    raw_score = float(getattr(row, "similarity_score", 0.0) if mode == "vector_cosine" else getattr(row, "lexical_score", 0) or 0)
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
        score=round(raw_score, 4),
        layout_source=str(getattr(row, "layout_source", "") or ""),
        bbox_source=str(getattr(row, "bbox_source", "") or ""),
    )
    return HybridCandidate(
        chunk=chunk,
        rrf_rank=rank,
        rrf_score=round(raw_score, 6),
        vector_rank=rank if mode == "vector_cosine" else None,
        lexical_rank=rank if mode == "lexical_bm25" else None,
        vector_distance=round(1.0 - raw_score, 6) if mode == "vector_cosine" else None,
        lexical_score=int(raw_score) if mode == "lexical_bm25" else None,
    )


def _chunks_to_candidates(chunks: list[DocumentChunk]) -> list[HybridCandidate]:
    return [
        HybridCandidate(
            chunk=ch,
            rrf_rank=idx,
            rrf_score=round(float(ch.score or 0.0), 6),
            vector_rank=idx,
            lexical_rank=None,
            vector_distance=None,
            lexical_score=None,
        )
        for idx, ch in enumerate(chunks, start=1)
    ]


class _UnrecordedCustomReplayEngine(BaseRAGEngine):
    """Fails loud in ``DEMO_MODE=replay`` for non-preset custom recipes."""

    def __init__(self, spec: PipelineSpec, settings: ArenaSettings) -> None:
        self.spec = spec
        self.engine_id = spec.pipeline_id
        self.engine_label = spec.summary_label(settings)
        self.tier_profile = spec.index_profile
        self.stages = active_query_stages(spec)
        super().__init__(settings=settings)

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        self.validate_query_inputs(query, solicitation_filter)
        raise ReplayUnavailableError(
            f"Custom pipeline recipe '{self.engine_label}' has no pre-recorded replay fixture; "
            "switch to Live mode (DEMO_MODE=live) to execute custom component combinations"
        )


class ComposableRAGEngine(BaseRAGEngine):
    """Modular executor for any validated ``PipelineSpec`` in ``live`` / ``record`` mode."""

    def __init__(
        self,
        spec: PipelineSpec,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
        vais_client: DiscoveryEngineClient | None = None,
        rag_client: VertexRagClient | None = None,
        alloydb_conn_factory: Any | None = None,
        vvs2_client: Any | None = None,
        model_armor_client: ModelArmorClient | None = None,
    ) -> None:
        if not isinstance(spec, PipelineSpec):
            raise TypeError("spec must be a PipelineSpec instance")
        from app.core.config import get_settings

        resolved_cfg = get_settings() if settings is None else settings
        if not isinstance(resolved_cfg, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance or None")
        preset_id = spec.preset_level_id(resolved_cfg)
        self.spec = spec
        self.engine_id = preset_id or spec.pipeline_id
        self.engine_label = spec.summary_label(resolved_cfg)
        self.tier_profile = spec.index_profile
        self.stages = active_query_stages(spec)
        super().__init__(settings=resolved_cfg)
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(
            self.settings, self.settings.gemini_fast_model
        )
        if spec.slots.reasoning_mode == "adk_agent":
            ensure_agent_model(self.synthesis_model)
        self._ranking_client = ranking_client
        self._vais_client = vais_client
        self._rag_client = rag_client
        self._alloydb_conn_factory = alloydb_conn_factory
        self._vvs2_client = vvs2_client
        self._model_armor_client = model_armor_client
        self._adk_screen_chunks = False
        self._adk_screened_tokens = 0

    def _resolve_model_armor_client(self) -> ModelArmorClient:
        return self._model_armor_client or build_model_armor_client(self.settings)

    async def _ensure_guardrail_ready(self, emitter: StageEmitter) -> None:
        """Fail loud immediately before any paid GCP call when live Model Armor lacks a template ID."""
        if self.spec.guardrail != "model_armor" or self._model_armor_client is not None:
            return
        if not self.settings.model_armor_template_id.strip():
            async with self.stage(
                emitter,
                "guardrail_in",
                label="Model Armor input & context guardrail",
                service=MODEL_ARMOR_SERVICE_LABEL,
                request={"location": self.settings.model_armor_location, "configured": False},
            ):
                ensure_model_armor_configured(self.settings)

    async def _screen_input_and_chunks(
        self,
        cleaned_prompt: str,
        chunks: list[DocumentChunk],
        emitter: StageEmitter,
    ) -> tuple[bool, str, float]:
        """Screen user prompt + untrusted retrieved chunks via ``:sanitizeUserPrompt`` before synthesis."""
        if self.spec.guardrail != "model_armor":
            return False, "", 0.0
        client = self._resolve_model_armor_client()
        req = {
            "service": MODEL_ARMOR_SERVICE_LABEL,
            "endpoint": f"modelarmor.{client.location}.rep.googleapis.com",
            "method": "sanitizeUserPrompt",
            "location": client.location,
            "chunks_to_screen": len(chunks),
        }
        async with self.stage(
            emitter,
            "guardrail_in",
            label="Model Armor input & context guardrail",
            service=MODEL_ARMOR_SERVICE_LABEL,
            request=req,
        ) as ctx:
            prompt_verdict = await self.blocking(client.sanitize_user_prompt, cleaned_prompt)
            tokens = prompt_verdict.tokens_screened
            latency_ms = prompt_verdict.duration_ms
            calls = 1
            if prompt_verdict.matched:
                filters = list(prompt_verdict.matched_filters)
                ctx.payload = {
                    **model_armor_pricing_summary(tokens),
                    "service_used": MODEL_ARMOR_SERVICE_LABEL,
                    "match_state": "MATCH_FOUND",
                    "blocked": True,
                    "blocked_target": "user_prompt",
                    "matched_filters": filters,
                    "prompt_verdict": prompt_verdict.summary_dict(),
                    "chunks_screened": 0,
                    "calls": calls,
                    "latency_ms": max(0, latency_ms),
                }
                answer = (
                    "Synthesis halted by Google Cloud Model Armor before LLM invocation "
                    f"(filterMatchState=MATCH_FOUND; target=user_prompt; matched_filters={', '.join(filters)})."
                )
                return True, answer, model_armor_cost_usd(tokens)

            for chunk in chunks:
                chunk_verdict = await self.blocking(
                    client.sanitize_user_prompt, chunk.content[:MAX_SANITIZE_TEXT_CHARS]
                )
                tokens += chunk_verdict.tokens_screened
                latency_ms += chunk_verdict.duration_ms
                calls += 1
                if chunk_verdict.matched:
                    filters = list(chunk_verdict.matched_filters)
                    ctx.payload = {
                        **model_armor_pricing_summary(tokens),
                        "service_used": MODEL_ARMOR_SERVICE_LABEL,
                        "match_state": "MATCH_FOUND",
                        "blocked": True,
                        "blocked_target": "retrieved_chunk",
                        "blocked_chunk_id": chunk.chunk_id,
                        "blocked_doc_name": chunk.doc_name,
                        "matched_filters": filters,
                        "prompt_verdict": prompt_verdict.summary_dict(),
                        "chunk_verdict": chunk_verdict.summary_dict(),
                        "chunks_screened": calls - 1,
                        "calls": calls,
                        "latency_ms": max(0, latency_ms),
                    }
                    answer = (
                        "Synthesis halted by Google Cloud Model Armor before LLM invocation "
                        f"(filterMatchState=MATCH_FOUND; target=retrieved_chunk; chunk_id={chunk.chunk_id}; "
                        f"doc={chunk.doc_name}; matched_filters={', '.join(filters)})."
                    )
                    return True, answer, model_armor_cost_usd(tokens)

            ctx.payload = {
                **model_armor_pricing_summary(tokens),
                "service_used": MODEL_ARMOR_SERVICE_LABEL,
                "match_state": "NO_MATCH_FOUND",
                "blocked": False,
                "matched_filters": [],
                "prompt_verdict": prompt_verdict.summary_dict(),
                "chunks_screened": len(chunks),
                "calls": calls,
                "latency_ms": max(0, latency_ms),
            }
            return False, "", model_armor_cost_usd(tokens)

    async def _screen_model_output(
        self,
        answer: str,
        cleaned_prompt: str,
        emitter: StageEmitter,
    ) -> tuple[bool, str, float]:
        """Screen synthesized LLM response via ``:sanitizeModelResponse`` before returning it."""
        if self.spec.guardrail != "model_armor":
            return False, answer, 0.0
        client = self._resolve_model_armor_client()
        req = {
            "service": MODEL_ARMOR_SERVICE_LABEL,
            "endpoint": f"modelarmor.{client.location}.rep.googleapis.com",
            "method": "sanitizeModelResponse",
            "location": client.location,
        }
        async with self.stage(
            emitter,
            "guardrail_out",
            label="Model Armor output guardrail",
            service=MODEL_ARMOR_SERVICE_LABEL,
            request=req,
        ) as ctx:
            verdict = await self.blocking(client.sanitize_model_response, answer, user_prompt=cleaned_prompt)
            filters = list(verdict.matched_filters)
            ctx.payload = {
                **model_armor_pricing_summary(verdict.tokens_screened),
                "service_used": MODEL_ARMOR_SERVICE_LABEL,
                "match_state": verdict.match_state,
                "blocked": verdict.matched,
                "blocked_target": "model_response" if verdict.matched else None,
                "matched_filters": filters,
                "response_verdict": verdict.summary_dict(),
                "calls": 1,
                "latency_ms": max(0, verdict.duration_ms),
            }
            if verdict.matched:
                safe_answer = (
                    "Model response withheld by Google Cloud Model Armor post-synthesis guardrail "
                    f"(filterMatchState=MATCH_FOUND; target=model_response; matched_filters={', '.join(filters)})."
                )
                return True, safe_answer, model_armor_cost_usd(verdict.tokens_screened)
            return False, answer, model_armor_cost_usd(verdict.tokens_screened)

    async def _finish_single_pass_with_guardrail(
        self,
        *,
        cleaned: str,
        top: list[DocumentChunk],
        emb_tokens: int,
        base_cost: float,
        rank_cost: float,
        reranked_flag: bool,
        layout: str,
        start: float,
        emitter: StageEmitter,
    ) -> EngineResult:
        blocked_in, blocked_answer, in_cost = await self._screen_input_and_chunks(cleaned, top, emitter)
        if blocked_in:
            await emitter.skipped("synthesize", reason="Halted by Google Cloud Model Armor (MATCH_FOUND before synthesis)")
            await emitter.skipped("guardrail_out", reason="Synthesis skipped due to pre-synthesis guardrail block")
            usage = TokenUsage(
                prompt_tokens=0,
                output_tokens=0,
                embedding_tokens=emb_tokens,
                model=self.synthesis_model.id,
                embedding_model=self._embed_model if emb_tokens > 0 else "",
            )
            return EngineResult(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                answer=blocked_answer,
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + base_cost + rank_cost + in_cost, 8),
                stage_log=self.stage_log_of(emitter),
                usage=usage,
                flags=EngineFlags(reranker_used=reranked_flag, adk_runner_used=False, layout_source=layout),
            )

        answer, usage = await self._synthesize_chunks(cleaned, top, emitter, emb_tokens)
        blocked_out, final_answer, out_cost = await self._screen_model_output(answer, cleaned, emitter)
        cites = [] if blocked_out else self.chunks_to_citations(top, max_citations=len(top))
        kept_chunks = [] if blocked_out else top
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=final_answer,
            citations=cites,
            retrieved_chunks=kept_chunks,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(
                cost_from_usage(usage, self.synthesis_model.id) + base_cost + rank_cost + in_cost + out_cost, 8
            ),
            stage_log=self.stage_log_of(emitter),
            usage=usage,
            flags=EngineFlags(reranker_used=reranked_flag, adk_runner_used=False, layout_source=layout),
        )

    @property
    def _embed_model(self) -> str:
        if self.spec.index_profile == "multimodal":
            return self.settings.multimodal_embedding_model
        if self.spec.index_profile == "rag_engine":
            return self.settings.rag_engine_embedding_model
        return self.settings.text_embedding_model

    @property
    def _embed_col(self) -> str:
        return "embedding_multimodal" if self.spec.index_profile == "multimodal" else "embedding"

    def _transform_query_local(self, query: str) -> tuple[str, str, L3PreparedQuery]:
        l3 = prepare_l3_query(query)
        mode = self.spec.slots.query_transform
        if mode == "none":
            norm = " ".join(query.split())
            empty_base = PreparedQuery(original=norm, prepared=norm, expansions=())
            return norm, norm, L3PreparedQuery(base=empty_base, element_intent=(), intent_matches=())
        if mode == "acronym":
            return l3.base.prepared, l3.base.original, L3PreparedQuery(base=l3.base, element_intent=(), intent_matches=())
        return l3.base.prepared, l3.base.original, l3

    async def _run_query_prep(self, query: str, emitter: StageEmitter, *, upload_scope: str | None = None) -> tuple[str, str, L3PreparedQuery]:
        mode = self.spec.slots.query_transform
        label = {
            "none": "Query preparation",
            "acronym": "Query preparation (acronym expansion)",
            "acronym_plus_intent": "Query preparation (acronyms + element intent)",
        }[mode]
        req: dict[str, Any] = {"query": query, "query_transform": mode}
        if mode != "none":
            req["acronym_map"] = [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS]
        if mode == "acronym_plus_intent":
            req["intent_keywords"] = {i: list(w) for i, w in ELEMENT_INTENT_KEYWORDS}
        async with self.stage(emitter, "query_prep", label=label, service="Engine (local)", request=req) as ctx:
            prepared, original, l3 = self._transform_query_local(query)
            payload = l3.payload() if mode == "acronym_plus_intent" else l3.base.payload()
            if self.spec.slots.reasoning_mode == "adk_agent":
                payload = {**payload, "handed_to_agent": user_message(l3, upload_scope=upload_scope)}
            ctx.payload = payload
        return prepared, original, l3

    async def _retrieve_bq(
        self,
        prepared: str,
        original: str,
        sol_filter: str | None,
        genai_client: Any,
        bq_client: Any,
        emitter: StageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
    ) -> tuple[list[HybridCandidate], int]:
        mode = self.spec.slots.retrieval_mode
        cand_k = self.spec.slots.candidate_k
        tier = self.spec.index_profile
        restricted = bool(caller_access and caller_access.is_restricted)
        caller_groups = caller_access.effective_groups if restricted and caller_access is not None else None
        acl_level = {"naive": "level1_naive", "hybrid": "level2_hybrid", "multimodal": "level3_multimodal"}.get(tier, "level2_hybrid")
        vector: list[float] = []
        emb_tokens = 0
        if mode in ("vector_cosine", "hybrid_rrf"):
            vector, emb_tokens = await embed_query_stage(self, prepared, genai_client, emitter, model=self._embed_model)

        if mode == "vector_cosine":
            sql = build_vector_search_sql(
                self.settings.gcp_project_id, self.settings.bq_dataset, self.settings.bq_table,
                top_k=cand_k, include_solicitation_filter=bool(sol_filter),
                tier_profile=tier, embedding_column=self._embed_col, include_acl_filter=restricted,
            )
            params: dict[str, Any] = {"query_embedding": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]}}
            if sol_filter:
                params["solicitation_id"] = sol_filter
            if caller_groups:
                params["caller_groups"] = list(caller_groups)
            async with self.stage(
                emitter, "retrieve", label=f"BigQuery VECTOR_SEARCH ({tier})", service="BigQuery",
                request={"sql": sql, "params": params, "candidate_k": cand_k, "tier_profile": tier},
            ) as ctx:
                if caller_groups:
                    rows, stats = await self.blocking(
                        _run_vector_search_job, bq_client, sql, vector, sol_filter, self.settings.query_timeout_seconds,
                        caller_groups=caller_groups,
                    )
                else:
                    rows, stats = await self.blocking(
                        _run_vector_search_job, bq_client, sql, vector, sol_filter, self.settings.query_timeout_seconds
                    )
                cands = [_row_to_bq_candidate(r, idx, tier, mode) for idx, r in enumerate(rows[:cand_k], start=1)]
                if not cands and not restricted:
                    raise RuntimeError(f"vector_cosine retrieval returned 0 rows for tier '{tier}'")
                ctx.payload = {
                    "rows_returned": len(rows), "top_k": cand_k, **stats,
                    "candidates": [candidate_summary(c) for c in cands[:_MAX_CANDIDATES_IN_PAYLOAD]],
                    **({**acl_stage_metadata(acl_level, caller_access), "acl_access_denied": len(cands) == 0} if restricted else {}),
                }
            return cands, emb_tokens

        terms = build_lexical_terms(original)
        if mode == "lexical_bm25":
            sql = build_lexical_search_sql(
                self.settings.gcp_project_id, self.settings.bq_dataset, self.settings.bq_table,
                top_k=cand_k, include_solicitation_filter=bool(sol_filter), tier_profile=tier, include_acl_filter=restricted,
            )
            params = {"lexical_query": " ".join(terms), "lexical_terms": list(terms)}
            if sol_filter:
                params["solicitation_id"] = sol_filter
            if caller_groups:
                params["caller_groups"] = list(caller_groups)
            async with self.stage(
                emitter, "retrieve", label=f"BigQuery SEARCH() lexical ({tier})", service="BigQuery",
                request={"sql": sql, "params": params, "candidate_k": cand_k, "tier_profile": tier},
            ) as ctx:
                if caller_groups:
                    rows, stats = await self.blocking(
                        run_lexical_query_job, bq_client, sql, terms, sol_filter, self.settings.query_timeout_seconds,
                        caller_groups=caller_groups,
                    )
                else:
                    rows, stats = await self.blocking(
                        run_lexical_query_job, bq_client, sql, terms, sol_filter, self.settings.query_timeout_seconds
                    )
                cands = [_row_to_bq_candidate(r, idx, tier, mode) for idx, r in enumerate(rows[:cand_k], start=1)]
                if not cands and not restricted:
                    raise RuntimeError(f"lexical_bm25 retrieval returned 0 matching rows for tier '{tier}'")
                ctx.payload = {
                    "rows_returned": len(rows), "candidate_k": cand_k, **stats,
                    "candidates": [candidate_summary(c) for c in cands[:_MAX_CANDIDATES_IN_PAYLOAD]],
                    **({**acl_stage_metadata(acl_level, caller_access), "acl_access_denied": len(cands) == 0} if restricted else {}),
                }
            return cands, 0

        leg_k = max(cand_k, 50)
        sql = build_hybrid_rrf_sql(
            self.settings.gcp_project_id, self.settings.bq_dataset, self.settings.bq_table,
            rrf_k=RRF_K, top_k=cand_k, include_solicitation_filter=bool(sol_filter),
            embedding_column=self._embed_col, tier_profile=tier, vector_k=leg_k, lexical_k=leg_k,
            include_acl_filter=restricted,
        )
        params = {
            "query_embedding": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
            "lexical_query": " ".join(terms), "lexical_terms": list(terms),
        }
        if sol_filter:
            params["solicitation_id"] = sol_filter
        if caller_groups:
            params["caller_groups"] = list(caller_groups)
        async with self.stage(
            emitter, "retrieve", label=f"BigQuery VECTOR_SEARCH + SEARCH() → RRF ({tier})", service="BigQuery",
            request={"sql": sql, "params": params, "candidate_k": cand_k, "tier_profile": tier},
        ) as ctx:
            if caller_groups:
                rows, stats = await self.blocking(
                    run_hybrid_query_job, bq_client, sql, vector, terms, sol_filter, self.settings.query_timeout_seconds,
                    caller_groups=caller_groups,
                )
            else:
                rows, stats = await self.blocking(
                    run_hybrid_query_job, bq_client, sql, vector, terms, sol_filter, self.settings.query_timeout_seconds
                )
            cands = [_row_to_bq_candidate(r, idx, tier, mode) for idx, r in enumerate(rows[:cand_k], start=1)]
            if not cands and not restricted:
                raise RuntimeError(f"hybrid_rrf retrieval returned 0 rows for tier '{tier}'")
            ctx.payload = {
                "rows_returned": len(rows), "candidate_k": cand_k, "rrf_k": RRF_K, **stats,
                "candidates": [candidate_summary(c) for c in cands[:_MAX_CANDIDATES_IN_PAYLOAD]],
                **({**acl_stage_metadata(acl_level, caller_access), "acl_access_denied": len(cands) == 0} if restricted else {}),
            }
        return cands, emb_tokens

    async def _rerank(self, query: str, candidates: list[HybridCandidate], emitter: StageEmitter) -> list[RerankedCandidate]:
        client = self._ranking_client or build_ranking_client(self.settings)
        records = build_rank_records(candidates)
        top_n = len(records)
        async with self.stage(
            emitter, "rerank", label="Semantic rerank (cross-encoder)", service=RANKING_SERVICE,
            request={
                "service": RANKING_SERVICE, "model": client.model, "top_n": top_n,
                "records_sent": len(records), "query": query, "context_k": self.spec.slots.top_k,
            },
        ) as ctx:
            response = await self.blocking(client.rank, query, records, top_n)
            ranked = apply_rank_scores(candidates, response.scores)
            counts = rank_billed_counts(len(records))
            ctx.payload = {
                "service_used": RANKING_SERVICE, "model": client.model, "candidates_in": len(candidates),
                "candidates_out": min(self.spec.slots.top_k, len(ranked)),
                "results": [r.summary() for r in ranked], "rank_api_ms": response.duration_ms,
                "attempts": response.attempts, "billed_counts": counts, "cost_usd": rank_cost_usd(counts),
            }
        return ranked

    async def _synthesize_chunks(
        self, query: str, chunks: list[DocumentChunk], emitter: StageEmitter, emb_tokens: int
    ) -> tuple[str, TokenUsage]:
        if self.spec.slots.context_order == "authority_amendment_first":
            context_block, headers, mix = build_authority_context(chunks)
            return await synthesize_stage(
                self, self.synthesis_model, query, context_block, len(chunks), emitter, emb_tokens,
                instruction=L3_INSTRUCTION, embedding_model=self._embed_model,
                extra_payload={"authority_headers": headers, "element_mix": mix, "context_order": "authority_amendment_first"},
            )
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id or 'GENERAL'} | p.{c.page_number}]: {c.content[:_MAX_CONTEXT_CHUNK_CHARS]}"
            for c in chunks
        )
        return await synthesize_stage(
            self, self.synthesis_model, query, context_block, len(chunks), emitter, emb_tokens,
            embedding_model=self._embed_model, extra_payload={"context_order": "score_order"},
        )

    async def _acl_zero_result(
        self,
        *,
        cleaned: str,
        caller_access: CallerAccessContext,
        emb_tokens: int,
        base_cost: float,
        layout: str,
        start: float,
        emitter: StageEmitter,
    ) -> EngineResult:
        if "rerank" in self.stages:
            await emitter.skipped("rerank", reason=f"0 authorized chunks after ACL filtering ({caller_access.persona_id})")
        if "guardrail_in" in self.stages:
            await emitter.skipped("guardrail_in", reason="0 authorized chunks after ACL filtering")
        refusal = acl_refusal_answer(caller_access, cleaned)
        async with self.stage(
            emitter, "synthesize", label="Row-level security refusal (0 authorized chunks)", service="ACL Policy (local)",
            request={"query": cleaned, "context_chunks": 0, **acl_stage_metadata(self.spec.preset_level_id(self.settings) or "level2_hybrid", caller_access)},
        ) as ctx:
            ctx.payload = {
                "model": self.synthesis_model.id, "answer": refusal, "prompt_tokens": 0, "output_tokens": 0,
                "acl_access_denied": True, **acl_stage_metadata(self.spec.preset_level_id(self.settings) or "level2_hybrid", caller_access),
            }
        if "guardrail_out" in self.stages:
            await emitter.skipped("guardrail_out", reason="Deterministic ACL refusal (no synthesized LLM output)")
        usage = TokenUsage(
            embedding_tokens=emb_tokens,
            model=self.synthesis_model.id,
            embedding_model=self._embed_model if emb_tokens > 0 else "",
        )
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer=refusal,
            citations=[], retrieved_chunks=[], agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + base_cost, 8),
            stage_log=self.stage_log_of(emitter), usage=usage,
            flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=layout),
        )

    async def _run_scann_single_pass(
        self,
        cleaned: str,
        prepared: str,
        original: str,
        sol_filter: str | None,
        start: float,
        emitter: StageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        slots = self.spec.slots
        genai_client = gcp_clients.get_genai_client(self.settings)
        vector, emb_tokens = await embed_query_stage(self, prepared, genai_client, emitter, model=self._embed_model)
        if self.spec.index_profile == "alloydb_scann":
            helper_alloy = AlloyDBScaNNEngine(
                settings=self.settings,
                synthesis_model=self.synthesis_model,
                ranking_client=self._ranking_client,
                conn_factory=self._alloydb_conn_factory,
            )
            cands = await helper_alloy._retrieve_candidates(
                vector, original, sol_filter, emitter, candidate_k=slots.candidate_k, caller_access=caller_access
            )
            layout = ALLOYDB_LAYOUT_SOURCE
        else:
            helper_vvs = VertexVectorSearchV2Engine(
                settings=self.settings,
                synthesis_model=self.synthesis_model,
                ranking_client=self._ranking_client,
                vvs2_client=self._vvs2_client,
            )
            cands = await helper_vvs._retrieve_candidates(
                vector, original, sol_filter, emitter, candidate_k=slots.candidate_k, caller_access=caller_access
            )
            layout = VVS2_LAYOUT_SOURCE

        if not cands and caller_access is not None and caller_access.is_restricted:
            return await self._acl_zero_result(
                cleaned=cleaned, caller_access=caller_access, emb_tokens=emb_tokens,
                base_cost=0.0, layout=layout, start=start, emitter=emitter,
            )
        rank_cost = 0.0
        reranked_flag = False
        if slots.reranker == "vertex_ranking_api":
            ranked = await self._rerank(original, cands, emitter)
            top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: slots.top_k]]
            rank_cost = rank_cost_usd(rank_billed_counts(len(cands)))
            reranked_flag = True
        else:
            top = [c.chunk for c in cands[: slots.top_k]]
        return await self._finish_single_pass_with_guardrail(
            cleaned=cleaned,
            top=top,
            emb_tokens=emb_tokens,
            base_cost=0.0,
            rank_cost=rank_cost,
            reranked_flag=reranked_flag,
            layout=layout,
            start=start,
            emitter=emitter,
        )

    async def _run_managed_single_pass(
        self,
        cleaned: str,
        prepared: str,
        original: str,
        sol_filter: str | None,
        start: float,
        emitter: StageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        slots = self.spec.slots
        if self.spec.index_profile == "vertex_ai_search":
            is_upload_scope(sol_filter)
            cfg = replace(self.settings, vais_page_size=slots.candidate_k, vais_summary_result_count=slots.top_k)
            helper = ManagedVertexSearchEngine(settings=cfg, synthesis_model=self.synthesis_model, client=self._vais_client)
            outcome = await helper._retrieve(
                PreparedQuery(original, prepared, ()), emitter, solicitation_filter=sol_filter, caller_access=caller_access
            )
            if not outcome.hits and caller_access is not None and caller_access.is_restricted:
                return await self._acl_zero_result(
                    cleaned=cleaned, caller_access=caller_access, emb_tokens=0,
                    base_cost=vais_query_cost_usd(searches=1, summary_queries=0),
                    layout=VAIS_LAYOUT_SOURCE, start=start, emitter=emitter,
                )
            if self.synthesis_model.provider == PROVIDER_VERTEX_AI_SEARCH:
                answer, usage, cites, _ = await helper._synthesize_summary(outcome, emitter)
                ctx_chunks = segment_chunks(helper._context_hits(outcome))
                return EngineResult(
                    engine_id=self.engine_id, engine_label=self.engine_label, answer=answer,
                    citations=self.chunks_to_citations(cites, max_citations=min(len(cites), 50)) if cites else [], retrieved_chunks=ctx_chunks,
                    agent_steps=[], latency_ms=max(1, int((time.perf_counter() - start) * 1000)), cost_usd=helper._cost(usage),
                    stage_log=self.stage_log_of(emitter), usage=usage,
                    flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=VAIS_LAYOUT_SOURCE),
                )
            raw_chunks = segment_chunks(outcome.hits if slots.reranker == "vertex_ranking_api" else helper._context_hits(outcome))
            base_cost = vais_query_cost_usd(searches=1, summary_queries=0)
            layout = VAIS_LAYOUT_SOURCE
        else:
            if caller_access is not None and caller_access.is_restricted:
                async with self.stage(
                    emitter, "retrieve", label="Vertex AI RAG Engine (:retrieveContexts)", service="Vertex AI RAG Engine",
                    request={"query": prepared, **acl_stage_metadata("rag_engine", caller_access)},
                ):
                    raise AclEnforcementUnsupportedError(
                        "Vertex AI RAG Engine enforces IAM at the RagCorpus boundary, not per-document chunk ACLs "
                        f"(cannot safely restrict persona '{caller_access.persona_id}' inside a shared corpus)."
                    )
            cfg = replace(self.settings, rag_engine_top_k=slots.candidate_k)
            helper_rag = RagEngine(settings=cfg, synthesis_model=self.synthesis_model, client=self._rag_client)
            hits, res, file_ids, corpus_label = await helper_rag._retrieve(
                PreparedQuery(original, prepared, ()), emitter, solicitation_filter=sol_filter
            )
            raw_chunks = hits_to_chunks(hits)
            if self.synthesis_model.provider == PROVIDER_VERTEX_RAG_ENGINE:
                answer, usage, priced_id = await helper_rag._synthesize_grounded(cleaned, res, file_ids, corpus_label, emitter)
                top_chunks = raw_chunks[: slots.top_k]
                return EngineResult(
                    engine_id=self.engine_id, engine_label=self.engine_label, answer=answer,
                    citations=self.chunks_to_citations(top_chunks, max_citations=len(top_chunks)), retrieved_chunks=top_chunks,
                    agent_steps=[], latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                    cost_usd=cost_from_usage(usage, priced_id), stage_log=self.stage_log_of(emitter), usage=usage,
                    flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=RAG_ENGINE_LAYOUT_SOURCE),
                )
            base_cost = 0.0
            layout = RAG_ENGINE_LAYOUT_SOURCE

        rank_cost = 0.0
        reranked_flag = False
        if slots.reranker == "vertex_ranking_api":
            cands = _chunks_to_candidates(raw_chunks)
            ranked = await self._rerank(original, cands, emitter)
            top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: slots.top_k]]
            rank_cost = rank_cost_usd(rank_billed_counts(len(cands)))
            reranked_flag = True
        else:
            top = raw_chunks[: slots.top_k] if self.spec.index_profile == "rag_engine" else raw_chunks
        return await self._finish_single_pass_with_guardrail(
            cleaned=cleaned,
            top=top,
            emb_tokens=0,
            base_cost=base_cost,
            rank_cost=rank_cost,
            reranked_flag=reranked_flag,
            layout=layout,
            start=start,
            emitter=emitter,
        )

    async def search_multimodal(
        self,
        query: str,
        *,
        solicitation_filter: str | None,
        element_types: tuple[str, ...],
        genai_client: Any,
        bq_client: Any,
        emitter: StageEmitter,
        caller_access: CallerAccessContext | None = None,
    ) -> MultimodalSearch:
        """Retriever entrypoint used by ADK ``search_procurement_corpus`` — honors ``spec`` retrieval + reranker slots."""
        prepared, original, _ = self._transform_query_local(query)
        cands, emb_tokens = await self._retrieve_bq(
            prepared, original, solicitation_filter, genai_client, bq_client, emitter, caller_access=caller_access
        )
        kept = [c for c in cands if not element_types or c.chunk.element_type in element_types]
        if self.spec.slots.reranker == "vertex_ranking_api" and kept:
            ranked = await self._rerank(original, kept, emitter)
            top = tuple(replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.spec.slots.top_k])
            billed = rank_billed_counts(len(kept))
        else:
            top = tuple(c.chunk for c in kept[: self.spec.slots.top_k])
            billed = 0
        if self._adk_screen_chunks and top:
            armor_client = self._resolve_model_armor_client()
            for chunk in top:
                verdict = await self.blocking(
                    armor_client.sanitize_user_prompt, chunk.content[:MAX_SANITIZE_TEXT_CHARS]
                )
                self._adk_screened_tokens += verdict.tokens_screened
                if verdict.matched:
                    raise _AdkChunkBlockedError(
                        chunk, verdict.matched_filters, self._adk_screened_tokens, verdict.duration_ms
                    )
        return MultimodalSearch(
            chunks=top, candidates=len(cands), filtered_candidates=len(kept),
            embedding_tokens=emb_tokens, rank_billed_counts=billed, stage_records=self.stage_log_of(emitter),
        )

    async def _run_adk_agent(
        self,
        cleaned: str,
        sol_filter: str | None,
        start: float,
        emitter: BaseStageEmitter,
        *,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        upload_scope: str | None = None
        if sol_filter:
            if not sol_filter.startswith("upl_"):
                raise ValueError("ADK agent chooses corpus solicitation filters itself; solicitation_filter is only for upl_* workspaces")
            upload_scope = check_workspace_id(sol_filter)
        deadline = time.monotonic() + self.settings.query_timeout_seconds - DEADLINE_MARGIN_SECONDS
        _, _, l3 = await self._run_query_prep(cleaned, emitter, upload_scope=upload_scope)
        blocked_in, blocked_answer, in_cost = await self._screen_input_and_chunks(cleaned, [], emitter)
        if blocked_in:
            await emitter.skipped("agent_turn", reason="Halted by Google Cloud Model Armor (MATCH_FOUND on user prompt)")
            await emitter.skipped("synthesize", reason="Halted by Google Cloud Model Armor (MATCH_FOUND before agent loop)")
            await emitter.skipped("guardrail_out", reason="Agent loop skipped due to pre-synthesis guardrail block")
            return EngineResult(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                answer=blocked_answer,
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                cost_usd=round(in_cost, 8),
                stage_log=self.stage_log_of(emitter),
                usage=TokenUsage(model=self.synthesis_model.id),
                flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source="pymupdf"),
            )

        ledger = AgentLedger(run_id=emitter.run_id)
        retriever_spec = replace(
            self.spec,
            slots=replace(self.spec.slots, reasoning_mode="single_pass", agent_tools=()),
            guardrail="none",
        )
        retriever = ComposableRAGEngine(
            retriever_spec,
            settings=self.settings,
            synthesis_model=self.synthesis_model,
            ranking_client=self._ranking_client,
            model_armor_client=self._model_armor_client,
        )
        retriever._adk_screen_chunks = self.spec.guardrail == "model_armor"
        deps = ToolDeps(
            settings=self.settings, retriever=retriever, genai_client=gcp_clients.get_genai_client(self.settings),
            bq_client=gcp_clients.get_bigquery_client(self.settings), ledger=ledger, upload_scope=upload_scope,
            tier_profile=self.spec.index_profile, caller_access=caller_access,
        )
        message = user_message(l3, upload_scope=upload_scope)
        limits = AgentLimits(
            max_turns=self.settings.agent_max_turns,
            max_tool_calls_per_turn=self.settings.agent_max_tool_calls_per_turn,
            budget_seconds=max(1.0, deadline - time.monotonic()),
        )
        enabled_tools = self.spec.slots.agent_tools
        req = {
            "framework": "google-adk", "agent": "LlmAgent", "runner": "InMemoryRunner",
            "model": self.synthesis_model.id, "tools": list(enabled_tools),
            "instruction": agent_instruction_for_tools(limits.max_tool_calls_per_turn, enabled_tools),
            "max_turns": limits.max_turns, "max_tool_calls_per_turn": limits.max_tool_calls_per_turn,
            "model_call_attempts": MODEL_CALL_ATTEMPTS,
        }
        chunk_blocked: _AdkChunkBlockedError | None = None
        async with self.stage(
            emitter, "agent_turn", label="ADK agent loop (tools)",
            service=f"Google ADK · Vertex AI · {self.synthesis_model.label}", request=req,
        ) as ctx:
            tools = build_tools(deps, enabled_tools=enabled_tools)
            agent = build_agent(self.synthesis_model, self.settings, tools, enabled_tools=enabled_tools)
            try:
                outcome: AgentOutcome = await run_agent(agent, message, limits=limits, ledger=ledger, emitter=emitter)
            except _AdkChunkBlockedError as blocked_exc:
                chunk_blocked = blocked_exc
                ctx.payload = {
                    "match_state": "MATCH_FOUND",
                    "blocked": True,
                    "blocked_target": "retrieved_chunk",
                    "blocked_chunk_id": blocked_exc.chunk.chunk_id,
                    "blocked_doc_name": blocked_exc.chunk.doc_name,
                    "matched_filters": list(blocked_exc.matched_filters),
                    "latency_ms": max(0, blocked_exc.latency_ms),
                    "tokens_screened": blocked_exc.tokens,
                }
            else:
                cites: ResolvedCitations = resolve_citations(
                    outcome.final_text,
                    ledger,
                    allow_empty_abstention=bool(deps.caller_access and deps.caller_access.is_restricted),
                )
                ctx.payload = {
                    "turns": len(outcome.turns), "agent_turn_events": outcome.agent_turn_events,
                    "tool_calls": len(ledger.tool_records), "tool_calls_by_name": _tool_calls_by_name(ledger),
                    **verification_summary(ledger),
                    "final_answer_check": final_answer_check(cites.answer, cites.chunks, ledger),
                    "final_answer_source": FINAL_ANSWER_SOURCE, "final_turn_index": outcome.final_turn_index,
                    "tokens": outcome.tokens().to_dict(), "citations": [c.chunk_id for c in cites.chunks],
                    "tool_chunks_returned": len(ledger.chunks),
                }
        tool_armor_cost = model_armor_cost_usd(retriever._adk_screened_tokens)
        if chunk_blocked is not None:
            await emitter.skipped("synthesize", reason="Halted by Google Cloud Model Armor (MATCH_FOUND on ADK retrieved chunk)")
            await emitter.skipped("guardrail_out", reason="Synthesis skipped due to retrieved-chunk guardrail block")
            filters = ", ".join(chunk_blocked.matched_filters)
            blocked_msg = (
                "Synthesis halted by Google Cloud Model Armor before ADK synthesis "
                f"(filterMatchState=MATCH_FOUND; target=retrieved_chunk; chunk_id={chunk_blocked.chunk.chunk_id}; "
                f"doc={chunk_blocked.chunk.doc_name}; matched_filters={filters})."
            )
            usage = TokenUsage(
                embedding_tokens=ledger.usage.embedding_tokens,
                model=self.synthesis_model.id,
                embedding_model=self._embed_model if ledger.usage.embedding_tokens > 0 else "",
            )
            return EngineResult(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                answer=blocked_msg,
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                cost_usd=round(
                    cost_from_usage(usage, self.synthesis_model.id)
                    + rank_cost_usd(ledger.usage.rank_billed_counts)
                    + in_cost
                    + tool_armor_cost,
                    8,
                ),
                stage_log=self.stage_log_of(emitter),
                usage=usage,
                flags=EngineFlags(
                    reranker_used=ledger.usage.rank_billed_counts > 0,
                    adk_runner_used=True,
                    layout_source="pymupdf",
                ),
            )
        tokens = outcome.tokens()
        final = next(t for t in outcome.turns if t.turn_index == outcome.final_turn_index)
        async with self.stage(
            emitter, "synthesize", label="Final response (ADK agent)",
            service=f"Google ADK · Vertex AI · {self.synthesis_model.label}",
            request={"model": self.synthesis_model.id, "source": FINAL_ANSWER_SOURCE, "final_turn_index": final.turn_index},
        ) as ctx:
            ctx.payload = {
                "model": self.synthesis_model.id, "provider": self.synthesis_model.provider,
                "prompt_tokens": tokens.prompt, "output_tokens": tokens.output, "thinking_tokens": tokens.thinking,
                "turns": len(outcome.turns), "answer": cites.answer,
            }
        blocked_out, final_answer, out_cost = await self._screen_model_output(cites.answer, cleaned, emitter)
        usage = TokenUsage(
            prompt_tokens=tokens.prompt, output_tokens=tokens.output, thinking_tokens=tokens.thinking,
            embedding_tokens=ledger.usage.embedding_tokens, model=self.synthesis_model.id,
            embedding_model=self._embed_model,
        )
        steps = [
            AgentStep(step_number=i, tool_name=r.tool_name, tool_args=r.args, result_summary=r.summary, latency_ms=r.duration_ms)
            for i, r in enumerate(ledger.tool_records, start=1)
        ]
        citations: list[Citation] = (
            [] if (blocked_out or not cites.chunks) else self.chunks_to_citations(list(cites.chunks), max_citations=len(cites.chunks))
        )
        kept_chunks = [] if blocked_out else list(ledger.chunks[:MAX_JUDGE_CHUNKS])
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer=final_answer, citations=citations,
            retrieved_chunks=kept_chunks, agent_steps=steps,
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(
                cost_from_usage(usage, self.synthesis_model.id)
                + rank_cost_usd(ledger.usage.rank_billed_counts)
                + in_cost
                + tool_armor_cost
                + out_cost,
                8,
            ),
            stage_log=self.stage_log_of(emitter), usage=usage,
            flags=EngineFlags(
                reranker_used=ledger.usage.rank_billed_counts > 0,
                adk_runner_used=True,
                layout_source=_layout_source(ledger.chunks) or "pymupdf",
            ),
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
        """Execute ``self.spec`` across all configured query slots with real ``StageEmitter`` telemetry."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("ComposableRAGEngine requires DEMO_MODE=live|record; use build_composable_engine for replay")
        await self._ensure_guardrail_ready(emitter)
        if self.spec.slots.reasoning_mode == "adk_agent":
            if not isinstance(emitter, BaseStageEmitter):
                raise TypeError("ADK agent mode requires a BaseStageEmitter")
            return await self._run_adk_agent(cleaned, solicitation_filter, time.perf_counter(), emitter, caller_access=caller_access)

        start = time.perf_counter()
        prepared, original, _ = await self._run_query_prep(cleaned, emitter)
        if self.spec.index_profile in SCANN_INDEX_PROFILES:
            return await self._run_scann_single_pass(
                cleaned, prepared, original, solicitation_filter, start, emitter, caller_access=caller_access
            )
        if self.spec.index_profile not in BQ_INDEX_PROFILES:
            return await self._run_managed_single_pass(
                cleaned, prepared, original, solicitation_filter, start, emitter, caller_access=caller_access
            )

        genai_client = gcp_clients.get_genai_client(self.settings)
        bq_client = gcp_clients.get_bigquery_client(self.settings)
        cands, emb_tokens = await self._retrieve_bq(
            prepared, original, solicitation_filter, genai_client, bq_client, emitter, caller_access=caller_access
        )
        if not cands and caller_access is not None and caller_access.is_restricted:
            return await self._acl_zero_result(
                cleaned=cleaned, caller_access=caller_access, emb_tokens=emb_tokens,
                base_cost=0.0, layout="pymupdf", start=start, emitter=emitter,
            )
        rank_cost = 0.0
        reranked_flag = False
        if self.spec.slots.reranker == "vertex_ranking_api":
            ranked = await self._rerank(original, cands, emitter)
            top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.spec.slots.top_k]]
            rank_cost = rank_cost_usd(rank_billed_counts(len(cands)))
            reranked_flag = True
        else:
            top = [c.chunk for c in cands[: self.spec.slots.top_k]]
        sources = "+".join(sorted({c.layout_source for c in top if c.layout_source})) or "pymupdf"
        return await self._finish_single_pass_with_guardrail(
            cleaned=cleaned,
            top=top,
            emb_tokens=emb_tokens,
            base_cost=0.0,
            rank_cost=rank_cost,
            reranked_flag=reranked_flag,
            layout=sources,
            start=start,
            emitter=emitter,
        )


def build_composable_engine(
    spec: PipelineSpec,
    settings: ArenaSettings | None = None,
    *,
    models: RunModels | None = None,
) -> BaseRAGEngine:
    """Build a ``ComposableRAGEngine`` in live/record mode, or a preset ``ReplayRAGEngine`` in replay mode."""
    cfg = settings or ArenaSettings()
    if models is not None:
        validate_pipeline_models(spec, models)
    if cfg.demo_mode == "replay":
        preset_id = spec.preset_level_id(cfg)
        if preset_id is not None:
            return ReplayRAGEngine(
                engine_id=preset_id,
                engine_label=spec.summary_label(cfg),
                stages=active_query_stages(spec),
                tier_profile=spec.index_profile,
                settings=cfg,
                models=models,
            )
        return _UnrecordedCustomReplayEngine(spec, cfg)
    return ComposableRAGEngine(spec, settings=cfg, synthesis_model=models.synthesis if models else None)


__all__ = [
    "ComposableRAGEngine",
    "build_composable_engine",
    "run_lexical_query_job",
]
