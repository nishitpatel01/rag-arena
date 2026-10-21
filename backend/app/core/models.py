"""Immutable data models and schemas for The RAG Arena."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, NamedTuple


class _SerializableMixin:
    """Mixin providing deterministic dictionary serialization methods."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize dataclass instance to a standard Python dictionary."""
        return asdict(self)  # type: ignore[arg-type]

    def model_dump(self) -> dict[str, Any]:
        """Alias for Pydantic v2 compatibility."""
        return self.to_dict()


@dataclass(frozen=True)
class BoundingBox(_SerializableMixin):
    """Normalized page bounding box coordinates in [0.0, 1.0]."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if not isinstance(self.page, int) or self.page < 1:
            raise ValueError(f"BoundingBox page must be an int >= 1, got {self.page}")
        if not (0.0 <= float(self.x0) < float(self.x1) <= 1.0):
            raise ValueError(
                f"BoundingBox x coordinates must satisfy 0.0 <= x0 < x1 <= 1.0, got ({self.x0}, {self.x1})"
            )
        if not (0.0 <= float(self.y0) < float(self.y1) <= 1.0):
            raise ValueError(
                f"BoundingBox y coordinates must satisfy 0.0 <= y0 < y1 <= 1.0, got ({self.y0}, {self.y1})"
            )


@dataclass(frozen=True)
class DocumentChunk(_SerializableMixin):
    """Indexed procurement document chunk with provenance and amendment metadata."""

    chunk_id: str
    tier_profile: str
    solicitation_id: str
    doc_name: str
    doc_type: str
    element_type: str
    section_id: str | None
    amendment_number: int
    effective_date: str | None
    amends_section: str | None
    page_number: int
    bbox: BoundingBox
    content: str
    score: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.chunk_id, str) or not self.chunk_id.strip():
            raise ValueError("chunk_id must be a non-empty string")
        if not isinstance(self.doc_name, str) or not self.doc_name.strip():
            raise ValueError("doc_name must be a non-empty string")
        if not isinstance(self.page_number, int) or self.page_number < 1:
            raise ValueError("page_number must be an integer >= 1")
        if not isinstance(self.amendment_number, int) or self.amendment_number < 0:
            raise ValueError("amendment_number must be an integer >= 0")
        if not isinstance(self.bbox, BoundingBox):
            raise TypeError("bbox must be a BoundingBox instance")


@dataclass(frozen=True)
class AgentStep(_SerializableMixin):
    """Single tool invocation trace in a Level 4 Google ADK agentic workflow."""

    step_number: int
    tool_name: str
    tool_args: dict[str, Any]
    result_summary: str
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.step_number, int) or self.step_number < 1:
            raise ValueError("step_number must be an integer >= 1")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.tool_args, dict):
            raise TypeError("tool_args must be a dictionary")


@dataclass(frozen=True)
class Citation(_SerializableMixin):
    """Verifiable document citation with normalized bounding box."""

    chunk_id: str
    doc_name: str
    solicitation_id: str
    page_number: int
    section_id: str | None
    element_type: str
    bbox: BoundingBox
    excerpt: str

    def __post_init__(self) -> None:
        if not isinstance(self.chunk_id, str) or not self.chunk_id.strip():
            raise ValueError("Citation chunk_id must be a non-empty string")
        if not isinstance(self.doc_name, str) or not self.doc_name.strip():
            raise ValueError("Citation doc_name must be a non-empty string")
        if not isinstance(self.bbox, BoundingBox):
            raise TypeError("Citation bbox must be a BoundingBox instance")


@dataclass(frozen=True)
class EvalMetrics(_SerializableMixin):
    """Triad RAG evaluation metrics in [0.0, 1.0]."""

    groundedness: float
    qa_relevance: float
    context_precision: float

    def __post_init__(self) -> None:
        for field_name in ("groundedness", "qa_relevance", "context_precision"):
            val = float(getattr(self, field_name))
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{field_name} must be in [0.0, 1.0], got {val}")


class EvalOutcome(NamedTuple):
    """Result of one LLM-judge evaluation: metrics plus provenance (spec §6.5). Tuple-compatible."""

    metrics: EvalMetrics
    judge_model: str
    reference_answer_used: bool
    duration_ms: int
    # Judge provenance + measured judge usage (defaults keep pre-Opus recordings loadable).
    judge_provider: str = ""
    judge_location: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    judge_cost_usd: float = 0.0
    rationale: str = ""
    judge_response_id: str = ""


@dataclass(frozen=True)
class TokenUsage(_SerializableMixin):
    """Measured token usage for one engine run, read from SDK ``usage_metadata`` (never estimated)."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    thinking_tokens: int = 0
    model: str = ""
    embedding_model: str = ""

    def __post_init__(self) -> None:
        for field_name in ("prompt_tokens", "output_tokens", "embedding_tokens", "thinking_tokens"):
            val = getattr(self, field_name)
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ValueError(f"{field_name} must be an integer >= 0, got {val!r}")
        if not isinstance(self.model, str) or not isinstance(self.embedding_model, str):
            raise TypeError("model and embedding_model must be strings")


