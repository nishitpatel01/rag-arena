"""Multi-tier PDF parser supporting both PyMuPDF (``pymupdf``) and pure-Python PDF 1.4 stream extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re

from app.core.models import BoundingBox, DocumentChunk

_VALID_TIERS = ("naive", "hybrid", "multimodal")
_MAX_PARSE_PAGES = 30
_MAX_STREAM_MATCHES = 60
_STREAM_PATTERN = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_TJ_TEXT_PATTERN = re.compile(r"\(((?:\\.|[^\\()])*)\)\s*Tj")
_META_LINE_PATTERN = re.compile(
    r"DOC_TYPE=(?P<doc_type>[A-Z_]+)\s*\|\s*SECTION=(?P<section_id>[^|]+?)\s*"
    r"\|\s*ELEMENT=(?P<element_type>[a-z_]+)\s*\|\s*AMENDMENT=(?P<amendment>\d+)\s*"
    r"\|\s*EFFECTIVE=(?P<effective>[0-9-]+)\s*\|\s*AMENDS=(?P<amends>[^\s|]+)"
)
_CLAUSE_REF_PATTERN = re.compile(r"\b(?:CLIN-\d{4}(?:-[A-Z]{2})?|SEC-\d+\.\d+(?:\.\d+)?|DATA-SLA-\d+\.\d+)\b")


def _unescape_pdf_string(raw: str) -> str:
    """Unescape PDF literal string sequences."""
    return raw.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")


_GENERATED_DOC_PREFIX = re.compile(r"^\d{2}_")


def _infer_doc_type_from_filename(filename: str) -> tuple[str, int]:
    """Infer document type and amendment number from filename (page metadata banners override this).

    Only the generated solicitation documents carry the ``NN_`` prefix; every other corpus PDF is a third-party
    commercial agreement (CUAD) and is labelled ``COMMERCIAL_AGREEMENT`` — never as an RFP or an amendment.
    """
    lower = filename.lower()
    if not _GENERATED_DOC_PREFIX.match(filename):
        return "COMMERCIAL_AGREEMENT", 0
    if "amendment_0002" in lower:
        return "AMENDMENT", 2
    if "amendment" in lower or "addenda" in lower:
        return "AMENDMENT", 1
    if "msa" in lower or "master_services" in lower:
        return "MSA", 1
    if "pricing" in lower or "clin" in lower:
        return "PRICING_CLIN", 0
    if "exhibit" in lower or "architecture" in lower or "zero_trust" in lower:
        return "TECHNICAL_EXHIBIT", 0
    if "sla" in lower or "compliance" in lower or "incident" in lower:
        return "SLA_MATRIX", 0
    if "sow" in lower or "statement_of_work" in lower:
        return "SOW", 0
    if "proposal" in lower:
        return "VENDOR_PROPOSAL", 0
    return "BASE_RFP", 0


def _import_pymupdf() -> Any | None:
    """Return the ``pymupdf`` module, or None when it is not installed (backend choice is explicit, never silent)."""
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pymupdf


def pymupdf_available() -> bool:
    """True when PyMuPDF (``pymupdf``) is importable; decides the parser backend explicitly (no silent fallback)."""
    return _import_pymupdf() is not None


def parser_backend_name() -> str:
    """Name recorded as ``layout_source`` for text parsing: ``pymupdf`` or ``pdf_stream_regex``."""
    return "pymupdf" if pymupdf_available() else "pdf_stream_regex"


def _extract_pages_with_pymupdf(raw_bytes: bytes) -> list[list[str]]:
    pymupdf = _import_pymupdf()
    if pymupdf is None:
        raise RuntimeError("PyMuPDF is not installed")
    pages: list[list[str]] = []
    with pymupdf.open(stream=raw_bytes, filetype="pdf") as doc:
        for idx in range(min(len(doc), _MAX_PARSE_PAGES)):
            # INFINITE_RECT: the corpus generator writes metadata banners past the right page edge (x1≈631 > 612);
            # PyMuPDF's default mediabox clip silently drops their tail (e.g. "AMENDS=NONE" → "AMENDS=").
            text = doc[idx].get_text("text", clip=pymupdf.INFINITE_RECT())
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if lines:
                pages.append(lines)
    return pages


def is_genuinely_blank_pdf(pdf_path: Path) -> bool:
    """True only when every page has no text, no images and no vector drawings (needs PyMuPDF; bounded pages)."""
    pymupdf = _import_pymupdf()
    if pymupdf is None:
        raise RuntimeError("PyMuPDF is required to decide whether a PDF is genuinely blank")
    with pymupdf.open(pdf_path) as doc:
        for idx in range(min(len(doc), _MAX_PARSE_PAGES)):
            page = doc[idx]
            if page.get_text("text").strip() or page.get_images() or page.get_drawings():
                return False
    return True


def _extract_pages_with_stream_regex(raw_bytes: bytes) -> list[list[str]]:
    pages_fallback: list[list[str]] = []
    for match in list(_STREAM_PATTERN.finditer(raw_bytes))[:_MAX_STREAM_MATCHES]:
        stream_str = match.group(1).decode("latin-1", errors="replace")
        if "BT" not in stream_str:
            continue
        lines = [_unescape_pdf_string(m.group(1)).strip() for m in _TJ_TEXT_PATTERN.finditer(stream_str)]
        clean_lines = [ln for ln in lines if ln]
        if clean_lines:
            pages_fallback.append(clean_lines)
        if len(pages_fallback) >= _MAX_PARSE_PAGES:
            break
    return pages_fallback


def _extract_pages_lines(raw_bytes: bytes) -> list[list[str]]:
    """Extract text lines per page with the explicitly selected backend (see ``parser_backend_name``)."""
    if pymupdf_available():
        return _extract_pages_with_pymupdf(raw_bytes)
    return _extract_pages_with_stream_regex(raw_bytes)


def extract_pdf_pages(pdf_path: Path) -> list[list[str]]:
    """Public parse step for ingestion jobs: validated PDF → per-page text lines (bounded pages)."""
    if not isinstance(pdf_path, Path):
        raise TypeError("pdf_path must be a pathlib.Path instance")
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    raw_bytes = pdf_path.read_bytes()
    if not raw_bytes.startswith(b"%PDF-"):
        raise ValueError(f"File {pdf_path.name} does not start with %PDF- magic header")
    return _extract_pages_lines(raw_bytes)


def chunk_pages(
    pages_lines: list[list[str]],
    *,
    pdf_name: str,
    solicitation_id: str,
    tier_profile: str,
) -> list[DocumentChunk]:
    """Public chunk step for ingestion jobs: per-page lines → tier-specific DocumentChunks."""
    if not isinstance(pages_lines, list):
        raise TypeError("pages_lines must be a list of per-page line lists")
    if not isinstance(pdf_name, str) or not pdf_name.strip():
        raise ValueError("pdf_name must be a non-empty string")
    if not isinstance(solicitation_id, str) or not solicitation_id.strip():
        raise ValueError("solicitation_id must be a non-empty string")
    if tier_profile not in _VALID_TIERS:
        raise ValueError(f"tier_profile must be one of {_VALID_TIERS}, got '{tier_profile}'")
    chunks: list[DocumentChunk] = []
    for page_idx, lines in enumerate(pages_lines[:_MAX_PARSE_PAGES], start=1):
        chunks.extend(
            _build_chunks_from_page_lines(
                lines=lines,
                page_num=page_idx,
                pdf_name=pdf_name,
                solicitation_id=solicitation_id.strip(),
                tier_profile=tier_profile,
            )
        )
    return chunks


_NAIVE_WINDOW_CHARS = 360
_NAIVE_OVERLAP_CHARS = 60
_MAX_WINDOWS_PER_PAGE = 20


def _split_naive_windows(
    text: str,
    max_chars: int = _NAIVE_WINDOW_CHARS,
    overlap: int = _NAIVE_OVERLAP_CHARS,
) -> list[str]:
    """Flatten Markdown table pipes and split page text into bounded fixed-size sliding windows."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_chars <= overlap or overlap < 0:
        raise ValueError("max_chars must be greater than overlap >= 0")

    flat_text = " ".join(text.replace("|", " ").split())
    if not flat_text:
        return [text.strip() or "EMPTY_PAGE"]
    if len(flat_text) <= max_chars:
        return [flat_text]

    step = max_chars - overlap
    windows: list[str] = []
    start = 0
    for _ in range(_MAX_WINDOWS_PER_PAGE):
        window = flat_text[start : start + max_chars].strip()
        if window:
            windows.append(window)
        if start + max_chars >= len(flat_text):
            break
        start += step
    return windows


