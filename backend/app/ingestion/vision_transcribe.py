"""Level 3 ingestion: Gemini vision transcription of diagram pages (spec §3 Level 3; audit §A: v2 never called vision).

Pipeline per diagram page: render the page to PNG with PyMuPDF (bounded DPI / pixel size) → ``generate_content`` on
Vertex AI (``VISION_MODEL``, default ``gemini-4-argon``) with ONE image part + a strict, grounded prompt → strict JSON
parse into a ``DiagramSpec`` (components, labelled connections, regions, notes). Nothing is invented downstream: an
empty, non-JSON, or component-less response raises ``VisionTranscriptionError`` naming the page and the reason, and
the ingest ``parse`` stage fails (spec P2).

Diagram-page detection (documented in the parse payload as ``diagram_heuristic``) — a page is transcribed when:

1. Document AI Layout Parser returned an ``imageBlock`` (figure) on that page, OR
2. PyMuPDF ``page.get_drawings()`` shows a box-and-connector figure: ≥ ``MIN_FILLED_BOXES`` filled rectangles each
   covering 0.5–50 % of the page AND stroked line segments ≤ 2 × boxes + 10 (ruled tables/forms have hundreds of
   line segments and are excluded).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable

MIN_FILLED_BOXES = 4
_BOX_MIN_AREA_FRAC = 0.005
_BOX_MAX_AREA_FRAC = 0.5
_LINE_SLACK = 10
RENDER_DPI = 150
MAX_RENDER_SIDE_PX = 2000
MAX_VISION_PAGES = 40
MAX_VISION_ATTEMPTS = 3
_VISION_BACKOFF_SECONDS = 2.0
_MAX_COMPONENTS = 60
_MAX_CONNECTIONS = 120
_MAX_LABEL_CHARS = 200
DIAGRAM_HEURISTIC = (
    "Document AI Layout Parser imageBlock (figure) on the page OR PyMuPDF get_drawings(): "
    f">= {MIN_FILLED_BOXES} filled rectangles covering {_BOX_MIN_AREA_FRAC:.1%}-{_BOX_MAX_AREA_FRAC:.0%} of the page "
    f"and stroked line segments <= 2 x boxes + {_LINE_SLACK} (box-and-connector figure, not a ruled table)"
)

VISION_PROMPT = """You are transcribing ONE architecture/flow diagram image from a procurement document.
Use ONLY what is visible in the image. Do not use outside knowledge, do not infer components, links, protocols,
regions or numbers that are not drawn or written in the image. If a label is illegible, say so in notes.

Return STRICT JSON (no markdown fences) with exactly these keys:
{
  "title": "diagram title as written, or empty string",
  "components": [{"id": "C1", "label": "text written in or next to the box/shape", "shape": "box|circle|cylinder|icon|other"}],
  "connections": [{"from": "C1", "to": "C2", "label": "text written on the connector or empty", "style": "line|arrow|dashed"}],
  "regions": [{"label": "text of an enclosing container/zone", "contains": ["C1", "C2"]}],
  "notes": ["short factual observations about layout, legend, or illegible parts"]
}
Rules: every connection endpoint must be a component id; list each drawn connector once; keep labels verbatim.
If the image contains NO architecture, network, data-flow or process diagram (for example only a logo, a signature,
a cover page or plain text), return "components": [] and say what the image shows in notes."""


class VisionTranscriptionError(ValueError):
    """The vision model's answer for a page is unusable (empty, not JSON, no components, dangling connections)."""


@dataclass(frozen=True)
class PageDrawingStats:
    page_number: int
    filled_boxes: int
    line_segments: int
    drawings: int
    text_chars: int

    @property
    def is_box_diagram(self) -> bool:
        return self.filled_boxes >= MIN_FILLED_BOXES and self.line_segments <= 2 * self.filled_boxes + _LINE_SLACK


