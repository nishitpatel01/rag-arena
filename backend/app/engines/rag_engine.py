"""Level 6: Managed Vertex AI RAG Engine (``RagCorpus`` + ``:retrieveContexts`` + catalog LLM | ``VertexRagStore``).

Stages (all emitted from inside this engine at the moment they happen — spec P1):
* ``query_prep`` — Level 2 procurement acronym expansion; the prepared query is sent to ``:retrieveContexts``.
* ``retrieve`` — ONE REST call ``projects/<p>/locations/<loc>:retrieveContexts`` against ``RAG_ENGINE_CORPUS_ID`` (or
  for ``upl_<12hex>`` upload scopes, the workspace's isolated ``rag-arena-upl_<12hex>`` ``RagCorpus``; for corpus
  ``solicitation_XX`` filters, scoped via ``ragResources[0].ragFileIds``). Returns observable chunks, distances/scores,
  source URIs, and ``chunk.pageSpan.firstPage`` mapped to PDF viewer citations.
* ``synthesize`` — two honest modes selected by ``synthesis_model``:
  (a) any catalog LLM (Gemini or Claude) → ``synthesize_stage`` over the retrieved ``RagChunk`` context,
  (b) ``vertex-rag-engine-grounded`` → ``genai_client.models.generate_content`` grounded natively with
      ``types.Tool(retrieval=types.Retrieval(vertex_rag_store=...))``, measuring actual Gemini token usage.
* ``evaluate`` — owned by the evaluator over the retrieved chunks.

Fail loud (spec P2): zero local fallback, zero silent corpus fallback. Every URI, resource path, and error redacts
``${GCS_CORPUS_BUCKET}``, ``${UPLOAD_BUCKET}``, ``${GCP_PROJECT_ID}``, ``projects/${GCP_PROJECT_NUMBER}``, and
``${RAG_ENGINE_CORPUS_ID}``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
import time
from typing import Any

from google.genai import types

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token, get_genai_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import (
    PROVIDER_VERTEX_RAG_ENGINE,
    RAG_ENGINE_GROUNDED_MODEL_ID,
    ModelSpec,
    spec_for_settings_model,
)
from app.core.pricing import cost_from_usage
from app.core.rag_engine_client import (
    RagEngineResources,
    VertexRagClient,
    extract_short_corpus_id,
    redact_resource_text,
)
from app.core.telemetry import StageEmitter
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import synthesize_stage
from app.engines.level2_hybrid import ACRONYM_EXPANSIONS, PreparedQuery, prepare_query
from app.engines.vais_search import redact_vais_uri, solicitation_of
from app.ingestion.rag_engine_job import upload_corpus_display_name
from app.ingestion.uploads import check_workspace_id

RETRIEVE_SERVICE = "Vertex AI RAG Engine · retrieveContexts"
LAYOUT_SOURCE = "vertex_ai_rag_engine"
BBOX_SOURCE = "page"
MODEL_INSTRUCTION = (
    "Answer the procurement question using ONLY the retrieved Vertex AI RAG Engine chunks below. Each chunk is "
    "headed by its document and page. Cite the document name and page for every claim. If the chunks do not "
    "contain the answer, say so."
)
_UPLOAD_FILE_PREFIX_RE = re.compile(r"^f_[0-9a-f]{12}-")
_CORPUS_SOL_RE = re.compile(r"^solicitation_[0-9]{2}$")
_MAX_CONTEXTS = 50
_MAX_TEXT_CHARS = 8000


class RagEngineResponseError(ValueError):
    """``:retrieveContexts`` or ``VertexRagStore`` returned an empty, malformed, or cross-workspace payload."""


@dataclass(frozen=True)
class RetrievedContextHit:
    rank: int
    chunk_id: str
    file_id: str
    doc_name: str
    uri: str
    solicitation_id: str
    page_number: int
    last_page: int
    page_source: str
    score: float
    text: str


def _clean_doc_name(raw_name: str) -> str:
    return _UPLOAD_FILE_PREFIX_RE.sub("", raw_name)


def build_rag_client(settings: ArenaSettings) -> VertexRagClient:
    """Bounded-retry REST client inside the query budget."""
    return VertexRagClient(
        project_id=settings.gcp_project_id,
        location=settings.rag_engine_location,
        token_provider=lambda: get_access_token(settings),
        attempt_timeout_seconds=float(settings.rag_engine_timeout_seconds),
        budget_seconds=float(min(settings.rag_engine_timeout_seconds * 2, settings.query_timeout_seconds)),
    )


def build_retrieve_body(
    query: str,
    *,
    corpus_path: str,
    top_k: int,
    rag_file_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """REST v1 ``RetrieveContextsRequest`` body."""
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ValueError("query must be a non-empty string of at most 4000 characters")
    if not isinstance(corpus_path, str) or "/ragCorpora/" not in corpus_path:
        raise ValueError(f"corpus_path must be a full RagCorpus resource path, got {corpus_path!r}")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= _MAX_CONTEXTS:
        raise ValueError(f"top_k must be in [1, {_MAX_CONTEXTS}], got {top_k!r}")
    resource: dict[str, Any] = {"ragCorpus": corpus_path}
    if rag_file_ids:
        resource["ragFileIds"] = list(rag_file_ids)
    return {
        "vertexRagStore": {"ragResources": [resource]},
        "query": {
            "text": query.strip(),
            "ragRetrievalConfig": {"topK": top_k},
        },
    }


def _parse_page_span(chunk_obj: dict[str, Any]) -> tuple[int, int, str]:
    span = chunk_obj.get("pageSpan") if isinstance(chunk_obj.get("pageSpan"), dict) else {}
    first = span.get("firstPage")
    last = span.get("lastPage")
    if isinstance(first, int) and not isinstance(first, bool) and first >= 1:
        end = int(last) if isinstance(last, int) and not isinstance(last, bool) and last >= first else first
        return first, end, "chunk.pageSpan"
    return 1, 1, "document_default_page_1"


def parse_retrieve_response(doc: Any) -> tuple[RetrievedContextHit, ...]:
    """Parse ``RetrieveContextsResponse`` into typed ``RetrievedContextHit`` rows; fail loud on 0 contexts."""
    if not isinstance(doc, dict):
        raise RagEngineResponseError("retrieveContexts response is not a JSON object")
    outer = doc.get("contexts") if isinstance(doc.get("contexts"), dict) else {}
    raw_items = outer.get("contexts") if isinstance(outer.get("contexts"), list) else []
    if not raw_items:
        raise RagEngineResponseError("Vertex AI RAG Engine returned 0 contexts")
    hits: list[RetrievedContextHit] = []
    for rank, item in enumerate(raw_items[:_MAX_CONTEXTS], start=1):
        if not isinstance(item, dict):
            raise RagEngineResponseError(f"context {rank}: item is not an object")
        chunk_obj = item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
        text = str(item.get("text") or chunk_obj.get("text") or "").strip()
        if not text:
            raise RagEngineResponseError(f"context {rank}: empty chunk text")
        uri = str(item.get("sourceUri") or "").strip()
        display = str(item.get("sourceDisplayName") or "").strip()
        raw_file_name = PurePosixPath(uri).name if uri else display
        doc_name = _clean_doc_name(raw_file_name or display)
        if not doc_name:
            raise RagEngineResponseError(f"context {rank}: missing sourceUri and sourceDisplayName")
        sol_id = solicitation_of(uri) if uri else "unknown"
        first_page, last_page, page_source = _parse_page_span(chunk_obj)
        raw_score = item.get("score", item.get("distance", 0.0))
        score = float(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else 0.0
        file_id = str(chunk_obj.get("fileId") or f"f{rank}").strip()
        raw_cid = str(chunk_obj.get("chunkId") or f"c{rank}").strip()
        hits.append(
            RetrievedContextHit(
                rank=rank,
                chunk_id=f"rag_{file_id[:16]}_p{first_page}_{raw_cid[:16]}",
                file_id=file_id,
                doc_name=doc_name,
                uri=uri,
                solicitation_id=sol_id,
                page_number=first_page,
                last_page=last_page,
                page_source=page_source,
                score=round(score, 4),
                text=text[:_MAX_TEXT_CHARS],
            )
        )
    return tuple(hits)


def hits_to_chunks(hits: tuple[RetrievedContextHit, ...]) -> list[DocumentChunk]:
    """Convert retrieved RAG Engine contexts into ``DocumentChunk`` objects for synthesis, PDF drawer, and judge."""
    chunks: list[DocumentChunk] = []
    for h in hits:
        doc_type = "UPLOADED_DOCUMENT" if h.solicitation_id.startswith("upl_") else "UNKNOWN"
        chunks.append(
            DocumentChunk(
                chunk_id=h.chunk_id,
                tier_profile=TIER_PROFILE,
                solicitation_id=h.solicitation_id,
                doc_name=h.doc_name,
                doc_type=doc_type,
                element_type="rag_chunk",
                section_id=None,
                amendment_number=0,
                effective_date=None,
                amends_section=None,
                page_number=h.page_number,
                bbox=BoundingBox(page=h.page_number, x0=0.0, y0=0.0, x1=1.0, y1=1.0),
                content=h.text,
                score=h.score,
                layout_source=LAYOUT_SOURCE,
                bbox_source=BBOX_SOURCE,
            )
        )
    return chunks


def chunks_context(chunks: list[DocumentChunk]) -> str:
    return "\n\n".join(f"[{c.doc_name} · p.{c.page_number} · {c.chunk_id}]\n{c.content}" for c in chunks)


def _redact_all(text: str, settings: ArenaSettings, extra_corpus_id: str = "") -> str:
    pairs = [
        (settings.upload_bucket.strip(), "${UPLOAD_BUCKET}"),
        (settings.gcs_corpus_bucket.strip(), "${GCS_CORPUS_BUCKET}"),
        (settings.rag_engine_corpus_id.strip(), "${RAG_ENGINE_CORPUS_ID}"),
        (extra_corpus_id.strip(), "${UPLOAD_RAG_CORPUS_ID}"),
        (settings.gcp_project_id.strip(), "${GCP_PROJECT_ID}"),
    ]
    out = text
    for raw, placeholder in sorted((p for p in pairs if p[0]), key=lambda item: -len(item[0])):
        out = out.replace(raw, placeholder)
    return redact_resource_text(out, settings.gcp_project_id)


def _resolve_target_corpus(
    client: VertexRagClient, settings: ArenaSettings, solicitation_filter: str | None
) -> tuple[RagEngineResources, tuple[str, ...], str]:
    """Return ``(resources, rag_file_ids, corpus_display_label)`` for corpus or ``upl_<12hex>`` queries."""
    if not solicitation_filter:
        if not settings.rag_engine_corpus_id:
            raise RuntimeError(
                "rag_engine needs RAG_ENGINE_CORPUS_ID (run scripts/provision_rag_engine.py and add it to .env)"
            )
        res = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location, settings.rag_engine_corpus_id)
        return res, (), "ragCorpora/${RAG_ENGINE_CORPUS_ID}"

    if solicitation_filter.startswith("upl_"):
        check_workspace_id(solicitation_filter)
        display_name = upload_corpus_display_name(solicitation_filter)
        parent = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location)
        for item in client.list_corpora(parent.corpora_path):
            if item.get("displayName") == display_name:
                short_id = extract_short_corpus_id(str(item.get("name") or ""))
                return (
                    RagEngineResources(settings.gcp_project_id, settings.rag_engine_location, short_id),
                    (),
                    f"ragCorpora/{display_name}",
                )
        raise RuntimeError(
            f"upload workspace {solicitation_filter} has no Vertex AI RAG Engine corpus '{display_name}'; "
            "run Level 6 upload ingestion first"
        )

    if _CORPUS_SOL_RE.match(solicitation_filter):
        if not settings.rag_engine_corpus_id:
            raise RuntimeError("rag_engine needs RAG_ENGINE_CORPUS_ID in .env")
        res = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location, settings.rag_engine_corpus_id)
        matched_ids: list[str] = []
        needle = f"/{solicitation_filter}_"
        for rf in client.list_files(res.corpus_path):
            uris = (rf.get("gcsSource") or {}).get("uris") or []
            if any(needle in str(u) for u in uris):
                rf_id = str(rf.get("name") or "").rsplit("/", 1)[-1].strip()
                if rf_id:
                    matched_ids.append(rf_id)
        if not matched_ids:
            raise RagEngineResponseError(f"0 RagFiles matched solicitation_filter={solicitation_filter!r}")
        return res, tuple(matched_ids), "ragCorpora/${RAG_ENGINE_CORPUS_ID}"

    raise ValueError(f"invalid solicitation_filter for rag_engine: {solicitation_filter!r}")


TIER_PROFILE = "rag_engine"


class RagEngine(BaseRAGEngine):
    """Level 6: Managed Vertex AI RAG Engine (``RagCorpus`` + ``:retrieveContexts`` + catalog LLM | ``VertexRagStore``)."""

    engine_id: str = "rag_engine"
    engine_label: str = "Level 6: Managed Vertex AI RAG Engine"
    tier_profile: str = TIER_PROFILE
    stages: tuple[str, ...] = ("query_prep", "retrieve", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        client: VertexRagClient | None = None,
    ) -> None:
        super().__init__(settings=settings)
        if synthesis_model is not None and not isinstance(synthesis_model, ModelSpec):
            raise TypeError("synthesis_model must be a ModelSpec or None")
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(
            self.settings, self.settings.gemini_fast_model
        )
        self._client = client

    @property
    def grounded_mode(self) -> bool:
        return self.synthesis_model.provider == PROVIDER_VERTEX_RAG_ENGINE

    async def _query_prep(self, query: str, emitter: StageEmitter) -> PreparedQuery:
        async with self.stage(
            emitter,
            "query_prep",
            label="Query preparation (acronym expansion)",
            service="Engine (local)",
            request={"query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS]},
        ) as ctx:
            prepared = prepare_query(query)
            ctx.payload = {**prepared.payload(), "sent_to": "Vertex AI RAG Engine query.text"}
        return prepared

    async def _retrieve(
        self,
        prepared: PreparedQuery,
        emitter: StageEmitter,
        *,
        solicitation_filter: str | None = None,
    ) -> tuple[tuple[RetrievedContextHit, ...], RagEngineResources, tuple[str, ...], str]:
        client = self._client or build_rag_client(self.settings)
        res, file_ids, corpus_label = await self.blocking(
            _resolve_target_corpus, client, self.settings, solicitation_filter
        )
        body = build_retrieve_body(
            prepared.prepared,
            corpus_path=res.corpus_path,
            top_k=self.settings.rag_engine_top_k,
            rag_file_ids=file_ids,
        )
        shown_body = json.loads(_redact_all(json.dumps(body), self.settings, res.corpus_id))
        endpoint_label = f"locations/{res.location}:retrieveContexts"
        async with self.stage(
            emitter,
            "retrieve",
            label="Vertex AI RAG Engine (:retrieveContexts)",
            service=RETRIEVE_SERVICE,
            request={"method": "POST", "endpoint": endpoint_label, "corpus": corpus_label, "body": shown_body},
        ) as ctx:
            t0 = time.perf_counter()
            doc, attempts = await self.blocking(
                client.request, "POST", f"{res.parent_path}:retrieveContexts", body=body
            )
            api_ms = max(1, int((time.perf_counter() - t0) * 1000))
            hits = parse_retrieve_response(doc)
            if solicitation_filter and solicitation_filter.startswith("upl_"):
                outside = [h.doc_name for h in hits if h.solicitation_id != solicitation_filter]
                if outside:
                    raise RagEngineResponseError(
                        f"Vertex AI RAG Engine returned chunk(s) outside workspace {solicitation_filter}: {outside[:5]}"
                    )
            ctx.payload = {
                "api_request": shown_body,
                "corpus": corpus_label,
                "location": res.location,
                "embedding_model": self.settings.rag_engine_embedding_model,
                "rows_returned": len(hits),
                "api_ms": api_ms,
                "attempts": attempts,
                "retrieval_api_fee_usd": 0.0,
                "query_embedding_tokens": "managed internally by Vertex AI RAG Engine",
                "solicitation_filter": solicitation_filter or "all corpus files",
                "contexts": [
                    {
                        "rank": h.rank,
                        "chunk_id": h.chunk_id,
                        "doc_name": h.doc_name,
                        "solicitation_id": h.solicitation_id,
                        "uri": redact_vais_uri(
                            h.uri,
                            corpus_bucket=self.settings.gcs_corpus_bucket,
                            upload_bucket=self.settings.upload_bucket,
                        ),
                        "page_number": h.page_number,
                        "last_page": h.last_page,
                        "page_source": h.page_source,
                        "score": h.score,
                        "text": h.text[:1200],
                    }
                    for h in hits
                ],
            }
        return hits, res, file_ids, corpus_label

    async def _synthesize_grounded(
        self,
        query: str,
        res: RagEngineResources,
        file_ids: tuple[str, ...],
        corpus_label: str,
        emitter: StageEmitter,
    ) -> tuple[str, TokenUsage, str]:
        base_spec = spec_for_settings_model(self.settings, self.settings.gemini_fast_model)
        thinking = self.settings.gemini_thinking_level.upper()
        rag_res = types.VertexRagStoreRagResource(
            rag_corpus=res.corpus_path,
            rag_file_ids=list(file_ids) if file_ids else None,
        )
        rag_tool = types.Tool(
            retrieval=types.Retrieval(
                vertex_rag_store=types.VertexRagStore(
                    rag_resources=[rag_res],
                    rag_retrieval_config=types.RagRetrievalConfig(top_k=self.settings.rag_engine_top_k),
                )
            )
        )
        gen_cfg = types.GenerateContentConfig(
            system_instruction=MODEL_INSTRUCTION,
            tools=[rag_tool],
            thinking_config=types.ThinkingConfig(thinking_level=thinking),
        )
        req_summary = {
            "model": base_spec.id,
            "pseudo_model": RAG_ENGINE_GROUNDED_MODEL_ID,
            "location": base_spec.location,
            "corpus": corpus_label,
            "top_k": self.settings.rag_engine_top_k,
            "thinking_level": thinking,
            "method": "models.generate_content (VertexRagStore tool)",
        }
        async with self.stage(
            emitter,
            "synthesize",
            label="VertexRagStore grounded synthesis",
            service=f"Vertex AI · {base_spec.label} + VertexRagStore",
            request=req_summary,
        ) as ctx:
            def _call() -> Any:
                genai = get_genai_client(self.settings, location=base_spec.location)
                return genai.models.generate_content(model=base_spec.id, contents=query, config=gen_cfg)

            resp = await self.blocking(_call)
            answer = str(getattr(resp, "text", "") or "").strip()
            if not answer:
                raise ValueError(f"VertexRagStore generate_content ({base_spec.id}) returned an empty answer")
            meta = getattr(resp, "usage_metadata", None)
            if meta is None:
                raise ValueError("VertexRagStore generate_content returned no usage_metadata")
            candidates = getattr(resp, "candidates", None) or []
            cand0 = candidates[0] if candidates else None
            finish = getattr(cand0, "finish_reason", "") if cand0 else ""
            gm = getattr(cand0, "grounding_metadata", None) if cand0 else None
            g_chunks = getattr(gm, "grounding_chunks", None) or []
            usage = TokenUsage(
                prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
                output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
                thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
                embedding_tokens=0,
                model=self.synthesis_model.id,
                embedding_model=self.settings.rag_engine_embedding_model,
            )
            ctx.payload = {
                "model": self.synthesis_model.id,
                "underlying_gemini_model": base_spec.id,
                "provider": self.synthesis_model.provider,
                "corpus": corpus_label,
                "thinking_level": thinking,
                "prompt_tokens": usage.prompt_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
                "grounding_chunks_count": len(g_chunks),
                "stop_reason": str(getattr(finish, "name", finish) or ""),
                "response_id": str(getattr(resp, "response_id", "") or ""),
                "answer": answer,
            }
        return answer, usage, base_spec.id

    async def run_query(
        self, query: str, *, question_id: str | None, solicitation_filter: str | None, emitter: StageEmitter
    ) -> EngineResult:
        """query_prep → :retrieveContexts → (catalog LLM | VertexRagStore grounded Gemini); live/record only."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError("RagEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine")
        start = time.perf_counter()
        prepared = await self._query_prep(cleaned, emitter)
        hits, res, file_ids, corpus_label = await self._retrieve(
            prepared, emitter, solicitation_filter=solicitation_filter
        )
        chunks = hits_to_chunks(hits)
        if self.grounded_mode:
            answer, usage, priced_model_id = await self._synthesize_grounded(
                cleaned, res, file_ids, corpus_label, emitter
            )
        else:
            answer, usage = await synthesize_stage(
                self,
                self.synthesis_model,
                cleaned,
                chunks_context(chunks),
                len(chunks),
                emitter,
                0,
                instruction=MODEL_INSTRUCTION,
                embedding_model=self.settings.rag_engine_embedding_model,
                extra_payload={
                    "context_source": "Vertex AI RAG Engine RetrieveContexts",
                    "context_chunks": len(chunks),
                    "corpus": corpus_label,
                },
            )
            priced_model_id = self.synthesis_model.id
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer,
            citations=self.chunks_to_citations(chunks, max_citations=min(len(chunks), 50)),
            retrieved_chunks=chunks,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=cost_from_usage(usage, priced_model_id),
            stage_log=self.stage_log_of(emitter),
            usage=usage,
            flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
        )


__all__ = [
    "BBOX_SOURCE",
    "LAYOUT_SOURCE",
    "MODEL_INSTRUCTION",
    "RETRIEVE_SERVICE",
    "RagEngine",
    "RagEngineResponseError",
    "RetrievedContextHit",
    "build_rag_client",
    "build_retrieve_body",
    "chunks_context",
    "hits_to_chunks",
    "parse_retrieve_response",
]
