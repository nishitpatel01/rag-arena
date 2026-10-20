"""Level 5: Managed Vertex AI Search (Discovery Engine data store + extractive segments + summary).

v2 implementation — to be rebuilt in Phase 5. The live body runs under a single measured ``legacy`` stage
(see ``app.engines._legacy``); it is disabled by default (``ENABLED_LEVELS``). The v2 local-search
impersonation of the managed answer has been deleted: a failing search call now fails the stage (spec P2).
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import ArenaSettings
from app.core.gcp_clients import _ensure_live_settings, get_search_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.telemetry import StageEmitter
from app.engines._legacy import run_legacy_body
from app.engines.base import BaseRAGEngine


def _execute_vertex_search(
    settings: ArenaSettings,
    data_store_id: str,
    cleaned_query: str,
) -> Any:
    """Execute synchronous Discovery Engine SearchServiceClient.search inside a worker thread."""
    try:
        from google.cloud import discoveryengine_v1 as discoveryengine
    except ImportError as exc:
        raise RuntimeError("google-cloud-discoveryengine is required for live ManagedVertexSearchEngine") from exc

    client = get_search_client(settings)
    serving_config = (
        f"projects/{settings.gcp_project_id}/locations/global/collections/default_collection/"
        f"dataStores/{data_store_id}/servingConfigs/default_search"
    )
    content_spec = discoveryengine.SearchRequest.ContentSearchSpec(
        extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
            max_extractive_segment_count=3,
        ),
        summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
            summary_result_count=5,
            include_citations=True,
            ignore_adversarial_query=True,
            ignore_non_summary_seeking_query=True,
        ),
    )
    request = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query=cleaned_query,
        page_size=5,
        content_search_spec=content_spec,
    )
    return client.search(
        request=request,
        timeout=min(6.0, float(settings.query_timeout_seconds)),
    )


def _segments_to_chunks(response: Any, solicitation_filter: str | None, query: str) -> list[DocumentChunk]:
    """Convert Vertex AI Search extractive segments into DocumentChunks (v2 shape; rebuilt in Phase 5)."""
    chunks: list[DocumentChunk] = []
    for idx, res in enumerate(list(response.results)[:5], start=1):
        doc_struct = dict(res.document.derived_struct_data) if res.document.derived_struct_data else {}
        title = str(doc_struct.get("title") or res.document.id or f"doc_{idx}.pdf")
        segments = doc_struct.get("extractive_segments") or []
        if not segments:
            raise ValueError(f"Vertex AI Search result {idx} ('{title}') returned no extractive_segments")
        seg_text = str(segments[0].get("content", ""))
        page_num = max(1, int(segments[0].get("pageNumber", 1)))
        chunks.append(
            DocumentChunk(
                chunk_id=f"vertex_seg_{idx}",
                tier_profile="hybrid",
                solicitation_id=solicitation_filter or "managed_vertex_store",
                doc_name=title,
                doc_type="BASE_RFP",
                element_type="narrative_text",
                section_id=None,
                amendment_number=0,
                effective_date=None,
                amends_section=None,
                page_number=page_num,
                bbox=BoundingBox(page=page_num, x0=0.0, y0=0.0, x1=1.0, y1=1.0),
                content=seg_text,
                score=0.0,
            )
        )
    return chunks


class ManagedVertexSearchEngine(BaseRAGEngine):
    """Managed baseline: Vertex AI Search with extractive segments and summary (v2 body; rebuilt in Phase 5)."""

    engine_id: str = "managed_vertex"
    engine_label: str = "Level 5: Managed Vertex AI Search"
    tier_profile: str = "hybrid"
    stages: tuple[str, ...] = ("query_prep", "retrieve", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        data_store_id: str = "procurement-corpus-store",
    ) -> None:
        super().__init__(settings=settings)
        if not isinstance(data_store_id, str) or not data_store_id.strip():
            raise ValueError("data_store_id must be a non-empty string")
        self.data_store_id = data_store_id.strip()

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Run the v2 managed-search body under one measured ``legacy`` stage (live/record only)."""
        cleaned_query = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "ManagedVertexSearchEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not in the managed data store; upload ingestion is not available")
        return await run_legacy_body(
            self,
            emitter,
            phase="Phase 5",
            query=cleaned_query,
            body=lambda: self._legacy_live(cleaned_query, solicitation_filter),
        )

    async def _legacy_live(self, cleaned_query: str, solicitation_filter: str | None) -> EngineResult:
        _ensure_live_settings(self.settings)
        start_ts = time.perf_counter()
        response = await self.blocking(_execute_vertex_search, self.settings, self.data_store_id, cleaned_query)
        summary_text = str(getattr(response.summary, "summary_text", "") or "").strip()
        if not summary_text:
            raise ValueError("Vertex AI Search returned no summary_text")
        chunks = _segments_to_chunks(response, solicitation_filter, cleaned_query)
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=summary_text,
            citations=self.chunks_to_citations(chunks, max_citations=5),
            retrieved_chunks=chunks,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start_ts) * 1000)),
            cost_usd=0.0,
            usage=TokenUsage(),
            flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=""),
        )
