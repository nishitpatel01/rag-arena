"""Serve the built SPA from the same origin as the API (Cloud Run single container, spec §8.1).

Route order matters: the API routes are registered first, then ``/api/{rest}`` (any method → 404 JSON, so a typo in an
API path never returns ``index.html``), then the SPA catch-all: an existing file under the static directory is served
as-is (hashed ``/assets/*`` cached as immutable), a path that looks like a file but is missing is a plain 404, and
every other path is a client route → ``index.html`` (``no-cache``) so react-router deep links survive a reload.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_IMMUTABLE = "public, max-age=31536000, immutable"
_MAX_PATH_CHARS = 512


async def api_not_found_endpoint(request: Request) -> JSONResponse:
    """Any unmatched ``/api/**`` path (any method): JSON 404, never the SPA shell."""
    return JSONResponse({"error": "not_found", "path": request.url.path[:_MAX_PATH_CHARS]}, status_code=404)


def resolve_static_dir(configured: str, default_dir: Path) -> Path | None:
    """``STATIC_DIR`` must hold ``index.html`` (fail loud); unset → ``default_dir`` only when a build exists there."""
    if not isinstance(configured, str) or not isinstance(default_dir, Path):
        raise TypeError("configured must be a str and default_dir a Path")
    if configured.strip():
        candidate = Path(configured.strip())
        if not (candidate / "index.html").is_file():
            raise ValueError(f"STATIC_DIR={configured!r} does not contain index.html (build the SPA or unset STATIC_DIR)")
        return candidate.resolve()
    if (default_dir / "index.html").is_file():
        return default_dir.resolve()
    return None


def _safe_file(root: Path, rel_path: str) -> Path | None:
    """Return the file under ``root`` for ``rel_path``, or None when missing or outside ``root`` (traversal)."""
    if len(rel_path) > _MAX_PATH_CHARS or "\x00" in rel_path:
        return None
    candidate = (root / rel_path.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _looks_like_file(rel_path: str) -> bool:
    last = rel_path.rstrip("/").rsplit("/", 1)[-1]
    return "." in last


def spa_routes(static_dir: Path) -> list[Route]:
    """``/api/{rest}`` JSON 404 + the SPA catch-all bound to ``static_dir`` (must already be resolved)."""
    if not isinstance(static_dir, Path) or not (static_dir / "index.html").is_file():
        raise ValueError("static_dir must be a directory containing index.html")
    root = static_dir.resolve()
    index_html = root / "index.html"

    async def spa_endpoint(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse("Method Not Allowed", status_code=405)
        rel_path = str(request.path_params.get("rest", ""))
        found = _safe_file(root, rel_path) if rel_path else None
        if found is not None and found != index_html:
            immutable = rel_path.startswith("assets/")
            return FileResponse(found, headers={"Cache-Control": _IMMUTABLE if immutable else "no-cache"})
        if rel_path and _looks_like_file(rel_path):
            return PlainTextResponse("Not Found", status_code=404)
        return FileResponse(index_html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return [
        Route("/api/{rest:path}", endpoint=api_not_found_endpoint, methods=_ALL_METHODS),
        Route("/{rest:path}", endpoint=spa_endpoint, methods=_ALL_METHODS),
    ]
