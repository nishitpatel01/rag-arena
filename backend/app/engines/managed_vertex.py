"""Level 5: Managed Vertex AI Search (Discovery Engine data store + search app; extractive segments; summary).

Stages (spec §3), all emitted from inside this engine at the moment they happen (spec P1):

* ``query_prep`` — Level 2 acronym expansions; the prepared query is what the search call receives.
* ``retrieve`` — ONE REST call ``engines/<engine>/servingConfigs/default_search:search`` with ``extractiveContentSpec``
  (segments + scores) and ``snippetSpec``; when the synthesis model is the pseudo model ``vertex-ai-search-summary``
  the SAME request also carries ``summarySpec`` (the service generates the summary inside this call, so its
  generation time is part of the measured retrieve ``api_ms``). Payload: request body, total_size, results with
  extractive segments (page numbers), attribution token, api_ms.
* ``synthesize`` — two honest modes chosen by the run's synthesis model:
  (a) ``vertex-ai-search-summary`` → the summary returned by the retrieve call (no second call; payload says so),
  (b) any other catalog model → the common synthesize stage over the extractive segments ("buy retrieval, build
  generation").
* ``evaluate`` — owned by the evaluator; the judge context is the extractive segments of the top
  ``VAIS_SUMMARY_RESULT_COUNT`` results (the same documents the summary model reads), in both modes.

Fail loud (spec P2): no local search, no fallback model. Any HTTP error, a result without segments/page numbers, or a
skipped summary → ``stage_failed`` → ``engine_error``. Corpus ``solicitation_XX`` filters are rejected (the corpus data
store holds unstructured PDFs with no filterable metadata); upload workspace scopes (``upl_<12hex>``) target the
dedicated ``VAIS_UPLOADS_ENGINE_ID`` with ``filter: workspace_id: ANY("upl_<12hex>")`` and verify every returned hit.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token
from app.core.models import DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import PROVIDER_VERTEX_AI_SEARCH, ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, vais_query_cost_usd
from app.core.telemetry import StageEmitter
from app.core.vertex_search import DiscoveryEngineClient, VaisResources
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import synthesize_stage
from app.engines.level2_hybrid import ACRONYM_EXPANSIONS, PreparedQuery, prepare_query
from app.engines.vais_search import (
    BBOX_SOURCE,
    LAYOUT_SOURCE,
    SearchOutcome,
    VaisResponseError,
    build_search_body,
    hit_payload,
    parse_search_response,
    redact_vais_uri,
    segment_chunks,
)
from app.ingestion.uploads import check_workspace_id

SEARCH_SERVICE = "Vertex AI Search · search"
SUMMARY_MODEL_ID = "vertex-ai-search-summary"
MODEL_INSTRUCTION = (
    "Answer the question using ONLY the extractive segments below (returned by Vertex AI Search). Each segment is "
    "headed by its document and page. Cite the document name and page for every claim. If the segments do not "
    "contain the answer, say so."
)


def is_upload_scope(solicitation_filter: str | None) -> bool:
    """``False`` for None/empty, ``True`` for a valid ``upl_<12hex>``, ``ValueError`` for anything else."""
    if not solicitation_filter:
        return False
    if solicitation_filter.startswith("upl_"):
        check_workspace_id(solicitation_filter)
        return True
    raise ValueError("managed_vertex does not support corpus solicitation_filter (no filterable metadata in corpus store)")


def workspace_filter_expr(workspace_id: str) -> str:
    return f'workspace_id: ANY("{check_workspace_id(workspace_id)}")'


def _check_resources(settings: ArenaSettings, *, upload_scope: bool = False) -> VaisResources:
    if upload_scope:
        if not settings.vais_uploads_engine_id or not settings.vais_uploads_data_store_id:
            raise RuntimeError(
                "managed_vertex upload search needs VAIS_UPLOADS_DATA_STORE_ID and VAIS_UPLOADS_ENGINE_ID "
                "(run scripts/provision_vertex_search.py and add the printed short ids to the git-ignored .env)"
            )
        return VaisResources(
            settings.gcp_project_id, settings.vais_location,
            settings.vais_uploads_data_store_id, settings.vais_uploads_engine_id,
        )
    if not settings.vais_engine_id or not settings.vais_data_store_id:
        raise RuntimeError(
            "managed_vertex needs VAIS_DATA_STORE_ID and VAIS_ENGINE_ID (run scripts/provision_vertex_search.py and add "
            "the printed short ids to the git-ignored .env)"
        )
    return VaisResources(settings.gcp_project_id, settings.vais_location, settings.vais_data_store_id, settings.vais_engine_id)


def build_search_client(settings: ArenaSettings) -> DiscoveryEngineClient:
    """Bounded-retry REST client inside the query budget (attempt ≤ VAIS_TIMEOUT_SECONDS)."""
    return DiscoveryEngineClient(
        project_id=settings.gcp_project_id, location=settings.vais_location,
        token_provider=lambda: get_access_token(settings), attempt_timeout_seconds=float(settings.vais_timeout_seconds),
        budget_seconds=float(min(settings.vais_timeout_seconds * 2, settings.query_timeout_seconds)),
    )


def segments_context(chunks: list[DocumentChunk]) -> str:
    """Prompt context: ``[doc · p.N · segment id]`` header + segment text, in result order."""
    return "\n\n".join(f"[{c.doc_name} · p.{c.page_number} · {c.chunk_id}]\n{c.content}" for c in chunks)


class ManagedVertexSearchEngine(BaseRAGEngine):
    """Build vs. Buy baseline: Vertex AI Search retrieval + (managed summary | catalog model synthesis)."""

    engine_id: str = "managed_vertex"
    engine_label: str = "Level 5: Managed Vertex AI Search"
    tier_profile: str = "vertex_ai_search"
    stages: tuple[str, ...] = ("query_prep", "retrieve", "synthesize", "evaluate")

    def __init__(
        self, settings: ArenaSettings | None = None, *, synthesis_model: ModelSpec | None = None,
        client: DiscoveryEngineClient | None = None,
    ) -> None:
        super().__init__(settings=settings)
        if synthesis_model is not None and not isinstance(synthesis_model, ModelSpec):
            raise TypeError("synthesis_model must be a ModelSpec or None")
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(self.settings, self.settings.gemini_fast_model)
        self._client = client

    @property
    def summary_mode(self) -> bool:
        return self.synthesis_model.provider == PROVIDER_VERTEX_AI_SEARCH

    async def _query_prep(self, query: str, emitter: StageEmitter) -> PreparedQuery:
        async with self.stage(
            emitter, "query_prep", label="Query preparation (acronym expansion)", service="Engine (local)",
            request={"query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS]},
        ) as ctx:
            prepared = prepare_query(query)
            ctx.payload = {**prepared.payload(), "sent_to": "Vertex AI Search search.query"}
        return prepared

    def _search_body(self, prepared: str, *, solicitation_filter: str | None = None) -> dict[str, Any]:
        s = self.settings
        return build_search_body(
            prepared, page_size=s.vais_page_size, max_segments=s.vais_max_segments,
            summary_result_count=min(s.vais_summary_result_count, s.vais_page_size) if self.summary_mode else None,
            summary_model_version=s.vais_summary_model_version,
            filter_expr=workspace_filter_expr(solicitation_filter) if solicitation_filter else None,
        )

    def _retrieve_payload(
        self, body: dict[str, Any], outcome: SearchOutcome, api_ms: int, attempts: int, res: VaisResources,
        *, solicitation_filter: str | None = None,
    ) -> dict[str, Any]:
        return {
            "api_request": body, "serving_config": res.serving_config_label, "location": res.location,
            "total_size": outcome.total_size, "rows_returned": len(outcome.hits),
            "results": [
                hit_payload(h, self.settings.gcs_corpus_bucket, upload_bucket=self.settings.upload_bucket)
                for h in outcome.hits
            ],
            "segments_returned": sum(len(h.segments) for h in outcome.hits), "attribution_token": outcome.attribution_token,
            "api_ms": api_ms, "attempts": attempts, "summary_requested": self.summary_mode,
            "solicitation_filter": solicitation_filter or "not supported (unstructured data store without filterable metadata)",
        }

    async def _retrieve(
        self, prepared: PreparedQuery, emitter: StageEmitter, *, solicitation_filter: str | None = None
    ) -> SearchOutcome:
        upload_scope = is_upload_scope(solicitation_filter)
        res = _check_resources(self.settings, upload_scope=upload_scope)
        body = self._search_body(prepared.prepared, solicitation_filter=solicitation_filter)
        client = self._client or build_search_client(self.settings)
        service = SEARCH_SERVICE + (" + summary" if self.summary_mode else "")
        async with self.stage(
            emitter, "retrieve", label="Vertex AI Search (extractive segments)", service=service,
            request={"method": "POST", "endpoint": f"{res.serving_config_label}:search", "body": body},
        ) as ctx:
            t0 = time.perf_counter()
            doc, attempts = await self.blocking(client.request, "POST", f"{res.serving_config_path}:search", body=body)
            api_ms = max(1, int((time.perf_counter() - t0) * 1000))
            outcome = parse_search_response(doc, summary_requested=self.summary_mode)
            if upload_scope:
                outside = [h.document_id for h in outcome.hits if h.solicitation_id != solicitation_filter]
                if outside:
                    raise VaisResponseError(
                        f"Vertex AI Search returned document(s) outside workspace {solicitation_filter}: {outside[:5]}"
                    )
            ctx.payload = self._retrieve_payload(
                body, outcome, api_ms, attempts, res, solicitation_filter=solicitation_filter
            )
        return outcome

    def _context_hits(self, outcome: SearchOutcome) -> tuple:
        return outcome.hits[: min(self.settings.vais_summary_result_count, len(outcome.hits))]

    async def _synthesize_summary(self, outcome: SearchOutcome, emitter: StageEmitter) -> tuple[str, TokenUsage, list[DocumentChunk], str]:
        summary = outcome.summary
        spec = self.synthesis_model
        cited_hits = tuple(h for h in outcome.hits if h.rank in set(summary.cited_ranks)) if summary else ()
        async with self.stage(
            emitter, "synthesize", label="Vertex AI Search summary", service=f"Vertex AI Search · summary ({spec.label})",
            request={"source": "summarySpec on the retrieve search call (same request, no second call)",
                     "summarySpec": self._search_body("q")["contentSearchSpec"]["summarySpec"]},
        ) as ctx:
            if summary is None:
                raise RuntimeError("summary mode but the retrieve call returned no summary")
            cites = segment_chunks(cited_hits) if cited_hits else segment_chunks(self._context_hits(outcome))
            source = "summary_citations" if cited_hits else "summary_input_results (no citation markers returned)"
            refs = [
                {
                    **r,
                    "uri": redact_vais_uri(
                        r.get("uri", ""),
                        corpus_bucket=self.settings.gcs_corpus_bucket,
                        upload_bucket=self.settings.upload_bucket,
                    ),
                }
                for r in summary.references
            ]
            ctx.payload = {
                "model": spec.id, "provider": spec.provider, "answer": summary.text, "generated_in_stage": "retrieve",
                "model_version_requested": self.settings.vais_summary_model_version,
                "model_version_used": "not returned by the API", "summary_skipped_reasons": list(summary.skipped_reasons),
                "safety_attributes": summary.safety_attributes, "cited_result_ranks": list(summary.cited_ranks),
                "citation_metadata": list(summary.citation_metadata), "references": refs,
                "citation_source": source, "prompt_tokens": 0, "output_tokens": 0,
            }
        return summary.text, TokenUsage(model=spec.id), cites, source

    async def _synthesize_model(self, query: str, outcome: SearchOutcome, emitter: StageEmitter) -> tuple[str, TokenUsage, list[DocumentChunk], str]:
        chunks = segment_chunks(self._context_hits(outcome))
        answer, usage = await synthesize_stage(
            self, self.synthesis_model, query, segments_context(chunks), len(chunks), emitter, 0,
            instruction=MODEL_INSTRUCTION,
            extra_payload={"context_source": "Vertex AI Search extractive segments", "context_segments": len(chunks),
                           "context_results": len(self._context_hits(outcome))},
        )
        return answer, usage, chunks, "context_segments"

    def _cost(self, usage: TokenUsage) -> float:
        search = vais_query_cost_usd(searches=1, summary_queries=1 if self.summary_mode else 0)
        return round(search + (0.0 if self.summary_mode else cost_from_usage(usage, self.synthesis_model.id)), 8)

    async def run_query(
        self, query: str, *, question_id: str | None, solicitation_filter: str | None, emitter: StageEmitter
    ) -> EngineResult:
        """prep → search (segments [+ summary]) → summary | model synthesis; live/record only."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("ManagedVertexSearchEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        is_upload_scope(solicitation_filter)
        start = time.perf_counter()
        prepared = await self._query_prep(cleaned, emitter)
        outcome = await self._retrieve(prepared, emitter, solicitation_filter=solicitation_filter)
        if self.summary_mode:
            answer, usage, cites, _ = await self._synthesize_summary(outcome, emitter)
        else:
            answer, usage, cites, _ = await self._synthesize_model(cleaned, outcome, emitter)
        context = segment_chunks(self._context_hits(outcome))
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer=answer,
            citations=self.chunks_to_citations(cites, max_citations=min(len(cites), 50)), retrieved_chunks=context,
            agent_steps=[], latency_ms=max(1, int((time.perf_counter() - start) * 1000)), cost_usd=self._cost(usage),
            stage_log=self.stage_log_of(emitter), usage=usage,
            flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
        )


__all__ = [
    "BBOX_SOURCE",
    "MODEL_INSTRUCTION",
    "ManagedVertexSearchEngine",
    "SUMMARY_MODEL_ID",
    "build_search_client",
    "is_upload_scope",
    "segments_context",
    "workspace_filter_expr",
]
