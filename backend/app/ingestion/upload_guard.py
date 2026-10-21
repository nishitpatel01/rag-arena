"""Guardrails for validating user-uploaded PDF documents before ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import uuid

from app.core.models import UploadMetadata

_PAGE_OBJECT_PATTERN = re.compile(rb"/Type\s*/Page\b(?![sS])")


class UploadGuardError(ValueError):
    """Raised when an uploaded file violates size, format, or page-count bounds."""


def _count_pdf_pages(raw_bytes: bytes) -> int:
    """Deterministically count PDF pages using PyMuPDF if available or PDF object markers."""
    if not raw_bytes:
        return 0
    try:
        import pymupdf  # type: ignore[import-not-found]

        with pymupdf.open(stream=raw_bytes, filetype="pdf") as doc:
            return int(len(doc))
    except ImportError:
        pass
    except Exception as exc:
        raise UploadGuardError(f"Corrupt or unreadable PDF structure: {exc}") from exc

    matches = _PAGE_OBJECT_PATTERN.findall(raw_bytes)
    return len(matches)


def validate_pdf_upload(
    raw_bytes: bytes,
    filename: str,
    max_bytes: int = 10 * 1024 * 1024,
    max_pages: int = 30,
) -> UploadMetadata:
    """Validate PDF magic bytes, filename extension, byte size, and page count bounds."""
    if not isinstance(raw_bytes, bytes):
        raise UploadGuardError("raw_bytes must be a bytes instance")
    if not isinstance(filename, str) or not filename.strip():
        raise UploadGuardError("filename must be a non-empty string")
    if max_bytes < 1024 or max_pages < 1:
        raise ValueError("max_bytes (>=1024) and max_pages (>=1) must be positive bounds")

    clean_name = filename.strip()
    if not clean_name.lower().endswith(".pdf"):
        raise UploadGuardError(f"Uploaded file '{clean_name}' must end with .pdf")
    if len(raw_bytes) == 0:
        raise UploadGuardError("Uploaded file is empty")
    if len(raw_bytes) > max_bytes:
        raise UploadGuardError(
            f"File '{clean_name}' ({len(raw_bytes)} bytes) exceeds 10 MB limit ({max_bytes} bytes)"
        )
    if not raw_bytes.startswith(b"%PDF-"):
        raise UploadGuardError("Invalid PDF magic header: file must begin with '%PDF-'")

    page_count = _count_pdf_pages(raw_bytes)
    if page_count < 1:
        raise UploadGuardError("PDF contains 0 recognizable pages")
    if page_count > max_pages:
        raise UploadGuardError(
            f"PDF page count ({page_count}) exceeds maximum limit of {max_pages} pages"
        )

    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    return UploadMetadata(
        session_id=str(uuid.uuid4()),
        filename=clean_name,
        size_bytes=len(raw_bytes),
        page_count=page_count,
        sha256_hex=sha256_hex,
        expires_at=expires_at,
    )
