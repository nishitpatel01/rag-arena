"""ASGI application entrypoint for The RAG Arena backend."""

from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.api.documents import get_document_routes
from app.api.routes import get_api_routes
from app.core.config import ArenaSettings, get_settings
from app.api.query_runs import QueryRunWriter
from app.ingestion.jobs import BackendFactory, IngestJobRunner, unavailable_backends
from app.ingestion.live_backends import LiveQueryRunWriter, live_backends

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

    if _FRONTEND_DIST.is_dir() and (_FRONTEND_DIST / "index.html").is_file():
        routes.append(
            Mount("/", app=StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
        )

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_origin_regex=os.environ.get(
                "CORS_ALLOW_ORIGIN_REGEX", r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
            ),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    ]
    application = Starlette(debug=False, routes=routes, middleware=middleware)
    factory = ingest_backend_factory or (live_backends if resolved.is_live else unavailable_backends)
    writer = query_run_writer
    if writer is None and resolved.is_live:
        writer = LiveQueryRunWriter(resolved)
    application.state.settings = resolved
    application.state.ingest_runner = IngestJobRunner(resolved, backend_factory=factory)
    application.state.query_run_writer = writer
    return application


app = create_app()