UPLOAD_DOC_TYPE = "UPLOADED_DOCUMENT"


def _extract_page_metadata_and_body(
    lines: list[str],
    pdf_name: str,
    *,
    upload: bool = False,
) -> tuple[str, str | None, str, int, str | None, str | None, str]:
    """Extract procurement metadata banner and clean body text from page lines.

    ``upload=True`` (user documents, v1.1): no corpus filename rules and no default effective date — the type is
    ``UPLOADED_DOCUMENT`` and dates/amendments are only what a metadata banner on the page states.
    """
    if not isinstance(lines, list) or not isinstance(pdf_name, str):
        raise TypeError("lines must be a list and pdf_name must be a string")

    doc_type, amendment_num = (UPLOAD_DOC_TYPE, 0) if upload else _infer_doc_type_from_filename(pdf_name)
    section_id: str | None = None
    element_type = "narrative_text"
    effective_date: str | None = None if upload else "2026-01-15"
    amends_section: str | None = None
    body_lines: list[str] = []

    for line in lines[:120]:
        meta_match = _META_LINE_PATTERN.search(line)
        if meta_match:
            doc_type = meta_match.group("doc_type").strip()
            section_id = meta_match.group("section_id").strip()
            element_type = meta_match.group("element_type").strip()
            amendment_num = int(meta_match.group("amendment"))
            effective_date = meta_match.group("effective").strip()
            raw_amends = meta_match.group("amends").strip()
            amends_section = None if raw_amends == "NONE" else raw_amends
            continue
        if line.startswith("Page ") and "Verified Enterprise Procurement Record" in line:
            continue
        body_lines.append(line)

    content_text = "\n".join(body_lines)
    if amendment_num > 0 and not amends_section:
        refs = sorted(set(_CLAUSE_REF_PATTERN.findall(content_text)))
        amends_section = ",".join(refs) if refs else None

    return (
        doc_type,
        section_id,
        element_type,
        amendment_num,
        effective_date,
        amends_section,
        content_text,
    )


