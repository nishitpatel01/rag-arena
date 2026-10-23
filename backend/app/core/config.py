"""Immutable runtime configuration for The RAG Arena."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_GEMINI_FAST_MODEL: str = "gemini-4-argon"
DEFAULT_GEMINI_PRO_MODEL: str = "gemini-4-argon"
DEFAULT_GEMINI_THINKING_LEVEL: str = "high"
DEFAULT_EMBEDDING_MODEL: str = "gemini-embedding-2"
# LLM-as-a-judge: owner decision 2026-10-21 — the judge must be a different model than synthesis (Claude Opus 5.5).
JUDGE_PROVIDERS: tuple[str, ...] = ("anthropic_vertex", "gemini")
DEFAULT_JUDGE_PROVIDER: str = "anthropic_vertex"
DEFAULT_JUDGE_MODEL: str = "claude-opus-5-5"
DEFAULT_JUDGE_LOCATION: str = "global"
DEFAULT_JUDGE_MAX_TOKENS: int = 1024
DEFAULT_JUDGE_TIMEOUT_SECONDS: int = 60
_JUDGE_MAX_TOKENS_RANGE: tuple[int, int] = (64, 8192)
_JUDGE_TIMEOUT_RANGE: tuple[int, int] = (5, 120)
# Synthesis via a selectable catalog model (app.core.models_catalog); Claude rawPredict needs an explicit max_tokens.
DEFAULT_SYNTHESIS_MAX_TOKENS: int = 4096
DEFAULT_SYNTHESIS_TIMEOUT_SECONDS: int = 90
_SYNTHESIS_MAX_TOKENS_RANGE: tuple[int, int] = (256, 32768)
_SYNTHESIS_TIMEOUT_RANGE: tuple[int, int] = (5, 120)
MODEL_LOCATION_ENV_PREFIX: str = "MODEL_LOCATION_"
_MAX_MODEL_LOCATION_OVERRIDES: int = 32
_JUDGE_MODEL_PREFIX: dict[str, str] = {"anthropic_vertex": "claude-", "gemini": "gemini-"}
# Publisher model ids: lowercase letters, digits, dots, dashes, underscores, optional @version (≤ 128 chars).
_MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,110}(?:@[a-z0-9._-]{1,16})?")

_VALID_THINKING_LEVELS: tuple[str, ...] = ("low", "medium", "high")
_FORBIDDEN_MODEL_SUBSTRINGS: tuple[str, ...] = (
    "1.5",
    "2.0",
    "2.5",
    "3.1",
    "text-embedding-004",
    "gemini-embedding-001",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

ALL_LEVEL_IDS: tuple[str, ...] = (
    "level1_naive",
    "level2_hybrid",
    "level3_multimodal",
    "level4_agentic",
    "managed_vertex",
)
VALID_DEMO_MODES: tuple[str, ...] = ("live", "record", "replay")
# GCP project ids, bucket names, and locations: lowercase letters, digits, dots, dashes, underscores (≤ 128 chars).
_GCP_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
DEFAULT_ENABLED_LEVELS: tuple[str, ...] = ("level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex")
DEFAULT_MAX_SSE_FRAMES: int = 600
DEFAULT_INGEST_JOB_TIMEOUT_SECONDS: int = 1800
# Level 2 hybrid retrieval + Ranking API bounds (env: HYBRID_CANDIDATE_K, RERANK_TOP_N, RANKING_TIMEOUT_SECONDS).
DEFAULT_HYBRID_CANDIDATE_K: int = 50
DEFAULT_RERANK_TOP_N: int = 5
DEFAULT_RANKING_TIMEOUT_SECONDS: int = 30
_HYBRID_CANDIDATE_K_RANGE: tuple[int, int] = (5, 100)
_RERANK_TOP_N_RANGE: tuple[int, int] = (1, 20)
_RANKING_TIMEOUT_RANGE: tuple[int, int] = (2, 120)
# Level 4 ADK agent bounds (env: AGENT_MAX_TURNS, AGENT_MAX_TOOL_CALLS_PER_TURN).
DEFAULT_AGENT_MAX_TURNS: int = 8
DEFAULT_AGENT_MAX_TOOL_CALLS_PER_TURN: int = 8
# Per-turn thinking level of the Level 4 agent (env: AGENT_THINKING_LEVEL). Lower than synthesis by default: an agent
# run makes several model calls and must finish inside QUERY_TIMEOUT_SECONDS (≤ 120 s); measured with HIGH, a 4-turn
# Argon run spent 56 s in one turn and exceeded the budget.
DEFAULT_AGENT_THINKING_LEVEL: str = "low"
# Per model call HTTP timeout of the agent (env: AGENT_MODEL_CALL_TIMEOUT_SECONDS); a timed-out call is retried once by
# google-genai (bounded). Measured 2026-10-22: single Flash turns on the global endpoint stalled 74–110 s for ~50
# output tokens while healthy turns took 3–20 s.
DEFAULT_AGENT_MODEL_CALL_TIMEOUT_SECONDS: int = 45
_AGENT_MODEL_CALL_TIMEOUT_RANGE: tuple[int, int] = (5, 120)
_AGENT_MAX_TURNS_RANGE: tuple[int, int] = (1, 16)
_AGENT_MAX_TOOL_CALLS_RANGE: tuple[int, int] = (1, 8)
# Level 3 Document AI Layout Parser + Gemini vision transcription.
DEFAULT_DOCAI_LOCATION: str = "us"
DEFAULT_DOCAI_TIMEOUT_SECONDS: int = 120
_DOCAI_TIMEOUT_RANGE: tuple[int, int] = (10, 600)
_DOCAI_LOCATION_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
_DOCAI_PROCESSOR_ID_RE = re.compile(r"[a-z0-9]{1,64}")
_DOCAI_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
# Level 5 Managed Vertex AI Search (Discovery Engine REST v1). Ids are env-only (VAIS_DATA_STORE_ID / VAIS_ENGINE_ID).
DEFAULT_VAIS_LOCATION: str = "global"
DEFAULT_VAIS_PAGE_SIZE: int = 10
DEFAULT_VAIS_MAX_SEGMENTS: int = 3
DEFAULT_VAIS_SUMMARY_RESULT_COUNT: int = 5
DEFAULT_VAIS_TIMEOUT_SECONDS: int = 30
DEFAULT_VAIS_SUMMARY_MODEL_VERSION: str = "stable"
_VAIS_PAGE_SIZE_RANGE: tuple[int, int] = (1, 20)
_VAIS_MAX_SEGMENTS_RANGE: tuple[int, int] = (1, 10)
_VAIS_SUMMARY_RESULT_COUNT_RANGE: tuple[int, int] = (1, 10)
_VAIS_TIMEOUT_RANGE: tuple[int, int] = (2, 120)
_VAIS_LOCATION_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
# Discovery Engine resource ids: lowercase letters, digits, dashes, underscores (≤ 63 chars).
_VAIS_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_VAIS_MODEL_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9._/@-]{0,80}")


def _load_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    """Parse key=value pairs from a local .env file with bounded line scanning."""
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    lines = dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for raw_line in lines[:200]:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        k = key.strip()
        v = val.strip().strip('"').strip("'")
        if k:
            parsed[k] = v
    return parsed


def _validate_gemini_model_name(model_name: str, field_name: str) -> None:
    """Validate that a Gemini model identifier is non-empty and not deprecated."""
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    for forbidden in _FORBIDDEN_MODEL_SUBSTRINGS:
        if forbidden in model_name:
            raise ValueError(
                f"Deprecated or forbidden Gemini model '{model_name}' in {field_name}; "
                "configure an active latest Gemini model via GEMINI_FAST_MODEL / GEMINI_PRO_MODEL"
            )


@dataclass(frozen=True)
class ArenaSettings:
    """Immutable configuration settings for The RAG Arena backend."""

    demo_mode: str = "replay"
    gcp_project_id: str = ""
    gcp_location: str = "us-central1"
    vertex_genai_location: str = "global"
    gcs_corpus_bucket: str = ""
    cloudsdk_config: str = ""
    google_application_credentials: str = ""
    bq_dataset: str = "rag_arena"
    bq_table: str = "document_chunks"
    vvs2_collection: str = "procurement-chunks-v2"
    alloydb_table: str = "document_chunks"
    alloydb_dsn: str = ""
    gemini_fast_model: str = DEFAULT_GEMINI_FAST_MODEL
    gemini_pro_model: str = DEFAULT_GEMINI_PRO_MODEL
    gemini_thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL
    text_embedding_model: str = DEFAULT_EMBEDDING_MODEL
    multimodal_embedding_model: str = DEFAULT_EMBEDDING_MODEL
    reranker_model: str = "semantic-ranker-default@latest"
    query_timeout_seconds: int = 90
    max_upload_bytes: int = 10 * 1024 * 1024
    max_upload_pages: int = 30
    enabled_levels: tuple[str, ...] = DEFAULT_ENABLED_LEVELS
    replay_speed: float = 1.0
    max_sse_frames: int = DEFAULT_MAX_SSE_FRAMES
    ingest_job_timeout_seconds: int = DEFAULT_INGEST_JOB_TIMEOUT_SECONDS
    allow_dev_fixtures: bool = False
    recordings_dir: str = ""
    app_version: str = "3.0.0"
    static_dir: str = ""  # built SPA served with history fallback (container: /app/frontend/dist); empty = frontend/dist if built
    judge_provider: str = DEFAULT_JUDGE_PROVIDER
    judge_model: str = DEFAULT_JUDGE_MODEL
    judge_location: str = DEFAULT_JUDGE_LOCATION
    judge_max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS
    judge_timeout_seconds: int = DEFAULT_JUDGE_TIMEOUT_SECONDS
    synthesis_max_tokens: int = DEFAULT_SYNTHESIS_MAX_TOKENS
    synthesis_timeout_seconds: int = DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
    model_verification_file: str = ""
    # ``MODEL_LOCATION_<ID>`` overrides as (env suffix, location) pairs, e.g. ("CLAUDE_OPUS_5_5", "global").
    model_locations: tuple[tuple[str, str], ...] = ()
    hybrid_candidate_k: int = DEFAULT_HYBRID_CANDIDATE_K
    rerank_top_n: int = DEFAULT_RERANK_TOP_N
    ranking_timeout_seconds: int = DEFAULT_RANKING_TIMEOUT_SECONDS
    # Level 3 (env: DOCAI_LOCATION, DOCAI_PROCESSOR_ID, DOCAI_PROCESSOR_VERSION, DOCAI_TIMEOUT_SECONDS, VISION_MODEL).
    docai_location: str = DEFAULT_DOCAI_LOCATION
    docai_processor_id: str = ""
    docai_processor_version: str = ""
    docai_timeout_seconds: int = DEFAULT_DOCAI_TIMEOUT_SECONDS
    vision_model: str = DEFAULT_GEMINI_FAST_MODEL
    agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS
    agent_max_tool_calls_per_turn: int = DEFAULT_AGENT_MAX_TOOL_CALLS_PER_TURN
    agent_thinking_level: str = DEFAULT_AGENT_THINKING_LEVEL
    agent_model_call_timeout_seconds: int = DEFAULT_AGENT_MODEL_CALL_TIMEOUT_SECONDS
    # Level 5 (env: VAIS_LOCATION, VAIS_DATA_STORE_ID, VAIS_ENGINE_ID, VAIS_PAGE_SIZE, VAIS_MAX_SEGMENTS,
    # VAIS_SUMMARY_RESULT_COUNT, VAIS_TIMEOUT_SECONDS, VAIS_SUMMARY_MODEL_VERSION).
    vais_location: str = DEFAULT_VAIS_LOCATION
    vais_data_store_id: str = ""
    vais_engine_id: str = ""
    vais_page_size: int = DEFAULT_VAIS_PAGE_SIZE
    vais_max_segments: int = DEFAULT_VAIS_MAX_SEGMENTS
    vais_summary_result_count: int = DEFAULT_VAIS_SUMMARY_RESULT_COUNT
    vais_timeout_seconds: int = DEFAULT_VAIS_TIMEOUT_SECONDS
    vais_summary_model_version: str = DEFAULT_VAIS_SUMMARY_MODEL_VERSION

    @property
    def model_verification_path(self) -> Path:
        """``data/model_verification.json`` written by ``scripts/verify_models.py`` (override: MODEL_VERIFICATION_FILE)."""
        if self.model_verification_file.strip():
            return Path(self.model_verification_file.strip())
        return _PROJECT_ROOT / "data" / "model_verification.json"
    @property
    def gemini_reasoning_model(self) -> str:
        """Alias for the complex reasoning / agentic model (gemini_pro_model)."""
        return self.gemini_pro_model

    @property
    def is_live(self) -> bool:
        """True when real GCP calls are made (``live`` and ``record`` modes)."""
        return self.demo_mode in ("live", "record")

    @property
    def recordings_path(self) -> Path:
        """Directory holding ``<engine>/<question_id>.json`` recordings (spec §6.4)."""
        if self.recordings_dir.strip():
            return Path(self.recordings_dir.strip())
        return _PROJECT_ROOT / "data" / "recordings"

    def _validate_v3_gates(self) -> None:
        if not isinstance(self.enabled_levels, tuple) or not self.enabled_levels:
            raise ValueError("ENABLED_LEVELS must list at least one level id")
        unknown = [lvl for lvl in self.enabled_levels if lvl not in ALL_LEVEL_IDS]
        if unknown:
            raise ValueError(f"ENABLED_LEVELS contains unknown or parked level ids {unknown}; valid: {ALL_LEVEL_IDS}")
        if len(set(self.enabled_levels)) != len(self.enabled_levels):
            raise ValueError("ENABLED_LEVELS must not contain duplicates")
        if not isinstance(self.replay_speed, float) or not (0.0 < self.replay_speed <= 100.0):
            raise ValueError("REPLAY_SPEED must be a float in (0, 100]")
        if not isinstance(self.max_sse_frames, int) or not (10 <= self.max_sse_frames <= 5000):
            raise ValueError("MAX_SSE_FRAMES must be an integer between 10 and 5000")
        if not isinstance(self.ingest_job_timeout_seconds, int) or not (
            10 <= self.ingest_job_timeout_seconds <= 7200
        ):
            raise ValueError("INGEST_JOB_TIMEOUT_SECONDS must be an integer between 10 and 7200")
        if not isinstance(self.allow_dev_fixtures, bool):
            raise TypeError("allow_dev_fixtures must be a boolean")

    def __post_init__(self) -> None:
        if self.demo_mode not in VALID_DEMO_MODES:
            raise ValueError(f"Invalid demo_mode '{self.demo_mode}'; must be one of {VALID_DEMO_MODES}")
        self._validate_gcp_identity()
        self._validate_v3_gates()
        self._validate_judge()
        self._validate_synthesis()
        self._validate_hybrid()
        self._validate_level3()
        self._validate_agent()
        self._validate_vais()

    def _validate_vais(self) -> None:
        """Level 5 Vertex AI Search location/ids (empty id = not provisioned) and request bounds."""
        if not isinstance(self.vais_location, str) or not _VAIS_LOCATION_RE.fullmatch(self.vais_location):
            raise ValueError(f"VAIS_LOCATION is not a valid Discovery Engine location: {self.vais_location!r}")
        for name, value in (("VAIS_DATA_STORE_ID", self.vais_data_store_id), ("VAIS_ENGINE_ID", self.vais_engine_id)):
            if not isinstance(value, str) or (value and not _VAIS_ID_RE.fullmatch(value)):
                raise ValueError(f"{name} must be a short Discovery Engine id (not a resource name), got {value!r}")
        for name, value, (low, high) in (
            ("VAIS_PAGE_SIZE", self.vais_page_size, _VAIS_PAGE_SIZE_RANGE),
            ("VAIS_MAX_SEGMENTS", self.vais_max_segments, _VAIS_MAX_SEGMENTS_RANGE),
            ("VAIS_SUMMARY_RESULT_COUNT", self.vais_summary_result_count, _VAIS_SUMMARY_RESULT_COUNT_RANGE),
            ("VAIS_TIMEOUT_SECONDS", self.vais_timeout_seconds, _VAIS_TIMEOUT_RANGE),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}")
        if not _VAIS_MODEL_VERSION_RE.fullmatch(self.vais_summary_model_version or ""):
            raise ValueError(f"VAIS_SUMMARY_MODEL_VERSION is invalid: {self.vais_summary_model_version!r}")

    def _validate_agent(self) -> None:
        """Level 4 ADK bounds: max model turns per run and max function calls per model turn."""
        for name, value, (low, high) in (
            ("AGENT_MAX_TURNS", self.agent_max_turns, _AGENT_MAX_TURNS_RANGE),
            ("AGENT_MAX_TOOL_CALLS_PER_TURN", self.agent_max_tool_calls_per_turn, _AGENT_MAX_TOOL_CALLS_RANGE),
            ("AGENT_MODEL_CALL_TIMEOUT_SECONDS", self.agent_model_call_timeout_seconds, _AGENT_MODEL_CALL_TIMEOUT_RANGE),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}")
        if self.agent_thinking_level not in _VALID_THINKING_LEVELS:
            raise ValueError(f"AGENT_THINKING_LEVEL must be one of {_VALID_THINKING_LEVELS}, got {self.agent_thinking_level!r}")

    def _validate_level3(self) -> None:
        """Document AI location/processor/version identifiers and the Gemini vision model (empty processor id = unset)."""
        if not isinstance(self.docai_location, str) or not _DOCAI_LOCATION_RE.fullmatch(self.docai_location):
            raise ValueError(f"DOCAI_LOCATION is not a valid Document AI location: {self.docai_location!r}")
        if self.docai_processor_id and not _DOCAI_PROCESSOR_ID_RE.fullmatch(self.docai_processor_id):
            raise ValueError("DOCAI_PROCESSOR_ID must be the short processor id (lowercase letters/digits, ≤ 64 chars)")
        if self.docai_processor_version and not _DOCAI_VERSION_RE.fullmatch(self.docai_processor_version):
            raise ValueError(f"DOCAI_PROCESSOR_VERSION is not a valid processor version id: {self.docai_processor_version!r}")
        low, high = _DOCAI_TIMEOUT_RANGE
        if not isinstance(self.docai_timeout_seconds, int) or isinstance(self.docai_timeout_seconds, bool) or not (
            low <= self.docai_timeout_seconds <= high
        ):
            raise ValueError(f"DOCAI_TIMEOUT_SECONDS must be an integer between {low} and {high}")
        _validate_gemini_model_name(self.vision_model, "vision_model")
        if not self.vision_model.startswith("gemini-"):
            raise ValueError(f"VISION_MODEL must be a gemini-* model with image input, got {self.vision_model!r}")

    def _validate_hybrid(self) -> None:
        """Bounds for Level 2 RRF candidates, reranked context size and the Ranking API wall-clock budget."""
        for name, value, (low, high) in (
            ("HYBRID_CANDIDATE_K", self.hybrid_candidate_k, _HYBRID_CANDIDATE_K_RANGE),
            ("RERANK_TOP_N", self.rerank_top_n, _RERANK_TOP_N_RANGE),
            ("RANKING_TIMEOUT_SECONDS", self.ranking_timeout_seconds, _RANKING_TIMEOUT_RANGE),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}")
        if self.agent_thinking_level not in _VALID_THINKING_LEVELS:
            raise ValueError(f"AGENT_THINKING_LEVEL must be one of {_VALID_THINKING_LEVELS}, got {self.agent_thinking_level!r}")
        if self.rerank_top_n > self.hybrid_candidate_k:
            raise ValueError("RERANK_TOP_N must not exceed HYBRID_CANDIDATE_K")

    def _validate_synthesis(self) -> None:
        """Bounds for selectable-model synthesis and the MODEL_LOCATION_<ID> overrides."""
        for name, value, (low, high) in (
            ("SYNTHESIS_MAX_TOKENS", self.synthesis_max_tokens, _SYNTHESIS_MAX_TOKENS_RANGE),
            ("SYNTHESIS_TIMEOUT_SECONDS", self.synthesis_timeout_seconds, _SYNTHESIS_TIMEOUT_RANGE),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}")
        if self.agent_thinking_level not in _VALID_THINKING_LEVELS:
            raise ValueError(f"AGENT_THINKING_LEVEL must be one of {_VALID_THINKING_LEVELS}, got {self.agent_thinking_level!r}")
        if not isinstance(self.model_locations, tuple) or len(self.model_locations) > _MAX_MODEL_LOCATION_OVERRIDES:
            raise ValueError(f"model_locations must be a tuple of at most {_MAX_MODEL_LOCATION_OVERRIDES} pairs")
        for pair in self.model_locations:
            if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
                raise TypeError(f"model_locations entries must be (suffix, location) string pairs, got {pair!r}")
            if not re.fullmatch(r"[A-Z0-9_]{1,80}", pair[0]) or not _GCP_IDENTIFIER_RE.fullmatch(pair[1]):
                raise ValueError(f"invalid {MODEL_LOCATION_ENV_PREFIX}{pair[0]}={pair[1]!r}")
    @property
    def synthesis_models(self) -> tuple[str, ...]:
        """Every model that can write an answer (generation + reasoning/agentic); the judge must be none of them."""
        return (self.gemini_fast_model, self.gemini_pro_model)

    def _validate_judge(self) -> None:
        """Judge provider/model/location/bounds; the judge model must differ from every synthesis model."""
        if self.judge_provider not in JUDGE_PROVIDERS:
            raise ValueError(f"JUDGE_PROVIDER must be one of {JUDGE_PROVIDERS}, got {self.judge_provider!r}")
        if not isinstance(self.judge_model, str) or not _MODEL_ID_RE.fullmatch(self.judge_model):
            raise ValueError(f"JUDGE_MODEL is not a valid publisher model id: {self.judge_model!r}")
        prefix = _JUDGE_MODEL_PREFIX[self.judge_provider]
        if not self.judge_model.startswith(prefix):
            raise ValueError(f"JUDGE_PROVIDER={self.judge_provider} requires a '{prefix}*' JUDGE_MODEL, got {self.judge_model!r}")
        if self.judge_model in self.synthesis_models:
            raise ValueError(
                f"JUDGE_MODEL '{self.judge_model}' must differ from the synthesis model(s) {self.synthesis_models} "
                "(GEMINI_FAST_MODEL / GEMINI_PRO_MODEL): an LLM must not grade its own answers"
            )
        if self.judge_provider == "gemini":
            _validate_gemini_model_name(self.judge_model, "judge_model")
        if not isinstance(self.judge_location, str) or not _GCP_IDENTIFIER_RE.fullmatch(self.judge_location):
            raise ValueError(f"JUDGE_LOCATION contains invalid characters: {self.judge_location!r}")
        for name, value, (low, high) in (
            ("JUDGE_MAX_TOKENS", self.judge_max_tokens, _JUDGE_MAX_TOKENS_RANGE),
            ("JUDGE_TIMEOUT_SECONDS", self.judge_timeout_seconds, _JUDGE_TIMEOUT_RANGE),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}, got {value!r}")
        if self.agent_thinking_level not in _VALID_THINKING_LEVELS:
            raise ValueError(f"AGENT_THINKING_LEVEL must be one of {_VALID_THINKING_LEVELS}, got {self.agent_thinking_level!r}")

    def _validate_gcp_identity(self) -> None:
        """Project/bucket/location come only from the environment; live|record modes require a project."""
        for name, value in (
            ("GCP_PROJECT_ID", self.gcp_project_id),
            ("GCS_CORPUS_BUCKET", self.gcs_corpus_bucket),
            ("GCP_LOCATION", self.gcp_location),
            ("VERTEX_GENAI_LOCATION", self.vertex_genai_location),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if value and not _GCP_IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"{name} contains invalid characters: {value!r}")
        if self.is_live and not self.gcp_project_id:
            raise ValueError(
                "GCP_PROJECT_ID must be set (in the git-ignored .env or the environment) when DEMO_MODE=live|record"
            )
        if not self.gcp_location or not self.vertex_genai_location:
            raise ValueError("GCP_LOCATION and VERTEX_GENAI_LOCATION must be non-empty")
        _validate_gemini_model_name(self.gemini_fast_model, "gemini_fast_model")
        _validate_gemini_model_name(self.gemini_pro_model, "gemini_pro_model")
        _validate_gemini_model_name(self.text_embedding_model, "text_embedding_model")
        _validate_gemini_model_name(self.multimodal_embedding_model, "multimodal_embedding_model")
        if self.gemini_thinking_level not in _VALID_THINKING_LEVELS:
            raise ValueError(
                f"Invalid gemini_thinking_level '{self.gemini_thinking_level}'; "
                f"must be one of {_VALID_THINKING_LEVELS}"
            )
        if self.query_timeout_seconds < 1 or self.query_timeout_seconds > 120:
            raise ValueError("query_timeout_seconds must be between 1 and 120")
        if self.max_upload_bytes < 1024 or self.max_upload_bytes > 50 * 1024 * 1024:
            raise ValueError("max_upload_bytes must be between 1 KB and 50 MB")
        if self.max_upload_pages < 1 or self.max_upload_pages > 100:
            raise ValueError("max_upload_pages must be between 1 and 100")


def _parse_enabled_levels(raw: str) -> tuple[str, ...]:
    """Parse the ENABLED_LEVELS comma list; empty input is a configuration error (fail at startup)."""
    levels = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not levels:
        raise ValueError("ENABLED_LEVELS must list at least one level id (e.g. 'level1_naive')")
    return levels


def _parse_float(raw: str, name: str) -> float:
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _parse_int(raw: str, name: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_settings(env: Mapping[str, str] | None = None) -> ArenaSettings:
    """Load validated ArenaSettings from environment variables, .env file, or an explicit mapping."""
    if env is None:
        dotenv_vals = _load_dotenv_file(_PROJECT_ROOT / ".env")
        merged: dict[str, str] = {**dotenv_vals, **os.environ}
        source: Mapping[str, str] = merged
    else:
        if not isinstance(env, Mapping):
            raise TypeError("env must be a Mapping[str, str] or None")
        source = env

    mode = source.get("DEMO_MODE", "replay").strip().lower()
    timeout_raw = source.get("QUERY_TIMEOUT_SECONDS", "90").strip()
    max_bytes_raw = source.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)).strip()
    max_pages_raw = source.get("MAX_UPLOAD_PAGES", "30").strip()
    pro_model = (
        source.get("GEMINI_PRO_MODEL")
        or source.get("GEMINI_REASONING_MODEL")
        or DEFAULT_GEMINI_PRO_MODEL
    ).strip()
    thinking_level = source.get("GEMINI_THINKING_LEVEL", DEFAULT_GEMINI_THINKING_LEVEL).strip().lower()

    project_id = source.get("GCP_PROJECT_ID", "").strip()
    bucket = source.get("GCS_CORPUS_BUCKET", "").strip()
    if not bucket and project_id:
        bucket = f"{project_id}-rag-arena-corpus"  # same derivation as scripts/bootstrap_gcp.sh and infra/main.tf

    return ArenaSettings(
        demo_mode=mode,
        gcp_project_id=project_id,
        gcp_location=source.get("GCP_LOCATION", "us-central1").strip(),
        vertex_genai_location=source.get("VERTEX_GENAI_LOCATION", "global").strip(),
        gcs_corpus_bucket=bucket,
        cloudsdk_config=source.get("CLOUDSDK_CONFIG", "").strip(),
        google_application_credentials=source.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip(),
        bq_dataset=source.get("BQ_DATASET", "rag_arena").strip(),
        bq_table=source.get("BQ_TABLE", "document_chunks").strip(),
        vvs2_collection=source.get("VVS2_COLLECTION", "procurement-chunks-v2").strip(),
        alloydb_table=source.get("ALLOYDB_TABLE", "document_chunks").strip(),
        alloydb_dsn=source.get("ALLOYDB_DSN", "").strip(),
        gemini_fast_model=source.get("GEMINI_FAST_MODEL", DEFAULT_GEMINI_FAST_MODEL).strip(),
        gemini_pro_model=pro_model,
        gemini_thinking_level=thinking_level,
        text_embedding_model=source.get("TEXT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
        multimodal_embedding_model=source.get(
            "MULTIMODAL_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        ).strip(),
        reranker_model=source.get("RERANKER_MODEL", "semantic-ranker-default@latest").strip(),
        query_timeout_seconds=_parse_int(timeout_raw, "QUERY_TIMEOUT_SECONDS"),
        max_upload_bytes=_parse_int(max_bytes_raw, "MAX_UPLOAD_BYTES"),
        max_upload_pages=_parse_int(max_pages_raw, "MAX_UPLOAD_PAGES"),
        enabled_levels=_parse_enabled_levels(source.get("ENABLED_LEVELS", ",".join(DEFAULT_ENABLED_LEVELS))),
        replay_speed=_parse_float(source.get("REPLAY_SPEED", "1.0"), "REPLAY_SPEED"),
        max_sse_frames=_parse_int(source.get("MAX_SSE_FRAMES", str(DEFAULT_MAX_SSE_FRAMES)), "MAX_SSE_FRAMES"),
        ingest_job_timeout_seconds=_parse_int(
            source.get("INGEST_JOB_TIMEOUT_SECONDS", str(DEFAULT_INGEST_JOB_TIMEOUT_SECONDS)),
            "INGEST_JOB_TIMEOUT_SECONDS",
        ),
        allow_dev_fixtures=_parse_bool(source.get("ALLOW_DEV_FIXTURES", "0")),
        recordings_dir=source.get("RECORDINGS_DIR", "").strip(),
        app_version=source.get("APP_VERSION", "3.0.0").strip() or "3.0.0",
        static_dir=source.get("STATIC_DIR", "").strip(),
        hybrid_candidate_k=_parse_int(source.get("HYBRID_CANDIDATE_K", str(DEFAULT_HYBRID_CANDIDATE_K)), "HYBRID_CANDIDATE_K"),
        rerank_top_n=_parse_int(source.get("RERANK_TOP_N", str(DEFAULT_RERANK_TOP_N)), "RERANK_TOP_N"),
        ranking_timeout_seconds=_parse_int(
            source.get("RANKING_TIMEOUT_SECONDS", str(DEFAULT_RANKING_TIMEOUT_SECONDS)), "RANKING_TIMEOUT_SECONDS"
        ),
        **_judge_kwargs(source),
        **_synthesis_kwargs(source),
        **_level3_kwargs(source),
        **_vais_kwargs(source),
        agent_max_turns=_parse_int(source.get("AGENT_MAX_TURNS", str(DEFAULT_AGENT_MAX_TURNS)), "AGENT_MAX_TURNS"),
        agent_max_tool_calls_per_turn=_parse_int(
            source.get("AGENT_MAX_TOOL_CALLS_PER_TURN", str(DEFAULT_AGENT_MAX_TOOL_CALLS_PER_TURN)), "AGENT_MAX_TOOL_CALLS_PER_TURN"
        ),
        agent_thinking_level=source.get("AGENT_THINKING_LEVEL", DEFAULT_AGENT_THINKING_LEVEL).strip().lower(),
        agent_model_call_timeout_seconds=_parse_int(
            source.get("AGENT_MODEL_CALL_TIMEOUT_SECONDS", str(DEFAULT_AGENT_MODEL_CALL_TIMEOUT_SECONDS)), "AGENT_MODEL_CALL_TIMEOUT_SECONDS"
        ),
    )


def _level3_kwargs(source: Mapping[str, str]) -> dict[str, object]:
    """DOCAI_* / VISION_MODEL keys → ArenaSettings Level 3 fields (validated in ``ArenaSettings._validate_level3``)."""
    return {
        "docai_location": source.get("DOCAI_LOCATION", DEFAULT_DOCAI_LOCATION).strip(),
        "docai_processor_id": source.get("DOCAI_PROCESSOR_ID", "").strip(),
        "docai_processor_version": source.get("DOCAI_PROCESSOR_VERSION", "").strip(),
        "docai_timeout_seconds": _parse_int(
            source.get("DOCAI_TIMEOUT_SECONDS", str(DEFAULT_DOCAI_TIMEOUT_SECONDS)), "DOCAI_TIMEOUT_SECONDS"
        ),
        "vision_model": source.get("VISION_MODEL", DEFAULT_GEMINI_FAST_MODEL).strip(),
    }


def _vais_kwargs(source: Mapping[str, str]) -> dict[str, object]:
    """VAIS_* keys → ArenaSettings Level 5 fields (validated in ``ArenaSettings._validate_vais``)."""
    ints = (
        ("vais_page_size", "VAIS_PAGE_SIZE", DEFAULT_VAIS_PAGE_SIZE),
        ("vais_max_segments", "VAIS_MAX_SEGMENTS", DEFAULT_VAIS_MAX_SEGMENTS),
        ("vais_summary_result_count", "VAIS_SUMMARY_RESULT_COUNT", DEFAULT_VAIS_SUMMARY_RESULT_COUNT),
        ("vais_timeout_seconds", "VAIS_TIMEOUT_SECONDS", DEFAULT_VAIS_TIMEOUT_SECONDS),
    )
    out: dict[str, object] = {field: _parse_int(source.get(key, str(default)), key) for field, key, default in ints}
    out.update({
        "vais_location": source.get("VAIS_LOCATION", DEFAULT_VAIS_LOCATION).strip(),
        "vais_data_store_id": source.get("VAIS_DATA_STORE_ID", "").strip(),
        "vais_engine_id": source.get("VAIS_ENGINE_ID", "").strip(),
        "vais_summary_model_version": source.get("VAIS_SUMMARY_MODEL_VERSION", DEFAULT_VAIS_SUMMARY_MODEL_VERSION).strip(),
    })
    return out


def _synthesis_kwargs(source: Mapping[str, str]) -> dict[str, object]:
    """SYNTHESIS_* / MODEL_VERIFICATION_FILE / MODEL_LOCATION_<ID> keys → ArenaSettings fields (validated there)."""
    overrides = sorted(
        (key[len(MODEL_LOCATION_ENV_PREFIX):].strip(), str(value).strip())
        for key, value in source.items()
        if isinstance(key, str) and key.startswith(MODEL_LOCATION_ENV_PREFIX) and str(value).strip()
    )
    return {
        "synthesis_max_tokens": _parse_int(
            source.get("SYNTHESIS_MAX_TOKENS", str(DEFAULT_SYNTHESIS_MAX_TOKENS)), "SYNTHESIS_MAX_TOKENS"
        ),
        "synthesis_timeout_seconds": _parse_int(
            source.get("SYNTHESIS_TIMEOUT_SECONDS", str(DEFAULT_SYNTHESIS_TIMEOUT_SECONDS)), "SYNTHESIS_TIMEOUT_SECONDS"
        ),
        "model_verification_file": source.get("MODEL_VERIFICATION_FILE", "").strip(),
        "model_locations": tuple(overrides[: _MAX_MODEL_LOCATION_OVERRIDES + 1]),
    }


def _judge_kwargs(source: Mapping[str, str]) -> dict[str, object]:
    """JUDGE_* environment keys → ArenaSettings judge fields (validated in ``ArenaSettings._validate_judge``)."""
    return {
        "judge_provider": source.get("JUDGE_PROVIDER", DEFAULT_JUDGE_PROVIDER).strip().lower(),
        "judge_model": source.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL).strip(),
        "judge_location": source.get("JUDGE_LOCATION", DEFAULT_JUDGE_LOCATION).strip(),
        "judge_max_tokens": _parse_int(source.get("JUDGE_MAX_TOKENS", str(DEFAULT_JUDGE_MAX_TOKENS)), "JUDGE_MAX_TOKENS"),
        "judge_timeout_seconds": _parse_int(
            source.get("JUDGE_TIMEOUT_SECONDS", str(DEFAULT_JUDGE_TIMEOUT_SECONDS)), "JUDGE_TIMEOUT_SECONDS"
        ),
    }
