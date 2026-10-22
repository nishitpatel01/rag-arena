"""Shared gemini-embedding-2 request shaping for ingestion (documents) and engines (queries).

Measured on Vertex AI (google-genai 1.74, 2026-10-21):

* ``embed_content(contents=[str, str, ...])`` returns **one aggregated vector** for the whole list, and
  ``contents=[Content, Content]`` is rejected with "only supports one content at a time". Distinct vectors
  therefore need one call per text (``VertexEmbedder`` bounds the fan-out).
* ``task_type`` is accepted but did not change the returned vector; the model card recommends asymmetric
  retrieval prefixes instead. Both are sent so query/document spaces match either way.
"""

from __future__ import annotations

from typing import Any

DOCUMENT_TASK_TYPE = "RETRIEVAL_DOCUMENT"
QUERY_TASK_TYPE = "RETRIEVAL_QUERY"
QUERY_PREFIX = "task: question answering | query: "
DOCUMENT_PREFIX = "title: none | text: "


def _require_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("embedding input must be a non-empty string")
    return text.strip()


def format_query_for_embedding(text: str) -> str:
    """Asymmetric retrieval query format for gemini-embedding-2 (model card)."""
    return QUERY_PREFIX + _require_text(text)


def format_document_for_embedding(text: str, title: str | None = None) -> str:
    """Asymmetric retrieval document format for gemini-embedding-2 (model card); ``title`` defaults to ``none``."""
    if title is None:
        return DOCUMENT_PREFIX + _require_text(text)
    clean_title = " ".join(str(title).replace("|", " ").split())[:200] or "none"
    return f"title: {clean_title} | text: " + _require_text(text)


def embed_config(task_type: str) -> Any:
    """Build ``types.EmbedContentConfig`` for ``task_type`` (lazy SDK import; validated)."""
    if task_type not in (DOCUMENT_TASK_TYPE, QUERY_TASK_TYPE):
        raise ValueError(f"task_type must be {DOCUMENT_TASK_TYPE} or {QUERY_TASK_TYPE}, got {task_type!r}")
    from google.genai import types

    return types.EmbedContentConfig(task_type=task_type)


def single_vector(response: Any) -> list[float]:
    """Return the only embedding of a one-content response; fail loud on aggregated/empty responses."""
    embeddings = list(getattr(response, "embeddings", None) or [])
    if len(embeddings) != 1:
        raise ValueError(f"embed_content must return exactly one embedding per call, got {len(embeddings)}")
    values = [float(v) for v in (getattr(embeddings[0], "values", None) or [])]
    if not values:
        raise ValueError("embed_content returned an empty embedding vector")
    return values


__all__ = [
    "DOCUMENT_PREFIX",
    "DOCUMENT_TASK_TYPE",
    "QUERY_PREFIX",
    "QUERY_TASK_TYPE",
    "embed_config",
    "format_document_for_embedding",
    "format_query_for_embedding",
    "single_vector",
]
