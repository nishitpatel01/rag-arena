"""ASGI application entrypoint for The RAG Arena backend."""

from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from app.api.documents import get_document_routes
from app.api.spa import resolve_static_dir, spa_routes
from app.api.tco_api import get_tco_routes
from app.api.uploads_api import UploadServices, get_upload_routes
from app.api.routes import get_api_routes
from app.core.config import ArenaSettings, get_settings
from app.api.query_runs import QueryRunWriter
from app.ingestion.jobs import BackendFactory, IngestJobRunner, ManagedFactory, unavailable_backends
from app.ingestion.live_backends import LiveQueryRunWriter, live_backends
from app.ingestion.managed_job import live_managed_backends, managed_unavailable
from app.ingestion.managed_upload_job import live_managed_upload_backends, managed_upload_unavailable
from app.ingestion.rag_engine_job import (
    live_rag_engine_backends,
    live_rag_engine_upload_backends,
    rag_engine_unavailable,
    rag_engine_upload_unavailable,
)

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://storage.googleapis.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)
_STATIC_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", _CSP_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)
_SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600


class _SecurityHeadersAndSessionMiddleware:
    """Pure-ASGI middleware adding CSP/hardening headers and public-mode HttpOnly session cookies without buffering SSE."""

    def __init__(self, app: object) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return
        scope.setdefault("state", {})

        async def _send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                raw_headers = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in raw_headers}
                for key, val in _STATIC_SECURITY_HEADERS:
                    if key not in existing:
                        raw_headers.append((key, val))
                cookie_info = scope.get("state", {}).get("new_session_cookie")
                if isinstance(cookie_info, tuple) and len(cookie_info) == 3:
                    name, sid, is_https = cookie_info
                    flags = "; Secure" if is_https else ""
                    cookie_val = f"{name}={sid}; Path=/; Max-Age={_SESSION_MAX_AGE_SECONDS}; HttpOnly; SameSite=Lax{flags}"
                    raw_headers.append((b"set-cookie", cookie_val.encode("ascii")))
                message = {**message, "headers": raw_headers}
            await send(message)  # type: ignore[operator]

        await self._app(scope, receive, _send)  # type: ignore[operator]


def create_app(
    allowed_origins: list[str] | None = None,
    *,
    settings: ArenaSettings | None = None,
    ingest_backend_factory: BackendFactory | None = None,
    ingest_managed_factory: ManagedFactory | None = None,
    ingest_upload_managed_factory: ManagedFactory | None = None,
    ingest_rag_engine_factory: ManagedFactory | None = None,
    ingest_upload_rag_engine_factory: ManagedFactory | None = None,
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
    routes.extend(get_tco_routes())

    static_dir = resolve_static_dir(resolved.static_dir, _FRONTEND_DIST)
    if static_dir is not None:
        routes.extend(spa_routes(static_dir))  # after every API route: /api/** misses → 404 JSON, client routes → index.html

    middleware = [
        Middleware(_SecurityHeadersAndSessionMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_origin_regex=os.environ.get(
                "CORS_ALLOW_ORIGIN_REGEX", r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
            ),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        ),
    ]
    application = Starlette(debug=False, routes=routes, middleware=middleware)
    factory = ingest_backend_factory or (live_backends if resolved.is_live else unavailable_backends)
    writer = query_run_writer
    if writer is None and resolved.is_live:
        writer = LiveQueryRunWriter(resolved)
    application.state.settings = resolved
    managed = ingest_managed_factory or (live_managed_backends if resolved.is_live else managed_unavailable)
    upload_managed = ingest_upload_managed_factory or (
        live_managed_upload_backends if resolved.is_live else managed_upload_unavailable
    )
    rag_engine = ingest_rag_engine_factory or (live_rag_engine_backends if resolved.is_live else rag_engine_unavailable)
    upload_rag_engine = ingest_upload_rag_engine_factory or (
        live_rag_engine_upload_backends if resolved.is_live else rag_engine_upload_unavailable
    )
    application.state.ingest_runner = IngestJobRunner(
        resolved,
        backend_factory=factory,
        managed_factory=managed,
        upload_managed_factory=upload_managed,
        rag_engine_factory=rag_engine,
        upload_rag_engine_factory=upload_rag_engine,
    )
    application.state.query_run_writer = writer
    # v1.1 uploads: live service only (replay copy answers 404); GCS identity = ADC (you locally, the SA on Cloud Run)
    application.state.upload_services = UploadServices(resolved) if resolved.is_live and resolved.upload_bucket else None
    return application


app = create_app()