@dataclass(frozen=True)
class DiagramSpec:
    """Validated vision transcription (components / connections / regions / notes)."""

    title: str
    components: tuple[tuple[str, str, str], ...]  # (id, label, shape)
    connections: tuple[tuple[str, str, str, str], ...]  # (from_id, to_id, label, style)
    regions: tuple[tuple[str, tuple[str, ...]], ...]  # (label, member ids)
    notes: tuple[str, ...]

    def label_of(self, cid: str) -> str:
        return next((label for i, label, _ in self.components if i == cid), cid)

    def to_text(self) -> str:
        """Retrieval/synthesis text of the transcription (Markdown-like; every line traceable to the JSON)."""
        lines = [f"Diagram: {self.title}" if self.title else "Diagram (untitled)", "Components:"]
        lines += [f"- {label} ({shape})" for _, label, shape in self.components]
        if self.connections:
            lines.append("Connections:")
            for src, dst, label, style in self.connections:
                arrow = "->" if style == "arrow" else "--"
                suffix = f" [{label}]" if label else ""
                lines.append(f"- {self.label_of(src)} {arrow} {self.label_of(dst)}{suffix}")
        if self.regions:
            lines.append("Regions:")
            lines += [f"- {label}: {', '.join(self.label_of(m) for m in members)}" for label, members in self.regions]
        if self.notes:
            lines.append("Notes:")
            lines += [f"- {n}" for n in self.notes]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "components": [{"id": i, "label": label, "shape": s} for i, label, s in self.components],
            "connections": [{"from": a, "to": b, "label": label, "style": s} for a, b, label, s in self.connections],
            "regions": [{"label": label, "contains": list(m)} for label, m in self.regions],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VisionResult:
    page_number: int
    spec: DiagramSpec
    model: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    duration_ms: int
    attempts: int
    image_bytes: int


def page_drawing_stats(page: Any) -> PageDrawingStats:
    """Count filled box shapes and stroked line segments on one PyMuPDF page."""
    area = float(page.rect.width * page.rect.height) or 1.0
    drawings = page.get_drawings()
    boxes = 0
    lines = 0
    for d in drawings[:5000]:
        rect = d.get("rect")
        frac = (rect.width * rect.height) / area if rect is not None else 0.0
        if d.get("fill") is not None and _BOX_MIN_AREA_FRAC < frac < _BOX_MAX_AREA_FRAC:
            boxes += 1
        lines += sum(1 for item in d.get("items", [])[:2000] if item and item[0] == "l")
    return PageDrawingStats(
        page_number=int(page.number) + 1, filled_boxes=boxes, line_segments=lines, drawings=len(drawings),
        text_chars=len(page.get_text("text").strip()),
    )


def detect_diagram_pages(pdf_path: Path, page_numbers: list[int], figure_pages: set[int]) -> dict[int, str]:
    """``{page_number: reason}`` for pages to transcribe (``documentai_figure`` and/or ``vector_box_diagram``)."""
    import pymupdf

    out: dict[int, str] = {}
    with pymupdf.open(pdf_path) as doc:
        for number in page_numbers[:500]:
            if not 1 <= number <= len(doc):
                raise ValueError(f"page {number} out of range for {pdf_path.name} ({len(doc)} pages)")
            reasons = ["documentai_figure"] if number in figure_pages else []
            if page_drawing_stats(doc[number - 1]).is_box_diagram:
                reasons.append("vector_box_diagram")
            if reasons:
                out[number] = "+".join(reasons)
    return out


def render_page_png(pdf_path: Path, page_number: int, dpi: int = RENDER_DPI) -> bytes:
    """Render one page to PNG; DPI is reduced so the longest side stays ≤ ``MAX_RENDER_SIDE_PX``."""
    import pymupdf

    if not 36 <= dpi <= 300:
        raise ValueError("dpi must be within [36, 300]")
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= len(doc):
            raise ValueError(f"page {page_number} out of range for {pdf_path.name}")
        page = doc[page_number - 1]
        longest_pt = max(page.rect.width, page.rect.height)
        effective = min(dpi, int(MAX_RENDER_SIDE_PX * 72 / longest_pt))
        return page.get_pixmap(dpi=effective).tobytes("png")


