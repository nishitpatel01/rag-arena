"""Abstract base engine contract (spec §6.1) and shared helpers for The RAG Arena v3."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, TypeVar

from app.core.config import ArenaSettings, get_settings
from app.core.models import BoundingBox, Citation, DocumentChunk, EngineResult, StageRecord
from app.core.telemetry import BaseStageEmitter, StageEmitter, StageScope, stage_scope

_MAX_EXCERPT_CHARS = 240
_MAX_QUERY_CHARS = 4000
_T = TypeVar("_T")


StageContext = StageScope


class BaseRAGEngine(ABC):
    """Abstract base class for all progressive RAG engines (spec §6.1).

    Subclasses declare ``engine_id``, ``engine_label``, ``tier_profile`` and ``stages`` (canonical IDs
    in execution order, spec §3) and implement ``run_query`` emitting every stage through the emitter.
    """

    engine_id: str = ""
    engine_label: str = ""
    tier_profile: str = "naive"
    stages: tuple[str, ...] = ()

    def __init__(self, settings: ArenaSettings | None = None) -> None:
        resolved = get_settings() if settings is None else settings
        if not isinstance(resolved, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance or None")
        if not isinstance(self.engine_id, str) or not self.engine_id.strip():
            raise ValueError("engine_id must be a non-empty string on BaseRAGEngine subclass")
        if not isinstance(self.engine_label, str) or not self.engine_label.strip():
            raise ValueError("engine_label must be a non-empty string on BaseRAGEngine subclass")
        if not isinstance(self.stages, tuple) or not self.stages:
            raise ValueError("stages must be a non-empty tuple of canonical stage ids")
        self.settings: ArenaSettings = resolved

    @staticmethod
    def validate_query_inputs(
        query: str,
        solicitation_filter: str | None = None,
    ) -> str:
        """Validate incoming query text and optional solicitation filter at unit boundary."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        cleaned = query.strip()
        if len(cleaned) > _MAX_QUERY_CHARS:
            raise ValueError(f"query exceeds maximum length of {_MAX_QUERY_CHARS} characters")
        if solicitation_filter is not None and not isinstance(solicitation_filter, str):
            raise TypeError("solicitation_filter must be a string or None")
        return cleaned

    @staticmethod
    def chunks_to_citations(
        chunks: list[DocumentChunk],
        max_citations: int = 5,
    ) -> list[Citation]:
        """Convert retrieved DocumentChunks into bounded, verifiable Citation objects."""
        if not isinstance(chunks, list):
            raise TypeError("chunks must be a list of DocumentChunk instances")
        if not isinstance(max_citations, int) or max_citations < 1 or max_citations > 50:
            raise ValueError("max_citations must be an integer between 1 and 50")

        citations: list[Citation] = []
        for chunk in chunks[:max_citations]:
            if not isinstance(chunk, DocumentChunk):
                raise TypeError("Every element in chunks must be a DocumentChunk instance")
            excerpt = chunk.content.strip().replace("\n", " ")[:_MAX_EXCERPT_CHARS]
            if not isinstance(chunk.bbox, BoundingBox):
                raise TypeError(f"chunk {chunk.chunk_id} has no BoundingBox")
            citations.append(
                Citation(
                    chunk_id=chunk.chunk_id,
                    doc_name=chunk.doc_name,
                    solicitation_id=chunk.solicitation_id,
                    page_number=chunk.page_number,
                    section_id=chunk.section_id,
                    element_type=chunk.element_type,
                    bbox=chunk.bbox,
                    excerpt=excerpt,
                    layout_source=chunk.layout_source,
                    bbox_source=chunk.bbox_source,
                )
            )
        return citations

    @asynccontextmanager
    async def stage(
        self,
        emitter: StageEmitter,
        stage_id: str,
        *,
        label: str,
        service: str,
        request: dict[str, Any],
    ) -> AsyncIterator[StageContext]:
        """Emit ``stage_started`` on entry and ``stage_completed``/``stage_failed`` on exit (duration measured
        by the emitter). The body fills ``ctx.payload``; exceptions are re-raised after ``failed`` is emitted."""
        if stage_id not in self.stages:
            raise ValueError(f"stage '{stage_id}' is not declared in {self.engine_id}.stages {self.stages}")
        async with stage_scope(emitter, stage_id, label=label, service=service, request=request) as ctx:
            yield ctx

    @staticmethod
    async def blocking(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a blocking SDK call in a worker thread (spec §6.2: emitter is called around it, not inside)."""
        if not callable(fn):
            raise TypeError("fn must be callable")
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    def stage_log_of(emitter: StageEmitter) -> tuple[StageRecord, ...]:
        """Return the StageRecords accumulated by a BaseStageEmitter (the engine never fabricates records)."""
        if not isinstance(emitter, BaseStageEmitter):
            raise TypeError("emitter must derive from BaseStageEmitter to provide a stage log")
        return emitter.records

    @abstractmethod
    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Execute the RAG pipeline for a query, emitting every canonical stage via ``emitter``."""
