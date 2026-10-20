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
DEFAULT_ENABLED_LEVELS: tuple[str, ...] = ("level1_naive",)
DEFAULT_MAX_SSE_FRAMES: int = 600
DEFAULT_INGEST_JOB_TIMEOUT_SECONDS: int = 1800


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
    )
