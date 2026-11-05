"""PROMOTED in Release 1.4.0: ``VertexVectorSearchV2Engine`` now lives at ``app.engines.vertex_vector_v2``.

Retained as a thin compatibility re-export shim for legacy imports and AST no-silent-fallback checks.
"""

from __future__ import annotations

from app.engines.vertex_vector_v2 import VertexVectorSearchV2Engine, parse_batch_search_candidates

__all__ = ["VertexVectorSearchV2Engine", "parse_batch_search_candidates"]
