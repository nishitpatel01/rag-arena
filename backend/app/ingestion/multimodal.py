"""Level 3 multimodal tier: element-aware chunking + multimodal embedding items (spec §3 Level 3, §5).

Inputs are Document AI layout elements (``layout_documentai``) and Gemini vision transcriptions
(``vision_transcribe``). Rules:

* tables → one atomic chunk each (``element_type='table'``, Markdown), never split;
* diagram pages with a vision transcription → one ``diagram`` chunk (``layout_source='gemini_vision'``), embedded as
  text + the rendered single-page PNG in ONE ``embed_content`` call (one vector);
* paragraphs/lists → grouped under their heading, ≤ ``MAX_NARRATIVE_CHARS`` per chunk, never across pages;
* amendment metadata (doc_type, section_id, amendment_number, amends_section, effective_date) comes from the corpus
  metadata line read on the PDF TEXT LAYER with PyMuPDF (the rendered line runs past the page edge, so Document AI
  sees a truncated ``AMENDS=`` field); ``metadata_source`` is reported in the chunk payload;
* bbox per chunk: Document AI box when returned, else the element located on the PDF text layer
  (``page.search_for``), else the full page — recorded as ``bbox_source``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from app.core.embeddings import DOCUMENT_TASK_TYPE, embed_config, format_document_for_embedding, single_vector
from app.core.models import BoundingBox, DocumentChunk
from app.ingestion.layout_documentai import LAYOUT_SOURCE, DocLayout, LayoutElement
from app.ingestion.parsers import _extract_page_metadata_and_body
from app.ingestion.vision_transcribe import VisionResult

VISION_LAYOUT_SOURCE = "gemini_vision"
METADATA_SOURCE = "pdf_text_layer (corpus metadata line via PyMuPDF, clip=INFINITE_RECT)"
MAX_NARRATIVE_CHARS = 1800
MAX_CHUNKS_PER_DOC = 2000
MAX_ITEMS_PER_BATCH = 32
MAX_EMBED_WORKERS = 8
MAX_EMBED_ATTEMPTS = 3
_EMBED_BACKOFF_SECONDS = 1.0
_SNIPPET_WORDS = 6
_FULL_PAGE = (0.0, 0.0, 1.0, 1.0)


@dataclass(frozen=True)
class PageMeta:
    doc_type: str
    section_id: str | None
    amendment_number: int
    effective_date: str | None
    amends_section: str | None


@dataclass(frozen=True)
class EmbedItem:
    """One embedding input: text (+ optional PNG of the source page for diagram chunks)."""

    text: str
    title: str
    image_png: bytes | None = None


@dataclass(frozen=True)
class MultimodalChunk:
    chunk: DocumentChunk
    item: EmbedItem


def page_metadata(pdf_path: Path, pages: int, *, upload: bool = False) -> dict[int, PageMeta]:
    """Corpus metadata line per page from the PDF text layer (CUAD agreements fall back to the filename rule);
    ``upload=True``: user documents get ``UPLOADED_DOCUMENT`` and no invented dates."""
    import pymupdf

    out: dict[int, PageMeta] = {}
    with pymupdf.open(pdf_path) as doc:
        for idx in range(min(pages, len(doc))):
            text = doc[idx].get_text("text", clip=pymupdf.INFINITE_RECT())
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            doc_type, section, _elem, amendment, effective, amends, _body = _extract_page_metadata_and_body(lines, pdf_path.name, upload=upload)
            out[idx + 1] = PageMeta(doc_type, section, amendment, effective, amends)
    return out


def _snippet(text: str, *, tail: bool) -> str:
    words = text.replace("|", " ").replace("\\", " ").split()
    picked = words[-_SNIPPET_WORDS:] if tail else words[:_SNIPPET_WORDS]
    return " ".join(w for w in picked if w != "---")


def locate_bbox(page: Any, text: str) -> tuple[tuple[float, float, float, float], str]:
    """Locate ``text`` on a PyMuPDF page by its first/last words; (bbox, 'pdf_text_layer') or (full page, 'page')."""
    width, height = float(page.rect.width), float(page.rect.height)
    rects: list[Any] = []
    for tail in (False, True):
        snippet = _snippet(text, tail=tail)
        if len(snippet) < 4:
            continue
        hits = page.search_for(snippet)[:4]
        if hits:
            rects.append(hits[0] if not tail else hits[-1])
    if not rects:
        return _FULL_PAGE, "page"
    x0 = max(0.0, min(r.x0 for r in rects) / width)
    y0 = max(0.0, min(r.y0 for r in rects) / height)
    x1 = min(1.0, max(r.x1 for r in rects) / width)
    y1 = min(1.0, max(r.y1 for r in rects) / height)
    if x1 <= x0 or y1 <= y0:
        return _FULL_PAGE, "page"
    return (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)), "pdf_text_layer"


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


@dataclass(frozen=True)
class _Group:
    kind: str  # narrative_text | table | diagram
    heading: str
    texts: tuple[str, ...]
    elements: tuple[LayoutElement, ...]


def _section_key(el: LayoutElement) -> str:
    """A heading opens its own section (its text); other elements belong to their enclosing heading."""
    return el.text if el.kind == "heading" else el.heading


def group_page_elements(elements: list[LayoutElement]) -> list[_Group]:
    """Reading-order grouping for one page: tables atomic; a heading + its paragraphs/lists grouped ≤ MAX_NARRATIVE_CHARS
    (a single oversized paragraph is kept whole, never cut mid-sentence)."""
    groups: list[_Group] = []
    buf: list[LayoutElement] = []

    def flush() -> None:
        if buf:
            groups.append(_Group("narrative_text", _section_key(buf[0]), tuple(e.text for e in buf), tuple(buf)))
            buf.clear()

    for el in elements:
        if el.kind == "figure":
            continue  # figures are represented by the page's vision transcription (diagram chunk)
        if el.kind == "table":
            flush()
            groups.append(_Group("table", el.heading, (el.text,), (el,)))
            continue
        only_headings = all(e.kind == "heading" for e in buf)
        size = sum(len(e.text) + 1 for e in buf)
        if buf and (el.kind == "heading" or _section_key(el) != _section_key(buf[0]) or (size + len(el.text) > MAX_NARRATIVE_CHARS and not only_headings)):
            flush()
        buf.append(el)
    flush()
    return groups


class ChunkBuilder:
    """Builds ``MultimodalChunk``s for one document (bounded) with bbox located on the open PyMuPDF document."""

    def __init__(self, *, pdf_path: Path, solicitation_id: str, metas: dict[int, PageMeta]) -> None:
        self.pdf_path = pdf_path
        self.solicitation_id = solicitation_id
        self.metas = metas
        stem = pdf_path.stem.lower()
        self._prefix = f"{solicitation_id}_{stem}"
        self.bbox_sources: dict[str, int] = {}

    def _bbox(self, page: Any, group: _Group) -> tuple[BoundingBox, str]:
        boxes: list[tuple[float, float, float, float]] = []
        sources: list[str] = []
        for el in group.elements:
            if el.bbox is not None:
                boxes.append(el.bbox)
                sources.append(el.bbox_source or "documentai")
                continue
            box, source = locate_bbox(page, el.text)
            if source != "page":
                boxes.append(box)
                sources.append(source)
        if not boxes:
            box, source = _FULL_PAGE, "page"
        else:
            box, source = _union(boxes), ("documentai" if all(s == "documentai" for s in sources) else "pdf_text_layer")
        self.bbox_sources[source] = self.bbox_sources.get(source, 0) + 1
        number = int(page.number) + 1
        return BoundingBox(page=number, x0=box[0], y0=box[1], x1=box[2], y1=box[3]), source

    def _chunk(self, page_number: int, idx: int, kind: str, content: str, bbox: BoundingBox, bbox_source: str, layout_source: str) -> DocumentChunk:
        meta = self.metas.get(page_number) or PageMeta("COMMERCIAL_AGREEMENT", None, 0, None, None)
        return DocumentChunk(
            chunk_id=f"{self._prefix}_p{page_number}_e{idx}_multimodal", tier_profile="multimodal",
            solicitation_id=self.solicitation_id, doc_name=self.pdf_path.name, doc_type=meta.doc_type, element_type=kind,
            section_id=meta.section_id, amendment_number=meta.amendment_number, effective_date=meta.effective_date,
            amends_section=meta.amends_section, page_number=page_number, bbox=bbox, content=content,
            score=1.0, layout_source=layout_source, bbox_source=bbox_source,
        )

    def build(self, layout: DocLayout, vision: dict[int, VisionResult], page_png: Callable[[int], bytes]) -> list[MultimodalChunk]:
        import pymupdf

        out: list[MultimodalChunk] = []
        with pymupdf.open(self.pdf_path) as doc:
            for number in range(1, layout.pages + 1):
                page = doc[number - 1]
                page_elements = [e for e in layout.elements if e.page_number == number]
                for idx, group in enumerate(group_page_elements(page_elements), start=1):
                    bbox, source = self._bbox(page, group)
                    body = "\n".join(t for t in group.texts if t)
                    content = f"{group.heading}\n{body}" if group.heading and not body.startswith(group.heading) else body
                    chunk = self._chunk(number, idx, group.kind, content, bbox, source, LAYOUT_SOURCE)
                    out.append(MultimodalChunk(chunk, EmbedItem(text=content, title=group.heading or self.pdf_path.stem)))
                if number in vision:
                    out.append(self._diagram(page, number, page_elements, vision[number], page_png))
                if len(out) > MAX_CHUNKS_PER_DOC:
                    raise ValueError(f"{self.pdf_path.name} produced more than {MAX_CHUNKS_PER_DOC} chunks")
        return out

    def _diagram(self, page: Any, number: int, elements: list[LayoutElement], result: VisionResult, page_png: Callable[[int], bytes]) -> MultimodalChunk:
        figures = [e for e in elements if e.kind == "figure"]
        heading = figures[0].heading if figures else next((e.heading for e in elements if e.heading), "")
        if figures and figures[0].bbox is not None:
            box, source = figures[0].bbox, figures[0].bbox_source or "documentai"
        else:
            box, source = _FULL_PAGE, "page"  # vision transcribed the whole rendered page
        self.bbox_sources[source] = self.bbox_sources.get(source, 0) + 1
        bbox = BoundingBox(page=number, x0=box[0], y0=box[1], x1=box[2], y1=box[3])
        text = f"[DIAGRAM transcription · {result.model}]\n{result.spec.to_text()}"
        content = f"{heading}\n{text}" if heading else text
        chunk = self._chunk(number, 0, "diagram", content, bbox, source, VISION_LAYOUT_SOURCE)
        return MultimodalChunk(chunk, EmbedItem(text=content, title=heading or self.pdf_path.stem, image_png=page_png(number)))


class VertexMultimodalEmbedder:
    """gemini-embedding-2 on Vertex AI, RETRIEVAL_DOCUMENT side; ONE ``embed_content`` call per item.

    Text items send one formatted string; diagram items send ONE ``Content`` with two parts (formatted text + PNG
    image) so the model returns a single fused vector. (A list of strings would also be aggregated into one vector by
    Vertex, and a list of Content objects is rejected — see ``app.core.embeddings``.)
    """

    def __init__(self, *, client: Any, model: str, max_workers: int = MAX_EMBED_WORKERS, sleep: Callable[[float], None] = time.sleep) -> None:
        if client is None or not hasattr(client, "models"):
            raise TypeError("client must be a google-genai Client (with .models)")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        self.client = client
        self.model = model.strip()
        self.task_type = DOCUMENT_TASK_TYPE
        self.input_format = "text: 'title: <heading> | text: <chunk>'; diagram: Content[parts=[that text, image/png page]]"
        self._max_workers = max(1, min(32, int(max_workers)))
        self._sleep = sleep

    @staticmethod
    def contents_for(item: EmbedItem) -> Any:
        text = format_document_for_embedding(item.text, title=item.title)
        if item.image_png is None:
            return text
        from google.genai import types

        return types.Content(role="user", parts=[types.Part.from_text(text=text), types.Part.from_bytes(data=item.image_png, mime_type="image/png")])

    def embed_items(self, items: list[EmbedItem]) -> list[list[float]]:
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS_PER_BATCH:
            raise ValueError(f"embed_items needs 1..{MAX_ITEMS_PER_BATCH} items")
        config = embed_config(self.task_type)
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(items)), thread_name_prefix="mm-embed") as pool:
            vectors = list(pool.map(lambda it: self._embed_one(it, config), items))
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("inconsistent embedding dimensionality in multimodal batch")
        return vectors

    def _embed_one(self, item: EmbedItem, config: Any) -> list[float]:
        from app.ingestion.live_backends import is_retryable_error

        contents = self.contents_for(item)
        for attempt in range(MAX_EMBED_ATTEMPTS):
            try:
                return single_vector(self.client.models.embed_content(model=self.model, contents=contents, config=config))
            except Exception as exc:  # classified: transient → bounded retry, otherwise re-raise
                if attempt + 1 >= MAX_EMBED_ATTEMPTS or not is_retryable_error(exc):
                    raise
                self._sleep(_EMBED_BACKOFF_SECONDS * (2 ** attempt))
        raise RuntimeError("unreachable: multimodal embed retry loop exited without a result")


__all__ = [
    "ChunkBuilder",
    "EmbedItem",
    "METADATA_SOURCE",
    "MultimodalChunk",
    "PageMeta",
    "VISION_LAYOUT_SOURCE",
    "VertexMultimodalEmbedder",
    "group_page_elements",
    "locate_bbox",
    "page_metadata",
]
