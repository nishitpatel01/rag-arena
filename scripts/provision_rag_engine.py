"""Provision the Level 6 Vertex AI RAG Engine corpus at minimum monthly cost (idempotent; REST v1 aiplatform).

Safety invariants:
1. Inspects regional ``ragEngineConfig`` first:
   - If ALREADY configured (e.g. ``spanner.basic``, ``serverless``, ``scaled``, ``enterprise``), reuses it untouched
     (0 writes, $0 incremental compute on projects already running ``RagManagedDb``).
   - ONLY when ``ragEngineConfig`` is ``unprovisioned`` does it initialize ``RagManagedDbConfig(serverless={})``
     (~$0/mo standing compute; overridable via ``--fresh-db-mode=spanner_basic``).
   - NEVER sets ``ragEngineConfig`` to ``unprovisioned``.
2. Lists existing ``ragCorpora`` in ``RAG_ENGINE_LOCATION`` and reuses ``displayName == "rag-arena-procurement"`` if
   present; otherwise creates it on ``ragManagedDb.knn`` with ``RAG_ENGINE_EMBEDDING_MODEL`` (default
   ``text-embedding-005``). Neighbor corpora in the project are never modified or deleted.
3. Prints ONLY ``RAG_ENGINE_LOCATION`` and the short ``RAG_ENGINE_CORPUS_ID`` for the git-ignored ``.env``; errors
   redact project id and project number.

Run: ``PYTHONPATH=backend .venv/bin/python scripts/provision_rag_engine.py``
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from app.core.config import DEFAULT_RAG_ENGINE_EMBEDDING_MODEL, ArenaSettings, get_settings
from app.core.rag_engine_client import (
    RagEngineResources,
    VertexRagClient,
    classify_engine_config,
    corpus_body as _build_corpus_body,
    extract_short_corpus_id,
    redact_resource_text,
)

CORPUS_DISPLAY_NAME = "rag-arena-procurement"
CORPUS_DESCRIPTION = "The RAG Arena benchmark procurement corpus"
VALID_FRESH_DB_MODES: tuple[str, ...] = ("serverless", "spanner_basic")
OPERATION_DEADLINE_SECONDS = 600.0


def corpus_body(
    res: RagEngineResources,
    *,
    display_name: str = CORPUS_DISPLAY_NAME,
    embedding_model: str = DEFAULT_RAG_ENGINE_EMBEDDING_MODEL,
    description: str = CORPUS_DESCRIPTION,
) -> dict[str, Any]:
    """REST v1 ``RagCorpus`` payload using managed KNN vector search and publisher embedding model."""
    return _build_corpus_body(
        res, display_name=display_name, embedding_model=embedding_model, description=description
    )


def ensure_engine_config(
    client: VertexRagClient, res: RagEngineResources, *, fresh_db_mode: str = "serverless"
) -> str:
    """Inspect ``ragEngineConfig``: reuse any active DB untouched, or initialize ``serverless`` if unprovisioned."""
    if fresh_db_mode not in VALID_FRESH_DB_MODES:
        raise ValueError(f"fresh_db_mode must be one of {VALID_FRESH_DB_MODES}, got {fresh_db_mode!r}")
    existing = client.get_or_none(res.engine_config_path)
    mode = classify_engine_config(existing)
    if mode != "unprovisioned":
        return f"reused:{mode}"
    managed_cfg: dict[str, Any] = (
        {"serverless": {}} if fresh_db_mode == "serverless" else {"spanner": {"basic": {}}}
    )
    resp, _ = client.request(
        "PATCH",
        res.engine_config_path,
        body={"name": res.engine_config_path, "ragManagedDbConfig": managed_cfg},
    )
    if isinstance(resp, dict) and isinstance(resp.get("name"), str) and (
        "done" in resp or "/operations/" in resp["name"]
    ):
        client.poll_operation(resp, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return f"initialized:{fresh_db_mode}"


def ensure_corpus(
    client: VertexRagClient,
    res: RagEngineResources,
    *,
    display_name: str = CORPUS_DISPLAY_NAME,
    embedding_model: str = DEFAULT_RAG_ENGINE_EMBEDDING_MODEL,
    description: str = CORPUS_DESCRIPTION,
) -> tuple[str, str]:
    """Return ``('exists' | 'created', short_corpus_id)`` without touching any other corpus in the region."""
    for corpus in client.list_corpora(res.corpora_path):
        if corpus.get("displayName") != display_name:
            continue
        state = str((corpus.get("corpusStatus") or {}).get("state") or "UNKNOWN")
        short_id = extract_short_corpus_id(str(corpus.get("name") or ""))
        if state == "ERROR":
            msg = redact_resource_text(str(corpus.get("corpusStatus")), res.project_id)
            raise RuntimeError(f"corpus '{display_name}' ({short_id}) is in ERROR state: {msg}")
        return "exists", short_id
    op, _ = client.request(
        "POST",
        res.corpora_path,
        body=corpus_body(res, display_name=display_name, embedding_model=embedding_model, description=description),
    )
    done = client.poll_operation(op, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    created = done.get("response") if isinstance(done.get("response"), dict) else {}
    short_id = extract_short_corpus_id(str(created.get("name") or ""))
    return "created", short_id


def provision(
    client: VertexRagClient, settings: ArenaSettings, *, fresh_db_mode: str = "serverless"
) -> dict[str, str]:
    res = RagEngineResources(settings.gcp_project_id, settings.rag_engine_location)
    db_state = ensure_engine_config(client, res, fresh_db_mode=fresh_db_mode)
    corpus_state, corpus_id = ensure_corpus(
        client, res, display_name=CORPUS_DISPLAY_NAME, embedding_model=settings.rag_engine_embedding_model
    )
    return {"db_state": db_state, "corpus_state": corpus_state, "corpus_id": corpus_id}


def _env() -> dict[str, str]:
    import os

    from app.core.config import _PROJECT_ROOT, _load_dotenv_file

    return {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the Level 6 Vertex AI RAG Engine corpus.")
    parser.add_argument(
        "--fresh-db-mode",
        choices=VALID_FRESH_DB_MODES,
        default="serverless",
        help="DB mode used ONLY when regional ragEngineConfig is unprovisioned (default: serverless).",
    )
    args = parser.parse_args(argv)

    from app.core.gcp_clients import get_access_token

    settings = get_settings()
    if not settings.is_live:
        settings = get_settings({**_env(), "DEMO_MODE": "live"})
    client = VertexRagClient(
        project_id=settings.gcp_project_id,
        location=settings.rag_engine_location,
        token_provider=lambda: get_access_token(settings),
        attempt_timeout_seconds=60.0,
        budget_seconds=180.0,
    )
    try:
        outcome = provision(client, settings, fresh_db_mode=args.fresh_db_mode)
    except Exception as exc:  # boundary: print the redacted error and exit non-zero (fail loud)
        print(f"ERROR {type(exc).__name__}: {redact_resource_text(str(exc), settings.gcp_project_id)}", file=sys.stderr)
        return 1
    print(
        f"ragEngineConfig ({settings.rag_engine_location}): {outcome['db_state']}; "
        f"corpus {CORPUS_DISPLAY_NAME}: {outcome['corpus_state']}",
        file=sys.stderr,
    )
    print(f"RAG_ENGINE_LOCATION={settings.rag_engine_location}")
    print(f"RAG_ENGINE_CORPUS_ID={outcome['corpus_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