def _build_chunks_from_page_lines(
    lines: list[str],
    page_num: int,
    pdf_name: str,
    solicitation_id: str,
    tier_profile: str,
) -> list[DocumentChunk]:
    """Construct tier-specific DocumentChunk(s) from extracted page lines."""
    if page_num < 1 or tier_profile not in _VALID_TIERS:
        raise ValueError(f"Invalid page_num ({page_num}) or tier_profile ({tier_profile})")

    (
        doc_type,
        section_id,
        element_type,
        amendment_num,
        effective_date,
        amends_section,
        content_text,
    ) = _extract_page_metadata_and_body(lines, pdf_name, upload=solicitation_id.startswith("upl_"))
    stem = pdf_name[:-4].lower() if pdf_name.lower().endswith(".pdf") else pdf_name.lower()

    if tier_profile == "naive":
        windows = _split_naive_windows(content_text)
        total_win = max(len(windows), 1)
        naive_chunks: list[DocumentChunk] = []
        for win_idx, win_text in enumerate(windows, start=1):
            y_start = round(0.12 + 0.70 * ((win_idx - 1) / total_win), 4)
            y_end = round(min(0.92, y_start + (0.70 / total_win) + 0.05), 4)
            naive_chunks.append(
                DocumentChunk(
                    chunk_id=f"{solicitation_id}_{stem}_p{page_num}_w{win_idx}_naive",
                    tier_profile="naive",
                    solicitation_id=solicitation_id,
                    doc_name=pdf_name,
                    doc_type=doc_type,
                    element_type="narrative_text",
                    section_id=section_id,
                    amendment_number=amendment_num,
                    effective_date=effective_date,
                    amends_section=amends_section,
                    page_number=page_num,
                    bbox=BoundingBox(page=page_num, x0=0.06, y0=y_start, x1=0.94, y1=y_end),
                    content=win_text,
                    score=1.0,
                )
            )
        return naive_chunks

    if tier_profile == "hybrid":
        hybrid_elem = "narrative_text" if element_type == "diagram" else element_type
        lineage = (
            f"[Contextual Lineage: Solicitation={solicitation_id} | Document={pdf_name} "
            f"| DocType={doc_type} | Section={section_id or 'GENERAL'}]"
        )
        return [
            DocumentChunk(
                chunk_id=f"{solicitation_id}_{stem}_p{page_num}_hybrid",
                tier_profile="hybrid",
                solicitation_id=solicitation_id,
                doc_name=pdf_name,
                doc_type=doc_type,
                element_type=hybrid_elem,
                section_id=section_id,
                amendment_number=amendment_num,
                effective_date=effective_date,
                amends_section=amends_section,
                page_number=page_num,
                bbox=BoundingBox(page=page_num, x0=0.06, y0=0.14, x1=0.94, y1=0.86),
                content=f"{lineage}\n{content_text}",
                score=1.0,
            )
        ]

    mm_content = (
        f"[Multimodal Diagram Topology | Visual Elements Extracted]\n{content_text}"
        if element_type == "diagram"
        else content_text
    )
    bbox = BoundingBox(
        page=page_num,
        x0=0.06,
        y0=0.14 if element_type != "diagram" else 0.22,
        x1=0.94,
        y1=0.86 if element_type != "diagram" else 0.78,
    )
    return [
        DocumentChunk(
            chunk_id=f"{solicitation_id}_{stem}_p{page_num}_multimodal",
            tier_profile="multimodal",
            solicitation_id=solicitation_id,
            doc_name=pdf_name,
            doc_type=doc_type,
            element_type=element_type,
            section_id=section_id,
            amendment_number=amendment_num,
            effective_date=effective_date,
            amends_section=amends_section,
            page_number=page_num,
            bbox=bbox,
            content=mm_content,
            score=1.0,
        )
    ]


