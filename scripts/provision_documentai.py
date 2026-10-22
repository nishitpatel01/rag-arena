#!/usr/bin/env python3
"""Idempotent provisioning of the Level 3 Document AI Layout Parser processor.

Lists the processors in ``DOCAI_LOCATION`` (default ``us``) and looks for exactly one processor with displayName
``rag-arena-layout-parser`` and type ``LAYOUT_PARSER_PROCESSOR``; creates it (``processors.create``) only when absent.
Every other processor in the project is ignored — never reused, modified, or deleted.

Prints ONLY the short processor id (put it in the git-ignored ``.env`` as ``DOCAI_PROCESSOR_ID``) plus a redacted
status line on stderr; the project id/number never appear in the output.

Usage (identity from the git-ignored .env): ``PYTHONPATH=backend .venv/bin/python scripts/provision_documentai.py``
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.gcp_clients import get_access_token  # noqa: E402
from app.ingestion.documentai_client import DocumentAIClient, ensure_layout_parser  # noqa: E402


def main() -> int:
    """Find-or-create ``rag-arena-layout-parser``; stdout = short processor id only."""
    settings = replace(get_settings(), demo_mode="live")
    location = os.environ.get("DOCAI_LOCATION", settings.docai_location).strip() or "us"
    client = DocumentAIClient(
        project_id=settings.gcp_project_id,
        location=location,
        token_provider=lambda: get_access_token(settings),
        timeout_seconds=60.0,
    )
    processor, created = ensure_layout_parser(client)
    action = "created" if created else "already present"
    print(
        f"rag-arena-layout-parser {action} in location '{location}' (type {processor.type}, state {processor.state}, "
        f"default version {processor.default_version or 'unset'})",
        file=sys.stderr,
    )
    print(processor.processor_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
