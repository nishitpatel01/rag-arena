"""Lazy Google Cloud SDK client factories with bounded singleton caching for Live Mode execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.config import ArenaSettings

_MAX_CACHED_CLIENTS = 16
_GENAI_CLIENTS: dict[tuple[str, str], Any] = {}
_BQ_CLIENTS: dict[tuple[str, str], Any] = {}
_RANK_CLIENTS: dict[str, Any] = {}
_SEARCH_CLIENTS: dict[str, Any] = {}
_VVS2_CLIENTS: dict[tuple[str, str], Any] = {}
_ALLOYDB_POOLS: dict[str, Any] = {}
_ADC_CREDENTIALS: dict[str, Any] = {}
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _ensure_live_settings(settings: ArenaSettings) -> None:
    """Validate that ArenaSettings is provided and configured for live GCP calls."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if not settings.is_live:
        raise RuntimeError(
            "GCP client factory invoked while demo_mode is 'replay'. "
            "Set DEMO_MODE=live or DEMO_MODE=record and configure Google Cloud ADC credentials."
        )
    if not settings.gcp_project_id.strip():
        raise ValueError("settings.gcp_project_id must not be empty (set GCP_PROJECT_ID in .env)")
    if settings.cloudsdk_config and Path(settings.cloudsdk_config).is_dir():
        os.environ["CLOUDSDK_CONFIG"] = settings.cloudsdk_config
    if (
        settings.google_application_credentials
        and Path(settings.google_application_credentials).is_file()
    ):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.google_application_credentials


def _evict_if_full(cache: dict[Any, Any]) -> None:
    """Evict the oldest cached client entry if the bounded cache capacity is reached."""
    while len(cache) >= _MAX_CACHED_CLIENTS:
        oldest = next(iter(cache))
        cache.pop(oldest, None)


def clear_client_cache() -> None:
    """Clear all cached Google Cloud SDK client instances for deterministic test isolation."""
    _GENAI_CLIENTS.clear()
    _BQ_CLIENTS.clear()
    _RANK_CLIENTS.clear()
    _SEARCH_CLIENTS.clear()
    _VVS2_CLIENTS.clear()
    _ALLOYDB_POOLS.clear()
    _ADC_CREDENTIALS.clear()


def get_access_token(settings: ArenaSettings) -> str:
    """Return a fresh ADC OAuth bearer token (cloud-platform scope) for raw Vertex REST calls (e.g. rawPredict)."""
    _ensure_live_settings(settings)
    try:
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest
    except ImportError as exc:
        raise RuntimeError("google-auth (with requests transport) is not installed in the current environment") from exc

    key = settings.gcp_project_id.strip()
    credentials = _ADC_CREDENTIALS.get(key)
    if credentials is None:
        _evict_if_full(_ADC_CREDENTIALS)
        credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        _ADC_CREDENTIALS[key] = credentials
    if not credentials.valid:
        credentials.refresh(AuthRequest())
    token = str(getattr(credentials, "token", "") or "")
    if not token:
        raise RuntimeError("Application Default Credentials returned an empty access token")
    return token


def get_genai_client(settings: ArenaSettings, location: str | None = None) -> Any:
    """Return a cached google.genai Client configured for Vertex AI (``location`` overrides VERTEX_GENAI_LOCATION)."""
    _ensure_live_settings(settings)
    if location is not None and (not isinstance(location, str) or not location.strip()):
        raise ValueError("location must be a non-empty string when provided")
    genai_location = (location or settings.vertex_genai_location).strip() or "global"
    key = (settings.gcp_project_id.strip(), genai_location)
    if key in _GENAI_CLIENTS:
        return _GENAI_CLIENTS[key]

    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai SDK is not installed in the current environment") from exc

    _evict_if_full(_GENAI_CLIENTS)
    client = genai.Client(
        vertexai=True,
        project=key[0],
        location=key[1],
    )
    _GENAI_CLIENTS[key] = client
    return client


def get_bigquery_client(settings: ArenaSettings) -> Any:
    """Return a cached Google Cloud BigQuery Client for vector and lexical hybrid search."""
    _ensure_live_settings(settings)
    key = (settings.gcp_project_id.strip(), settings.gcp_location.strip())
    if key in _BQ_CLIENTS:
        return _BQ_CLIENTS[key]

    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-bigquery SDK is not installed in the current environment"
        ) from exc

    _evict_if_full(_BQ_CLIENTS)
    client = bigquery.Client(project=key[0], location=key[1])
    _BQ_CLIENTS[key] = client
    return client


def get_rank_client(settings: ArenaSettings) -> Any:
    """Return a cached Vertex AI Discovery Engine RankServiceClient for semantic reranking."""
    _ensure_live_settings(settings)
    key = settings.gcp_project_id.strip()
    if key in _RANK_CLIENTS:
        return _RANK_CLIENTS[key]

    try:
        from google.cloud import discoveryengine_v1 as discoveryengine
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-discoveryengine SDK is not installed in the current environment"
        ) from exc

    _evict_if_full(_RANK_CLIENTS)
    client = discoveryengine.RankServiceClient()
    _RANK_CLIENTS[key] = client
    return client


def get_search_client(settings: ArenaSettings) -> Any:
    """Return a cached Vertex AI Discovery Engine SearchServiceClient for Managed Vertex AI Search."""
    _ensure_live_settings(settings)
    key = settings.gcp_project_id.strip()
    if key in _SEARCH_CLIENTS:
        return _SEARCH_CLIENTS[key]

    try:
        from google.cloud import discoveryengine_v1 as discoveryengine
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-discoveryengine SDK is not installed in the current environment"
        ) from exc

    _evict_if_full(_SEARCH_CLIENTS)
    client = discoveryengine.SearchServiceClient()
    _SEARCH_CLIENTS[key] = client
    return client


def get_vector_search_v2_client(settings: ArenaSettings) -> Any:
    """Return a cached Vertex AI Vector Search 2.0 SearchDataObjects / MatchService client."""
    _ensure_live_settings(settings)
    key = (settings.gcp_project_id.strip(), settings.gcp_location.strip())
    if key in _VVS2_CLIENTS:
        return _VVS2_CLIENTS[key]

    try:
        from google.cloud import aiplatform_v1
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-aiplatform SDK is not installed in the current environment"
        ) from exc

    _evict_if_full(_VVS2_CLIENTS)
    client = aiplatform_v1.MatchServiceClient(
        client_options={"api_endpoint": f"{key[1]}-aiplatform.googleapis.com"}
    )
    _VVS2_CLIENTS[key] = client
    return client


def get_alloydb_client(settings: ArenaSettings) -> Any:
    """Return a cached AlloyDB PostgreSQL connection or pool configured via settings.alloydb_dsn."""
    _ensure_live_settings(settings)
    dsn = settings.alloydb_dsn.strip()
    if not dsn:
        raise ValueError("settings.alloydb_dsn must be configured for live AlloyDB ScaNN queries")
    if dsn in _ALLOYDB_POOLS:
        return _ALLOYDB_POOLS[dsn]

    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("psycopg is required for live AlloyDB ScaNN execution") from exc

    _evict_if_full(_ALLOYDB_POOLS)
    conn = psycopg.connect(dsn)
    _ALLOYDB_POOLS[dsn] = conn
    return conn