def parse_pdf_Fallback_or_fitz(
    pdf_path: Path,
    solicitation_id: str,
    tier_profile: str = "naive",
) -> list[DocumentChunk]:
    """Parse a procurement PDF into structured DocumentChunks with normalized bounding boxes."""
    if not isinstance(pdf_path, Path):
        raise TypeError("pdf_path must be a pathlib.Path instance")
    if not pdf_path.exists() or not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if not isinstance(solicitation_id, str) or not solicitation_id.strip():
        raise ValueError("solicitation_id must be a non-empty string")
    if tier_profile not in _VALID_TIERS:
        raise ValueError(f"tier_profile must be one of {_VALID_TIERS}, got '{tier_profile}'")

    raw_bytes = pdf_path.read_bytes()
    if not raw_bytes.startswith(b"%PDF-"):
        raise ValueError(f"File {pdf_path.name} does not start with %PDF- magic header")

    pages_lines = _extract_pages_lines(raw_bytes)
    chunks: list[DocumentChunk] = []
    for page_idx, lines in enumerate(pages_lines[:_MAX_PARSE_PAGES], start=1):
        page_chunks = _build_chunks_from_page_lines(
            lines=lines,
            page_num=page_idx,
            pdf_name=pdf_path.name,
            solicitation_id=solicitation_id.strip(),
            tier_profile=tier_profile,
        )
        chunks.extend(page_chunks)
    return chunks

