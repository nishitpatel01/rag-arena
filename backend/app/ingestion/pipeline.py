"""End-to-end Corpus Ingestion Pipeline for parsing and serializing multi-tier DocumentChunks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.models import DocumentChunk
from app.ingestion.parsers import _MAX_STREAM_MATCHES, _STREAM_PATTERN, _VALID_TIERS, parse_pdf_Fallback_or_fitz

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CORPUS_DIR = _PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"
_MAX_SOLICITATION_DIRS = 20
_MAX_PDFS_PER_SOLICITATION = 50
_MAX_EMBED_PDF_BYTES = 5 * 1024 * 1024


def collect_corpus_chunks(
    corpus_dir: Path | None = None,
    tiers: tuple[str, ...] = ("naive", "hybrid", "multimodal"),
) -> list[DocumentChunk]:
    """Deterministically parse all procurement corpus PDFs across the specified RAG tiers."""
    resolved_dir = _DEFAULT_CORPUS_DIR if corpus_dir is None else corpus_dir
    if not isinstance(resolved_dir, Path):
        raise TypeError("corpus_dir must be a pathlib.Path instance or None")
    if not resolved_dir.exists() or not resolved_dir.is_dir():
        raise FileNotFoundError(f"Corpus directory not found: {resolved_dir}")
    if not isinstance(tiers, (tuple, list)) or not tiers:
        raise ValueError("tiers must be a non-empty tuple or list of tier profile names")
    for tier in tiers:
        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier '{tier}'. Must be one of {_VALID_TIERS}")

    sol_dirs = sorted(
        p
        for p in resolved_dir.iterdir()
        if p.is_dir() and p.name.startswith("solicitation_")
    )[:_MAX_SOLICITATION_DIRS]

    all_chunks: list[DocumentChunk] = []
    for sol_dir in sol_dirs:
        sol_id = "_".join(sol_dir.name.split("_")[:2])
        pdf_paths = sorted(sol_dir.glob("*.pdf"))[:_MAX_PDFS_PER_SOLICITATION]
        for pdf_path in pdf_paths:
            for tier in tiers:
                chunks = parse_pdf_Fallback_or_fitz(
                    pdf_path=pdf_path,
                    solicitation_id=sol_id,
                    tier_profile=tier,
                )
                all_chunks.extend(chunks)
    return all_chunks


def chunk_to_bigquery_row(
    chunk: DocumentChunk,
    embedding: list[float] | None = None,
    *,
    ingest_run_id: str = "",
    layout_source: str = "",
) -> dict[str, Any]:
    """Serialize a DocumentChunk and optional embedding vector into a BigQuery table row dict (spec §5 schema)."""
    if not isinstance(chunk, DocumentChunk):
        raise TypeError("chunk must be a DocumentChunk instance")
    if embedding is not None and not isinstance(embedding, list):
        raise TypeError("embedding must be a list of floats or None")
    if not isinstance(ingest_run_id, str) or not isinstance(layout_source, str):
        raise TypeError("ingest_run_id and layout_source must be strings")

    clean_embedding = [float(v) for v in embedding] if embedding is not None else []
    mm_embedding = clean_embedding if chunk.tier_profile == "multimodal" else []
    return {
        "chunk_id": chunk.chunk_id,
        "tier_profile": chunk.tier_profile,
        "ingest_run_id": ingest_run_id,
        "solicitation_id": chunk.solicitation_id,
        "doc_name": chunk.doc_name,
        "doc_type": chunk.doc_type,
        "element_type": chunk.element_type,
        "section_id": chunk.section_id,
        "amendment_number": int(chunk.amendment_number),
        "effective_date": chunk.effective_date,
        "amends_section": chunk.amends_section,
        "page_number": int(chunk.page_number),
        "bbox": chunk.bbox.to_dict(),
        "content": chunk.content,
        "layout_source": layout_source,
        "embedding": clean_embedding,
        "embedding_multimodal": mm_embedding,
    }


def _find_chunk_pdf_path(chunk: DocumentChunk, resolved_dir: Path) -> Path | None:
    """Locate the source PDF file for a DocumentChunk within resolved_dir with bounded iteration."""
    if not isinstance(chunk, DocumentChunk) or not isinstance(resolved_dir, Path):
        return None
    if not resolved_dir.exists() or not resolved_dir.is_dir():
        return None
    direct_path = resolved_dir / chunk.solicitation_id / chunk.doc_name
    if direct_path.exists() and direct_path.is_file():
        return direct_path
    sol_dirs = sorted(
        p for p in resolved_dir.iterdir() if p.is_dir() and p.name.startswith(chunk.solicitation_id)
    )[:_MAX_SOLICITATION_DIRS]
    for sol_dir in sol_dirs:
        candidate = sol_dir / chunk.doc_name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _wrap_single_page_pdf_stream(stream_data: bytes) -> bytes:
    """Wrap a single PDF 1.4 content stream into a minimal standards-compliant 1-page PDF binary."""
    objects: list[bytes] = [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii") + stream_data + b"\nendstream",
        (
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 1 0 R /F2 2 0 R /F3 3 0 R >> >> "
            b"/Contents 4 0 R >>"
        ),
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        b"<< /Type /Catalog /Pages 6 0 R >>",
    ]
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for obj_num, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 7 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(buf)


def extract_single_page_pdf_bytes(pdf_path: Path, page_number: int = 1) -> bytes:
    """Extract a single target page from a multi-page PDF to minimize multimodal embedding payload."""
    if not isinstance(pdf_path, Path) or not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be an integer >= 1")

    raw_bytes = pdf_path.read_bytes()[:_MAX_EMBED_PDF_BYTES]
    try:
        import fitz  # type: ignore[import-not-found]

        with fitz.open(stream=raw_bytes, filetype="pdf") as src_doc:
            if len(src_doc) <= 1:
                return raw_bytes
            target_idx = min(page_number - 1, len(src_doc) - 1)
            out_doc = fitz.open()
            out_doc.insert_pdf(src_doc, from_page=target_idx, to_page=target_idx)
            sliced = out_doc.tobytes()
            out_doc.close()
            return sliced[:_MAX_EMBED_PDF_BYTES]
    except ImportError:
        pass

    page_streams: list[bytes] = []
    for match in list(_STREAM_PATTERN.finditer(raw_bytes))[:_MAX_STREAM_MATCHES]:
        candidate = match.group(1)
        if b"BT" in candidate:
            page_streams.append(candidate)
    if len(page_streams) <= 1:
        return raw_bytes
    target_idx = min(page_number - 1, len(page_streams) - 1)
    return _wrap_single_page_pdf_stream(page_streams[target_idx])[:_MAX_EMBED_PDF_BYTES]


def build_embedding_contents_for_chunk(
    chunk: DocumentChunk,
    corpus_dir: Path | None = None,
) -> list[Any]:
    """Build multimodal or text contents payload for Vertex AI embed_content."""
    if not isinstance(chunk, DocumentChunk):
        raise TypeError("chunk must be a DocumentChunk instance")
    if corpus_dir is not None and not isinstance(corpus_dir, Path):
        raise TypeError("corpus_dir must be a pathlib.Path instance or None")
    if not chunk.content or not chunk.content.strip():
        raise ValueError("chunk.content must be non-empty")

    if chunk.tier_profile != "multimodal" or chunk.element_type != "diagram":
        return [chunk.content]

    resolved_dir = _DEFAULT_CORPUS_DIR if corpus_dir is None else corpus_dir
    pdf_path = _find_chunk_pdf_path(chunk, resolved_dir)
    if pdf_path is None:
        return [chunk.content]

    pdf_bytes = extract_single_page_pdf_bytes(pdf_path, page_number=chunk.page_number)
    try:
        from google.genai import types  # type: ignore[import-not-found]

        part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        return [part, chunk.content]
    except Exception:
        return [chunk.content]
