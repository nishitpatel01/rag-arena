"""Document viewer endpoints: render a real corpus page (PNG) and locate a cited excerpt on it (PyMuPDF).

Security model: a document is resolved ONLY by exact ``(solicitation_id, doc_name)`` match against
``discover_corpus_docs(corpus_dir)``; user input is never joined into a filesystem path. Highlights come
only from ``page.search_for`` on the real page text — when nothing matches, ``matched:false`` and no rects
(spec P1 measured-not-simulated, P5 honest labels).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.ingestion.jobs import DEFAULT_CORPUS_DIR, discover_corpus_docs
from app.ingestion.parsers import _import_pymupdf

MIN_DPI = 72
MAX_DPI = 200
DEFAULT_DPI = 110
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_RENDER_PIXELS = 40_000_000
MAX_NAME_CHARS = 255
MAX_TEXT_CHARS = 600
MAX_SEARCH_ATTEMPTS = 6
MIN_RUN_WORDS = 6
FIRST_QUERY_CHARS = 80
_MAX_EXCERPT_WORDS = 150
_MAX_PAGE_WORDS = 5_000
_SEARCH_HIT_MAX = 64
_RENDER_TIMEOUT_SECONDS = 20.0
_CACHE_CONTROL = "private, max-age=3600"
_FORBIDDEN_NAME = re.compile(r"(\.\.|[/\\\x00]|%2f|%5c|%00)", re.IGNORECASE)
_HIGHLIGHT_METHOD = "pymupdf.search_for"


class DocumentRequestError(ValueError):
    """Client error carrying the HTTP status to return (400/404/413)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PageRequest:
    path: Path
    solicitation_id: str
    doc_name: str
    page: int


def clamp_dpi(dpi: int) -> int:
    """Clamp a requested DPI into ``[MIN_DPI, MAX_DPI]``."""
    if not isinstance(dpi, int) or isinstance(dpi, bool):
        raise DocumentRequestError(400, "dpi must be an integer")
    return max(MIN_DPI, min(MAX_DPI, dpi))


def normalize_rect(rect: tuple[float, float, float, float], width: float, height: float) -> dict[str, float] | None:
    """Normalize a page-space rect to 0–1 (clamped; text may extend past the page edge). None if fully off-page."""
    if width <= 0 or height <= 0:
        raise ValueError("page width/height must be positive")
    x0, y0, x1, y1 = (float(v) for v in rect)
    nx0, nx1 = max(0.0, min(1.0, x0 / width)), max(0.0, min(1.0, x1 / width))
    ny0, ny1 = max(0.0, min(1.0, y0 / height)), max(0.0, min(1.0, y1 / height))
    if nx1 <= nx0 or ny1 <= ny0:
        return None
    return {"x0": round(nx0, 5), "y0": round(ny0, 5), "x1": round(nx1, 5), "y1": round(ny1, 5)}


def _is_separator(word: str) -> bool:
    """Punctuation-only tokens (table '|' cells, bullets) that chunk text drops but the rendered page keeps."""
    return not any(ch.isalnum() for ch in word)


def candidate_runs(excerpt_words: list[str], page_words: list[str], *, min_words: int = MIN_RUN_WORDS) -> list[str]:
    """Page-text spans whose words match a contiguous excerpt run (≥ ``min_words``); longest first.

    Case-insensitive like ``search_for``; punctuation-only tokens are skipped on both sides for matching, and
    the returned string is the page's own text for the span (separators included) so ``search_for`` can find
    it verbatim. Bounded: O(len(excerpt) × len(page)) with both lists capped.
    """
    a = [w.casefold() for w in excerpt_words[:_MAX_EXCERPT_WORDS] if not _is_separator(w)]
    page_idx = [j for j, w in enumerate(page_words[:_MAX_PAGE_WORDS]) if not _is_separator(w)]
    b = [page_words[j].casefold() for j in page_idx]
    best: dict[int, tuple[int, int]] = {}  # excerpt end index (exclusive) -> (run length, page end index)
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best.get(i, (0, 0))[0]:
                    best[i] = (cur[j], j)
        prev = cur
    spans = sorted(((n, end - n, end, pj) for end, (n, pj) in best.items() if n >= min_words), key=lambda s: (-s[0], s[1]))
    runs: list[str] = []
    covered: list[tuple[int, int]] = []
    for n, start, end, page_end in spans:
        if any(start >= cs and end <= ce for cs, ce in covered):
            continue  # a sub-span of a longer run already listed
        covered.append((start, end))
        first, last = page_idx[page_end - n], page_idx[page_end - 1]
        runs.append(" ".join(page_words[first : last + 1]))
    return runs



