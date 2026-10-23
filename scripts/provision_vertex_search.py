"""Provision the Level 5 Vertex AI Search resources (idempotent; REST v1 discoveryengine; list/get + create only).

Creates, only when absent (``default_collection`` in ``VAIS_LOCATION``, default ``global``):

1. Data store ``rag-arena-procurement`` — unstructured (``CONTENT_REQUIRED``), ``SOLUTION_TYPE_SEARCH``, GENERIC,
   default digital parser and NO chunking config: ``layoutBasedChunkingConfig`` makes the search API reject
   ``extractiveContentSpec`` ("extractive_content_spec must be not defined when the datastore is using 'chunking
   config'"), and Level 5 cites extractive segments with page numbers.
2. Search engine (app) ``rag-arena-procurement-search`` — ``SEARCH_TIER_ENTERPRISE`` + ``SEARCH_ADD_ON_LLM``
   (extractive segments + summaries) over that data store.

An existing resource with our id is verified (display name / tier) and left untouched — never modified, never
deleted; no other data store or engine is read or touched. Prints ONLY short ids (``VAIS_*`` lines for the
git-ignored .env); errors are redacted (project id / number).

Run: ``PYTHONPATH=backend .venv/bin/python scripts/provision_vertex_search.py`` (env: GCP_PROJECT_ID, VAIS_LOCATION).
"""

from __future__ import annotations

import sys
from typing import Any

from app.core.config import ArenaSettings, get_settings
from app.core.vertex_search import DiscoveryEngineClient, VaisResources, redact_resource_text

DATA_STORE_ID = "rag-arena-procurement"
DISPLAY_NAME = "rag-arena-procurement"
ENGINE_ID = "rag-arena-procurement-search"
ENGINE_DISPLAY_NAME = "rag-arena-procurement-search"
OPERATION_DEADLINE_SECONDS = 600.0


def data_store_body() -> dict[str, Any]:
    return {
        "displayName": DISPLAY_NAME,
        "industryVertical": "GENERIC",
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],
        "contentConfig": "CONTENT_REQUIRED",
    }


def engine_body() -> dict[str, Any]:
    return {
        "displayName": ENGINE_DISPLAY_NAME,
        "solutionType": "SOLUTION_TYPE_SEARCH",
        "industryVertical": "GENERIC",
        "dataStoreIds": [DATA_STORE_ID],
        "searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]},
    }


def _verify_existing_store(doc: dict[str, Any]) -> None:
    if doc.get("displayName") != DISPLAY_NAME:
        raise RuntimeError(f"data store '{DATA_STORE_ID}' exists with displayName {doc.get('displayName')!r} — not ours; refusing")
    if doc.get("contentConfig") != "CONTENT_REQUIRED":
        raise RuntimeError(f"data store '{DATA_STORE_ID}' has contentConfig {doc.get('contentConfig')!r}, expected CONTENT_REQUIRED")


def _verify_existing_engine(doc: dict[str, Any]) -> None:
    if doc.get("displayName") != ENGINE_DISPLAY_NAME:
        raise RuntimeError(f"engine '{ENGINE_ID}' exists with displayName {doc.get('displayName')!r} — not ours; refusing")
    cfg = doc.get("searchEngineConfig") or {}
    if cfg.get("searchTier") != "SEARCH_TIER_ENTERPRISE" or "SEARCH_ADD_ON_LLM" not in (cfg.get("searchAddOns") or []):
        raise RuntimeError(f"engine '{ENGINE_ID}' config {cfg} lacks Enterprise tier + LLM add-on (left unchanged)")
    stores = [str(s).rsplit("/", 1)[-1] for s in doc.get("dataStoreIds", []) or []]
    if stores and DATA_STORE_ID not in stores:
        raise RuntimeError(f"engine '{ENGINE_ID}' is not attached to data store '{DATA_STORE_ID}' (left unchanged)")


def ensure_data_store(client: DiscoveryEngineClient, res: VaisResources) -> str:
    """'exists' | 'created'."""
    existing = client.get_or_none(res.data_store_path)
    if existing is not None:
        _verify_existing_store(existing)
        return "exists"
    op, _ = client.request("POST", f"{res.collection_path}/dataStores", body=data_store_body(), params={"dataStoreId": DATA_STORE_ID})
    client.poll_operation(op, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return "created"


def ensure_engine(client: DiscoveryEngineClient, res: VaisResources) -> str:
    """'exists' | 'created'."""
    existing = client.get_or_none(res.engine_path)
    if existing is not None:
        _verify_existing_engine(existing)
        return "exists"
    op, _ = client.request("POST", f"{res.collection_path}/engines", body=engine_body(), params={"engineId": ENGINE_ID})
    client.poll_operation(op, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return "created"


def provision(client: DiscoveryEngineClient, settings: ArenaSettings) -> dict[str, str]:
    res = VaisResources(settings.gcp_project_id, settings.vais_location, DATA_STORE_ID, ENGINE_ID)
    store = ensure_data_store(client, res)
    engine = ensure_engine(client, res)
    return {"data_store": store, "engine": engine}


def main() -> int:
    from app.core.gcp_clients import get_access_token

    settings = get_settings()
    if not settings.is_live:
        settings = get_settings({**_env(), "DEMO_MODE": "live"})
    client = DiscoveryEngineClient(
        project_id=settings.gcp_project_id, location=settings.vais_location,
        token_provider=lambda: get_access_token(settings), attempt_timeout_seconds=60.0, budget_seconds=180.0,
    )
    try:
        outcome = provision(client, settings)
    except Exception as exc:  # boundary: print the redacted error and exit non-zero (fail loud)
        print(f"ERROR {type(exc).__name__}: {redact_resource_text(str(exc), settings.gcp_project_id)}", file=sys.stderr)
        return 1
    print(f"data store {DATA_STORE_ID}: {outcome['data_store']}; engine {ENGINE_ID}: {outcome['engine']}", file=sys.stderr)
    print(f"VAIS_LOCATION={settings.vais_location}")
    print(f"VAIS_DATA_STORE_ID={DATA_STORE_ID}")
    print(f"VAIS_ENGINE_ID={ENGINE_ID}")
    return 0


def _env() -> dict[str, str]:
    import os

    from app.core.config import _PROJECT_ROOT, _load_dotenv_file

    return {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}


if __name__ == "__main__":
    raise SystemExit(main())
