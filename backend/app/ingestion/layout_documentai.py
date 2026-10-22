"""Level 3 ingestion: Document AI Layout Parser (online ``:process``) → layout elements (spec §3 Level 3, §5).

* Raw PDF bytes are sent online; PDFs longer than the documented online limit (15 pages) are split into contiguous
  page ranges with PyMuPDF (bounded: ≤ ``MAX_PAGES_PER_DOC`` pages per document, ranges of ``ONLINE_PAGE_LIMIT``).
* ``processOptions.layoutConfig``: ``returnBoundingBoxes``, ``enableTableAnnotation``, ``enableImageAnnotation`` on;
  no ``chunkingConfig`` — the multimodal chunker in ``app.ingestion.multimodal`` chunks the elements itself.
* ``documentLayout.blocks`` → ``LayoutElement``: headings set the section context, paragraphs/lists are text
  elements, tables become Markdown with a header row (``headerRows[0]``; if the parser returned none, the first body
  row is used and ``header_inferred`` is set), ``imageBlock`` → ``figure`` (marks a diagram candidate page).
  ``header``/``footer`` blocks and the corpus metadata banner line are skipped and counted.
* Bounding boxes: ``boundingBox.normalizedVertices`` → page-relative (x0, y0, x1, y1) in [0, 1] with
  ``bbox_source='documentai'``. Measured 2026-10-22: the Gemini layout parser versions (v1.5 / v1.6-pro) return NO
  bounding boxes even with ``returnBoundingBoxes``; those elements carry ``bbox=None`` and are located later on the
  PDF text layer (``app.ingestion.multimodal.locate_bbox``), never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any

from app.ingestion.documentai_client import DocumentAIClient

LAYOUT_SOURCE = "documentai_layout"
ONLINE_PAGE_LIMIT = 15
MAX_PAGES_PER_DOC = 30  # same per-document page cap as the naive/hybrid tiers (parsers._MAX_PARSE_PAGES)
MAX_BLOCKS_PER_REQUEST = 20_000
MAX_BLOCK_DEPTH = 16
PROCESS_OPTIONS: dict[str, Any] = {
    "layoutConfig": {"returnBoundingBoxes": True, "enableTableAnnotation": True, "enableImageAnnotation": True}
}
_HEADING_TYPES = frozenset({"title", "subtitle", "heading-1", "heading-2", "heading-3", "heading-4", "heading-5"})
_SKIPPED_TYPES = frozenset({"header", "footer"})
_BANNER_RE = re.compile(r"DOC_TYPE=[A-Z_]+\s*\|\s*SECTION=")
_MAX_CELL_CHARS = 2000


@dataclass(frozen=True)
class LayoutElement:
    """One parsed element of a page, in reading order."""

    page_number: int
    kind: str  # heading | paragraph | list | table | figure
    text: str
    heading: str
    block_id: str
    bbox: tuple[float, float, float, float] | None = None
    bbox_source: str = ""
    header_inferred: bool = False
    rows: int = 0
    cols: int = 0


@dataclass(frozen=True)
class PageLayoutStats:
    page_number: int
    blocks: int
    tables: int
    figures: int


@dataclass(frozen=True)
class DocLayout:
    """Layout Parser result for one PDF (all page ranges merged, absolute page numbers)."""

    doc_name: str
    pages: int
    elements: tuple[LayoutElement, ...]
    page_stats: tuple[PageLayoutStats, ...]
    requests: int
    attempts: int
    docai_ms: int
    skipped_blocks: dict[str, int] = field(default_factory=dict)

    @property
    def figure_pages(self) -> set[int]:
        return {e.page_number for e in self.elements if e.kind == "figure"}


def normalized_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """``boundingBox.normalizedVertices`` → clamped (x0, y0, x1, y1); None when the parser returned no box."""
    vertices = ((block.get("boundingBox") or {}).get("normalizedVertices")) or []
    if len(vertices) < 2:
        return None
    xs = [min(1.0, max(0.0, float(v.get("x", 0.0) or 0.0))) for v in vertices[:8]]
    ys = [min(1.0, max(0.0, float(v.get("y", 0.0) or 0.0))) for v in vertices[:8]]
    box = (round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4))
    return box if box[2] > box[0] and box[3] > box[1] else None


def _block_text(block: dict[str, Any], budget: list[int]) -> str:
    """Concatenated text of a block subtree (cells and list entries); bounded by a shared node budget."""
    parts: list[str] = []
    stack = [block]
    while stack:
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError(f"Document AI block tree exceeds {MAX_BLOCKS_PER_REQUEST} nodes")
        node = stack.pop()
        if "textBlock" in node:
            tb = node["textBlock"]
            parts.append(str(tb.get("text", "") or ""))
            stack.extend(reversed(tb.get("blocks", []) or []))
        elif "listBlock" in node:
            for entry in reversed(node["listBlock"].get("listEntries", []) or []):
                stack.extend(reversed(entry.get("blocks", []) or []))
    return " ".join(" ".join(parts).split())


def _cell(text: str) -> str:
    return text.replace("|", "\\|")[:_MAX_CELL_CHARS]


def table_to_markdown(table: dict[str, Any], budget: list[int]) -> tuple[str, bool, int, int]:
    """``LayoutTableBlock`` → (markdown, header_inferred, body_rows, cols); colSpan cells are padded."""
    def row_cells(row: dict[str, Any]) -> list[str]:
        cells: list[str] = []
        for cell in (row.get("cells", []) or [])[:200]:
            text = " ".join(_block_text(b, budget) for b in cell.get("blocks", []) or [])
            cells.append(_cell(text))
            cells.extend([""] * (max(1, int(cell.get("colSpan", 1) or 1)) - 1))
        return cells

    header_rows = [row_cells(r) for r in (table.get("headerRows", []) or [])[:5]]
    body_rows = [row_cells(r) for r in (table.get("bodyRows", []) or [])[:500]]
    inferred = not header_rows
    if inferred:
        if not body_rows:
            raise ValueError("Document AI table block has neither header nor body rows")
        header_rows, body_rows = [body_rows[0]], body_rows[1:]
    header = header_rows[0]
    cols = max(len(r) for r in [header, *body_rows])
    pad = lambda r: r + [""] * (cols - len(r))  # noqa: E731
    lines = ["| " + " | ".join(pad(header)) + " |", "| " + " | ".join(["---"] * cols) + " |"]
    lines += ["| " + " | ".join(pad(r)) + " |" for r in body_rows]
    caption = str(table.get("caption", "") or "").strip()
    return ((f"{caption}\n" if caption else "") + "\n".join(lines), inferred, len(body_rows), cols)


def _element(block: dict[str, Any], kind: str, text: str, heading: str, offset: int, **extra: Any) -> LayoutElement:
    page = int((block.get("pageSpan") or {}).get("pageStart", 1) or 1) + offset
    box = normalized_bbox(block)
    return LayoutElement(
        page_number=page, kind=kind, text=text, heading=heading, block_id=str(block.get("blockId", "")),
        bbox=box, bbox_source="documentai" if box else "", **extra,
    )


def blocks_to_elements(blocks: list[dict[str, Any]], page_offset: int = 0) -> tuple[list[LayoutElement], dict[str, int]]:
    """Walk ``documentLayout.blocks`` depth-first with an explicit, bounded stack (no recursion)."""
    elements: list[LayoutElement] = []
    skipped = {"header_footer": 0, "metadata_banner": 0, "empty": 0}
    budget = [MAX_BLOCKS_PER_REQUEST]
    stack: list[tuple[dict[str, Any], str, int]] = [(b, "", 0) for b in reversed(blocks or [])]
    while stack:
        block, heading, depth = stack.pop()
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_BLOCK_DEPTH:
            raise ValueError(f"Document AI layout exceeds bounds ({MAX_BLOCKS_PER_REQUEST} blocks / depth {MAX_BLOCK_DEPTH})")
        if "tableBlock" in block:
            md, inferred, rows, cols = table_to_markdown(block["tableBlock"], budget)
            elements.append(_element(block, "table", md, heading, page_offset, header_inferred=inferred, rows=rows, cols=cols))
        elif "imageBlock" in block:
            text = " ".join(str(block["imageBlock"].get("imageText", "") or "").split())
            elements.append(_element(block, "figure", text, heading, page_offset))
        elif "listBlock" in block:
            entries = [_block_text(b, budget) for e in block["listBlock"].get("listEntries", []) or [] for b in e.get("blocks", []) or []]
            text = "\n".join(f"- {t}" for t in entries if t)
            if text:
                elements.append(_element(block, "list", text, heading, page_offset))
            else:
                skipped["empty"] += 1
        elif "textBlock" in block:
            heading = _visit_text_block(block, heading, depth, page_offset, elements, skipped, stack)
    return elements, skipped


def _visit_text_block(
    block: dict[str, Any], heading: str, depth: int, offset: int,
    elements: list[LayoutElement], skipped: dict[str, int], stack: list[tuple[dict[str, Any], str, int]],
) -> str:
    """Emit one text block; headings become the context of their children. Returns the context for siblings."""
    tb = block["textBlock"]
    kind = str(tb.get("type", "paragraph") or "paragraph")
    text = " ".join(str(tb.get("text", "") or "").split())
    child_heading = heading
    if kind in _SKIPPED_TYPES:
        skipped["header_footer"] += 1
    elif _BANNER_RE.search(text):
        skipped["metadata_banner"] += 1
    elif not text:
        skipped["empty"] += 1
    elif kind in _HEADING_TYPES:
        elements.append(_element(block, "heading", text, heading, offset))
        child_heading = text
    else:
        elements.append(_element(block, "paragraph", text, heading, offset))
    stack.extend((child, child_heading, depth + 1) for child in reversed(tb.get("blocks", []) or []))
    return heading


def split_pdf_ranges(pdf_bytes: bytes, *, range_size: int = ONLINE_PAGE_LIMIT, max_pages: int = MAX_PAGES_PER_DOC) -> tuple[int, list[tuple[int, bytes]]]:
    """(pages_kept, [(first_page_number, sub_pdf_bytes), ...]) — contiguous ranges of ≤ ``range_size`` pages."""
    import pymupdf

    if not 1 <= range_size <= ONLINE_PAGE_LIMIT:
        raise ValueError(f"range_size must be within [1, {ONLINE_PAGE_LIMIT}] (Layout Parser online limit)")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as src:
        kept = min(len(src), max_pages)
        if kept == 0:
            raise ValueError("PDF has no pages")
        if len(src) <= range_size:
            return kept, [(1, bytes(pdf_bytes))]
        ranges: list[tuple[int, bytes]] = []
        for start in range(0, kept, range_size):
            out = pymupdf.open()
            out.insert_pdf(src, from_page=start, to_page=min(start + range_size, kept) - 1)
            ranges.append((start + 1, out.tobytes()))
            out.close()
        return kept, ranges


def page_stats(elements: list[LayoutElement], pages: int) -> tuple[PageLayoutStats, ...]:
    return tuple(
        PageLayoutStats(
            page_number=p,
            blocks=sum(1 for e in elements if e.page_number == p),
            tables=sum(1 for e in elements if e.page_number == p and e.kind == "table"),
            figures=sum(1 for e in elements if e.page_number == p and e.kind == "figure"),
        )
        for p in range(1, pages + 1)
    )


class LayoutParser:
    """Runs the configured Layout Parser processor (and optional processor version) over one PDF."""

    def __init__(self, *, client: DocumentAIClient, processor_id: str, processor_version: str = "") -> None:
        if not isinstance(client, DocumentAIClient):
            raise TypeError("client must be a DocumentAIClient")
        self.client = client
        self.processor_id = processor_id
        self.processor_version = processor_version
        client.process_url(processor_id, processor_version)  # validates identifiers up front

    def request_summary(self) -> dict[str, Any]:
        """Redacted request description for stage payloads (no project id/number, short processor id only)."""
        return {
            "method": "processors.process" if not self.processor_version else "processorVersions.process",
            "location": self.client.location, "processor_id": self.processor_id,
            "processor_version": self.processor_version or "(processor default)",
            "process_options": PROCESS_OPTIONS, "online_page_limit": ONLINE_PAGE_LIMIT, "max_pages_per_doc": MAX_PAGES_PER_DOC,
        }

    def parse(self, doc_name: str, pdf_bytes: bytes) -> DocLayout:
        """Process every page range online; any API error propagates (no PyMuPDF fallback)."""
        pages, ranges = split_pdf_ranges(pdf_bytes)
        elements: list[LayoutElement] = []
        skipped_total: dict[str, int] = {}
        attempts = 0
        start = time.perf_counter()
        for first_page, chunk in ranges:
            document, tries = self.client.process_pdf(self.processor_id, chunk, PROCESS_OPTIONS, self.processor_version)
            attempts += tries
            blocks = (document.get("documentLayout") or {}).get("blocks") or []
            found, skipped = blocks_to_elements(blocks, page_offset=first_page - 1)
            elements.extend(found)
            for key, value in skipped.items():
                skipped_total[key] = skipped_total.get(key, 0) + value
        if not elements:
            raise ValueError(f"Document AI Layout Parser returned no elements for {doc_name} ({pages} pages)")
        return DocLayout(
            doc_name=doc_name, pages=pages, elements=tuple(elements), page_stats=page_stats(elements, pages),
            requests=len(ranges), attempts=attempts, docai_ms=int((time.perf_counter() - start) * 1000),
            skipped_blocks=skipped_total,
        )


__all__ = [
    "DocLayout",
    "LAYOUT_SOURCE",
    "LayoutElement",
    "LayoutParser",
    "ONLINE_PAGE_LIMIT",
    "PROCESS_OPTIONS",
    "PageLayoutStats",
    "blocks_to_elements",
    "normalized_bbox",
    "page_stats",
    "split_pdf_ranges",
    "table_to_markdown",
]