def _search_queries(excerpt: str, page_words: list[str]) -> list[str]:
    """First ~80 chars, then longest matching word runs (and their halves); deduped, ≤ MAX_SEARCH_ATTEMPTS."""
    queries: list[str] = []
    first = excerpt[:FIRST_QUERY_CHARS].strip()
    if first:
        queries.append(first)
    words = excerpt.split()
    for run in candidate_runs(words, page_words):
        queries.append(run)
        run_words = run.split()
        if len(run_words) >= 2 * MIN_RUN_WORDS:  # halves survive a line-break/hyphenation mismatch in the other half
            mid = len(run_words) // 2
            queries.extend([" ".join(run_words[:mid]), " ".join(run_words[mid:])])
    seen: set[str] = set()
    out = [q for q in queries if not (q in seen or seen.add(q))]
    return out[:MAX_SEARCH_ATTEMPTS]


def _corpus_dir(request: Request) -> Path:
    override = getattr(request.app.state, "corpus_dir", None)
    return override if isinstance(override, Path) else DEFAULT_CORPUS_DIR


def _required_name(params: Any, key: str) -> str:
    raw = params.get(key)
    if raw is None or not str(raw).strip():
        raise DocumentRequestError(400, f"Query parameter '{key}' is required")
    value = str(raw).strip()
    if len(value) > MAX_NAME_CHARS:
        raise DocumentRequestError(400, f"Query parameter '{key}' exceeds {MAX_NAME_CHARS} characters")
    if _FORBIDDEN_NAME.search(value) or value.startswith("~"):
        raise DocumentRequestError(400, f"Query parameter '{key}' must be a plain name (no path separators or '..')")
    return value


def _int_param(params: Any, key: str, default: int | None) -> int:
    raw = params.get(key)
    if raw is None or raw == "":
        if default is None:
            raise DocumentRequestError(400, f"Query parameter '{key}' is required")
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise DocumentRequestError(400, f"Query parameter '{key}' must be an integer, got {str(raw)[:40]!r}") from exc


def resolve_page_request(request: Request) -> PageRequest:
    """Validate params and resolve the doc by exact match against the discovered corpus (never path joining)."""
    params = request.query_params
    sol = _required_name(params, "solicitation_id")
    doc_name = _required_name(params, "doc_name")
    page = _int_param(params, "page", None)
    if page < 1:
        raise DocumentRequestError(400, f"page must be >= 1, got {page}")
    try:
        docs = discover_corpus_docs(_corpus_dir(request))
    except FileNotFoundError as exc:
        raise DocumentRequestError(404, f"Corpus unavailable: {exc}") from exc
    match = next((d for d in docs if d.solicitation_id == sol and d.path.name == doc_name), None)
    if match is None:
        raise DocumentRequestError(404, f"Unknown document {doc_name!r} in {sol!r} (not in the local corpus)")
    return PageRequest(path=match.path, solicitation_id=sol, doc_name=doc_name, page=page)


def _open_page(pymupdf: Any, doc: Any, req: PageRequest) -> Any:
    if req.page > doc.page_count:
        raise DocumentRequestError(400, f"page {req.page} out of range: {req.doc_name} has {doc.page_count} page(s)")
    return doc[req.page - 1]


def _require_pymupdf() -> Any:
    pymupdf = _import_pymupdf()
    if pymupdf is None:
        raise RuntimeError("PyMuPDF (pymupdf) is not installed; the document viewer cannot render pages")
    return pymupdf


def render_page_png(req: PageRequest, dpi: int) -> tuple[bytes, int]:
    """Render one page to PNG at ``dpi`` → (png, page_count); enforces pixel and byte bounds."""
    pymupdf = _require_pymupdf()
    with pymupdf.open(req.path) as doc:
        page = _open_page(pymupdf, doc, req)
        zoom = dpi / 72.0
        pixels = (page.rect.width * zoom) * (page.rect.height * zoom)
        if pixels > MAX_RENDER_PIXELS:
            raise DocumentRequestError(413, f"Rendered page would be {int(pixels)} px, exceeds {MAX_RENDER_PIXELS} px bound")
        png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
        page_count = int(doc.page_count)
    if len(png) > MAX_PNG_BYTES:
        raise DocumentRequestError(413, f"Rendered PNG is {len(png)} bytes, exceeds {MAX_PNG_BYTES} byte bound")
    return png, page_count