def _clean(value: Any, limit: int = _MAX_LABEL_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _strip_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    return body.strip()


def parse_vision_response(text: str, page_ref: str) -> DiagramSpec:
    """Strictly parse the model's JSON; raise ``VisionTranscriptionError`` with the reason when unusable."""
    if not isinstance(text, str) or not text.strip():
        raise VisionTranscriptionError(f"{page_ref}: vision model returned an empty response")
    try:
        doc = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise VisionTranscriptionError(f"{page_ref}: vision response is not JSON ({exc.msg} at char {exc.pos})") from exc
    if not isinstance(doc, dict):
        raise VisionTranscriptionError(f"{page_ref}: vision response JSON is not an object")
    comps = tuple(
        (_clean(c.get("id"), 20), _clean(c.get("label")), _clean(c.get("shape"), 20) or "other")
        for c in (doc.get("components") or [])[:_MAX_COMPONENTS]
        if isinstance(c, dict) and _clean(c.get("id"), 20) and _clean(c.get("label"))
    )
    if not comps:
        notes = "; ".join(_clean(n, 300) for n in (doc.get("notes") or [])[:2] if _clean(n, 300))
        raise VisionTranscriptionError(
            f"{page_ref}: vision response lists no labelled components" + (f" (model notes: {notes})" if notes else "")
        )
    ids = {c[0] for c in comps}
    conns = tuple(
        (_clean(c.get("from"), 20), _clean(c.get("to"), 20), _clean(c.get("label")), _clean(c.get("style"), 20) or "line")
        for c in (doc.get("connections") or [])[:_MAX_CONNECTIONS]
        if isinstance(c, dict)
    )
    dangling = [c for c in conns if c[0] not in ids or c[1] not in ids]
    if dangling:
        raise VisionTranscriptionError(f"{page_ref}: {len(dangling)} connection(s) reference unknown component ids, e.g. {dangling[0][:2]}")
    regions = tuple(
        (_clean(r.get("label")), tuple(m for m in (_clean(x, 20) for x in (r.get("contains") or [])[:_MAX_COMPONENTS]) if m in ids))
        for r in (doc.get("regions") or [])[:20]
        if isinstance(r, dict) and _clean(r.get("label"))
    )
    notes = tuple(_clean(n, 400) for n in (doc.get("notes") or [])[:20] if _clean(n, 400))
    return DiagramSpec(title=_clean(doc.get("title")), components=comps, connections=conns, regions=regions, notes=notes)


def labels_in_text_layer(spec: DiagramSpec, page_text: str) -> float:
    """Share of transcribed component labels found verbatim (case-insensitive) in the page's PDF text layer."""
    haystack = " ".join(page_text.lower().split())
    found = sum(1 for _, label, _ in spec.components if label.lower() in haystack)
    return round(found / len(spec.components), 4)


class GeminiVisionTranscriber:
    """``generate_content`` with one PNG part + ``VISION_PROMPT`` (JSON mode); bounded retries on 429/5xx only."""

    def __init__(self, *, client: Any, model: str, thinking_level: str = "low", sleep: Callable[[float], None] = time.sleep) -> None:
        if client is None or not hasattr(client, "models"):
            raise TypeError("client must be a google-genai Client (with .models)")
        if not isinstance(model, str) or not model.startswith("gemini-"):
            raise ValueError("model must be a gemini-* model id")
        self.client = client
        self.model = model
        self.thinking_level = thinking_level.upper()
        self._sleep = sleep

    def request_summary(self) -> dict[str, Any]:
        return {"model": self.model, "method": "models.generate_content", "response_mime_type": "application/json",
                "thinking_level": self.thinking_level, "prompt": VISION_PROMPT, "image_mime_type": "image/png",
                "render_dpi": RENDER_DPI, "max_render_side_px": MAX_RENDER_SIDE_PX}

    def _call(self, png: bytes) -> Any:
        from google.genai import types

        return self.client.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=png, mime_type="image/png"), VISION_PROMPT],
            config={"response_mime_type": "application/json", "thinking_config": {"thinking_level": self.thinking_level}},
        )

    def transcribe(self, png: bytes, page_number: int, page_ref: str) -> VisionResult:
        if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(b"\x89PNG"):
            raise ValueError("png must be PNG bytes")
        from app.ingestion.live_backends import is_retryable_error

        start = time.perf_counter()
        for attempt in range(1, MAX_VISION_ATTEMPTS + 1):
            try:
                resp = self._call(bytes(png))
                break
            except Exception as exc:  # classified: only transient errors are retried, everything else re-raises
                if attempt >= MAX_VISION_ATTEMPTS or not is_retryable_error(exc):
                    raise
                self._sleep(_VISION_BACKOFF_SECONDS * attempt)
        spec = parse_vision_response(str(getattr(resp, "text", "") or ""), page_ref)
        meta = getattr(resp, "usage_metadata", None)
        if meta is None:
            raise VisionTranscriptionError(f"{page_ref}: vision response carries no usage_metadata (cost unmeasurable)")
        return VisionResult(
            page_number=page_number, spec=spec, model=self.model,
            prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
            duration_ms=int((time.perf_counter() - start) * 1000), attempts=attempt, image_bytes=len(png),
        )


__all__ = [
    "DIAGRAM_HEURISTIC",
    "DiagramSpec",
    "GeminiVisionTranscriber",
    "MAX_VISION_PAGES",
    "PageDrawingStats",
    "VISION_PROMPT",
    "VisionResult",
    "VisionTranscriptionError",
    "detect_diagram_pages",
    "labels_in_text_layer",
    "page_drawing_stats",
    "parse_vision_response",
    "render_page_png",
]