_VALID_LAYOUT_SOURCES: tuple[str, ...] = ("", "pymupdf", "pdf_stream_regex", "documentai_layout", "gemini_vision")


@dataclass(frozen=True)
class EngineFlags(_SerializableMixin):
    """Honest capability flags (spec P5): only true when the named service actually ran."""

    reranker_used: bool = False
    adk_runner_used: bool = False
    layout_source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reranker_used, bool) or not isinstance(self.adk_runner_used, bool):
            raise TypeError("reranker_used and adk_runner_used must be booleans")
        if self.layout_source not in _VALID_LAYOUT_SOURCES:
            raise ValueError(f"layout_source must be one of {_VALID_LAYOUT_SOURCES}, got {self.layout_source!r}")


_VALID_STAGE_STATUSES: tuple[str, ...] = ("completed", "failed", "skipped")


@dataclass(frozen=True)
class StageRecord(_SerializableMixin):
    """One measured pipeline stage: what ran, where, for how long, with the bounded request/payload."""

    stage: str
    label: str
    service: str
    status: str
    started_at: str
    duration_ms: int
    request: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("StageRecord.stage must be a non-empty string")
        if self.status not in _VALID_STAGE_STATUSES:
            raise ValueError(f"StageRecord.status must be one of {_VALID_STAGE_STATUSES}, got {self.status!r}")
        if not isinstance(self.duration_ms, int) or isinstance(self.duration_ms, bool) or self.duration_ms < 0:
            raise ValueError("StageRecord.duration_ms must be an integer >= 0")
        if not isinstance(self.request, dict) or not isinstance(self.payload, dict):
            raise TypeError("StageRecord.request and payload must be dictionaries")


@dataclass(frozen=True)
class EngineResult(_SerializableMixin):
    """Complete execution result for a single RAG engine run. Carries NO evaluation metrics (spec P3)."""

    engine_id: str
    engine_label: str
    answer: str
    citations: list[Citation]
    retrieved_chunks: list[DocumentChunk]
    agent_steps: list[AgentStep]
    latency_ms: int
    cost_usd: float
    stage_log: tuple[StageRecord, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    flags: EngineFlags = field(default_factory=EngineFlags)

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id.strip():
            raise ValueError("engine_id must be a non-empty string")
        if not isinstance(self.answer, str):
            raise TypeError("answer must be a string")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        if self.cost_usd < 0.0:
            raise ValueError("cost_usd must be >= 0.0")
        if not isinstance(self.stage_log, tuple) or not all(isinstance(s, StageRecord) for s in self.stage_log):
            raise TypeError("stage_log must be a tuple of StageRecord instances")
        if not isinstance(self.usage, TokenUsage):
            raise TypeError("usage must be a TokenUsage instance")
        if not isinstance(self.flags, EngineFlags):
            raise TypeError("flags must be an EngineFlags instance")



@dataclass(frozen=True)
class BattleRequest(_SerializableMixin):
    """Incoming request payload for a multi-engine RAG Arena battle."""

    query: str
    question_id: str | None = None
    engines: list[str] | None = field(default=None)
    solicitation_filter: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("BattleRequest query must be a non-empty string")
        if len(self.query) > 4000:
            raise ValueError("BattleRequest query exceeds 4000 character bound")


@dataclass(frozen=True)
class UploadMetadata(_SerializableMixin):
    """Metadata returned after validating a user-uploaded PDF document."""

    session_id: str
    filename: str
    size_bytes: int
    page_count: int
    sha256_hex: str
    expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.size_bytes, int) or self.size_bytes < 1:
            raise ValueError("size_bytes must be an integer >= 1")
        if not isinstance(self.page_count, int) or self.page_count < 1:
            raise ValueError("page_count must be an integer >= 1")