def locate_excerpt(req: PageRequest, text: str) -> dict[str, Any]:
    """Find ``text`` on the real page via ``search_for``; never invents a rect."""
    pymupdf = _require_pymupdf()
    with pymupdf.open(req.path) as doc:
        page = _open_page(pymupdf, doc, req)
        clip = pymupdf.INFINITE_RECT()  # same unclipped semantics as parsers (text may pass the mediabox)
        width, height = float(page.rect.width), float(page.rect.height)
        page_count = int(doc.page_count)
        page_words = [w[4] for w in page.get_text("words", clip=clip)[:_MAX_PAGE_WORDS]]
        queries = _search_queries(text, page_words)
        attempts = 0
        for query in queries:
            attempts += 1
            hits = page.search_for(query, clip=clip)[:_SEARCH_HIT_MAX]
            rects = [normalize_rect(tuple(r * page.rotation_matrix), width, height) for r in hits]
            rects = [r for r in rects if r is not None]
            if rects:
                return _highlight_body(width, height, page_count, rects, True, query, attempts)
    return _highlight_body(width, height, page_count, [], False, None, attempts)


def _highlight_body(w: float, h: float, page_count: int, rects: list[dict[str, float]], matched: bool, query: str | None, attempts: int) -> dict[str, Any]:
    return {
        "page_width": round(w, 3),
        "page_height": round(h, 3),
        "page_count": page_count,
        "rects": rects,
        "method": _HIGHLIGHT_METHOD,
        "matched": matched,
        "query_used": query,
        "attempts": attempts,
    }


def _error(exc: DocumentRequestError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


async def document_page_endpoint(request: Request) -> Response:
    """GET /api/v1/documents/page?solicitation_id&doc_name&page&dpi → image/png of the real page."""
    try:
        req = resolve_page_request(request)
        dpi = clamp_dpi(_int_param(request.query_params, "dpi", DEFAULT_DPI))
        png, page_count = await asyncio.wait_for(asyncio.to_thread(render_page_png, req, dpi), timeout=_RENDER_TIMEOUT_SECONDS)
    except DocumentRequestError as exc:
        return _error(exc)
    except asyncio.TimeoutError:
        return JSONResponse({"error": f"Page render exceeded {_RENDER_TIMEOUT_SECONDS:.0f} s"}, status_code=504)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    headers = {"Cache-Control": _CACHE_CONTROL, "X-Render-Dpi": str(dpi), "X-Page-Count": str(page_count)}
    return Response(content=png, media_type="image/png", headers=headers)


async def document_highlights_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/documents/highlights?solicitation_id&doc_name&page&text → normalized search_for rects."""
    try:
        text = str(request.query_params.get("text") or "").strip()
        if not text:
            raise DocumentRequestError(400, "Query parameter 'text' is required")
        if len(text) > MAX_TEXT_CHARS:
            raise DocumentRequestError(400, f"Query parameter 'text' exceeds {MAX_TEXT_CHARS} characters")
        req = resolve_page_request(request)
        body = await asyncio.wait_for(asyncio.to_thread(locate_excerpt, req, text), timeout=_RENDER_TIMEOUT_SECONDS)
    except DocumentRequestError as exc:
        return _error(exc)
    except asyncio.TimeoutError:
        return JSONResponse({"error": f"Highlight search exceeded {_RENDER_TIMEOUT_SECONDS:.0f} s"}, status_code=504)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(body, headers={"Cache-Control": _CACHE_CONTROL})


def get_document_routes() -> list[Route]:
    """Routes for the document viewer (registered by ``app.main.create_app``)."""
    return [
        Route("/api/v1/documents/page", endpoint=document_page_endpoint, methods=["GET"]),
        Route("/api/v1/documents/highlights", endpoint=document_highlights_endpoint, methods=["GET"]),
    ]


__all__ = [
    "DEFAULT_CORPUS_DIR",
    "MAX_PNG_BYTES",
    "MAX_SEARCH_ATTEMPTS",
    "candidate_runs",
    "clamp_dpi",
    "get_document_routes",
    "locate_excerpt",
    "normalize_rect",
    "render_page_png",
    "resolve_page_request",
]
