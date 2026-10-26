"""ASGI application entrypoint for The RAG Arena backend."""

from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from app.api.documents import get_document_routes
from app.api.spa import resolve_static_dir, spa_routes
from app.api.uploads_api import UploadServices, get_upload_routes
from app.api.routes import get_api_routes
from app.core.config import ArenaSettings, get_settings
from app.api.query_runs import QueryRunWriter
from app.ingestion.jobs import BackendFactory, IngestJobRunner, ManagedFactory, unavailable_backends
from app.ingestion.live_backends import LiveQueryRunWriter, live_backends
from app.ingestion.managed_job import live_managed_backends, managed_unavailable

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def create_app(
    allowed_origins: list[str] | None = None,
    *,
    settings: ArenaSettings | None = None,
    ingest_backend_factory: BackendFactory | None = None,
    ingest_managed_factory: ManagedFactory | None = None,
    query_run_writer: QueryRunWriter | None | object = None,
) -> Starlette:
    """Create the Starlette app; settings are validated once here (startup fails loud on bad config).

    Live/record modes default to the real Vertex + BigQuery ingestion backends and a lazy BigQuery
    ``query_runs`` writer; replay mode gets ``unavailable_backends`` and no writer (zero credentials).
    """
    origins = _ALLOWED_ORIGINS if allowed_origins is None else allowed_origins
    if not isinstance(origins, list) or not origins:
        raise ValueError("allowed_origins must be a non-empty list of origin URLs")
    resolved = get_settings() if settings is None else settings
    if not isinstance(resolved, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance or None")

    routes = list(get_api_routes())
    if not routes:
        raise RuntimeError("API route table must not be empty")
    routes.extend(get_document_routes())
    routes.extend(get_upload_routes())

    static_dir = resolve_static_dir(resolved.static_dir, _FRONTEND_DIST)
    if static_dir is not None:
        routes.extend(spa_routes(static_dir))  # after every API route: /api/** misses → 404 JSON, client routes → index.html

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_origin_regex=os.environ.get(
                "CORS_ALLOW_ORIGIN_REGEX", r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
            ),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
    ]
    application = Starlette(debug=False, routes=routes, middleware=middleware)
    factory = ingest_backend_factory or (live_backends if resolved.is_live else unavailable_backends)
    writer = query_run_writer
    if writer is None and resolved.is_live:
        writer = LiveQueryRunWriter(resolved)
    application.state.settings = resolved
    managed = ingest_managed_factory or (live_managed_backends if resolved.is_live else managed_unavailable)
    application.state.ingest_runner = IngestJobRunner(resolved, backend_factory=factory, managed_factory=managed)
    application.state.query_run_writer = writer
    # v1.1 uploads: live service only (replay copy answers 404); GCS identity = ADC (you locally, the SA on Cloud Run)
    application.state.upload_services = UploadServices(resolved) if resolved.is_live and resolved.upload_bucket else None
    return application


app = create_app()
