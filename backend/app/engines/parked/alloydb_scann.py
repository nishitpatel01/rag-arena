"""PROMOTED in Release 1.4.0: ``AlloyDBScaNNEngine`` now lives at ``app.engines.alloydb_scann``.

Retained as a thin compatibility re-export shim for legacy imports and AST no-silent-fallback checks.
"""

from __future__ import annotations

from app.engines.alloydb_scann import (
    AlloyDBScaNNEngine,
    build_alloydb_hybrid_search_sql,
    build_alloydb_scann_ddl,
    validate_pg_identifier,
)

__all__ = [
    "AlloyDBScaNNEngine",
    "build_alloydb_hybrid_search_sql",
    "build_alloydb_scann_ddl",
    "validate_pg_identifier",
]
