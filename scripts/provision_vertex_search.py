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

import json
import sys
from typing import Any

from app.core.config import ArenaSettings, get_settings
from app.core.vertex_search import DiscoveryEngineClient, VaisResources, redact_resource_text

DATA_STORE_ID = "rag-arena-procurement"
DISPLAY_NAME = "rag-arena-procurement"
ENGINE_ID = "rag-arena-procurement-search"
ENGINE_DISPLAY_NAME = "rag-arena-procurement-search"
UPLOADS_DATA_STORE_ID = "rag-arena-uploads"
UPLOADS_DISPLAY_NAME = "rag-arena-uploads"
UPLOADS_ENGINE_ID = "rag-arena-uploads-search"
UPLOADS_ENGINE_DISPLAY_NAME = "rag-arena-uploads-search"
OPERATION_DEADLINE_SECONDS = 600.0


def data_store_body(display_name: str = DISPLAY_NAME) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "industryVertical": "GENERIC",
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],
        "contentConfig": "CONTENT_REQUIRED",
    }


def engine_body(display_name: str = ENGINE_DISPLAY_NAME, data_store_id: str = DATA_STORE_ID) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "solutionType": "SOLUTION_TYPE_SEARCH",
        "industryVertical": "GENERIC",
        "dataStoreIds": [data_store_id],
        "searchEngineConfig": {"searchTier": "SEARCH_TIER_ENTERPRISE", "searchAddOns": ["SEARCH_ADD_ON_LLM"]},
    }


def uploads_schema_json() -> str:
    """2020-12 JSON Schema marking ``workspace_id`` and ``doc_name`` indexable + retrievable for scoped upload search."""
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "datetime_detection": True,
            "geolocation_detection": True,
            "properties": {
                "workspace_id": {"type": "string", "indexable": True, "retrievable": True, "dynamicFacetable": False},
                "doc_name": {"type": "string", "indexable": True, "retrievable": True},
            },
        }
    )


def _verify_existing_store(doc: dict[str, Any], store_id: str = DATA_STORE_ID, display_name: str = DISPLAY_NAME) -> None:
    if doc.get("displayName") != display_name:
        raise RuntimeError(f"data store '{store_id}' exists with displayName {doc.get('displayName')!r} — not ours; refusing")
    if doc.get("contentConfig") != "CONTENT_REQUIRED":
        raise RuntimeError(f"data store '{store_id}' has contentConfig {doc.get('contentConfig')!r}, expected CONTENT_REQUIRED")


def _verify_existing_engine(
    doc: dict[str, Any], engine_id: str = ENGINE_ID, display_name: str = ENGINE_DISPLAY_NAME, store_id: str = DATA_STORE_ID
) -> None:
    if doc.get("displayName") != display_name:
        raise RuntimeError(f"engine '{engine_id}' exists with displayName {doc.get('displayName')!r} — not ours; refusing")
    cfg = doc.get("searchEngineConfig") or {}
    if cfg.get("searchTier") != "SEARCH_TIER_ENTERPRISE" or "SEARCH_ADD_ON_LLM" not in (cfg.get("searchAddOns") or []):
        raise RuntimeError(f"engine '{engine_id}' config {cfg} lacks Enterprise tier + LLM add-on (left unchanged)")
    stores = [str(s).rsplit("/", 1)[-1] for s in doc.get("dataStoreIds", []) or []]
    if stores and store_id not in stores:
        raise RuntimeError(f"engine '{engine_id}' is not attached to data store '{store_id}' (left unchanged)")


def ensure_data_store(
    client: DiscoveryEngineClient, res: VaisResources, store_id: str = DATA_STORE_ID, display_name: str = DISPLAY_NAME
) -> str:
    """'exists' | 'created'."""
    existing = client.get_or_none(res.data_store_path)
    if existing is not None:
        _verify_existing_store(existing, store_id, display_name)
        return "exists"
    op, _ = client.request(
        "POST", f"{res.collection_path}/dataStores", body=data_store_body(display_name), params={"dataStoreId": store_id}
    )
    client.poll_operation(op, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return "created"


def ensure_engine(
    client: DiscoveryEngineClient,
    res: VaisResources,
    engine_id: str = ENGINE_ID,
    display_name: str = ENGINE_DISPLAY_NAME,
    store_id: str = DATA_STORE_ID,
) -> str:
    """'exists' | 'created'."""
    existing = client.get_or_none(res.engine_path)
    if existing is not None:
        _verify_existing_engine(existing, engine_id, display_name, store_id)
        return "exists"
    op, _ = client.request(
        "POST", f"{res.collection_path}/engines", body=engine_body(display_name, store_id), params={"engineId": engine_id}
    )
    client.poll_operation(op, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return "created"


def _schema_has_upload_fields(schema_doc: dict[str, Any] | None) -> bool:
    raw = (schema_doc or {}).get("jsonSchema")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    props = parsed.get("properties") if isinstance(parsed, dict) else None
    if not isinstance(props, dict):
        return False
    for field in ("workspace_id", "doc_name"):
        spec = props.get(field)
        if not isinstance(spec, dict) or spec.get("type") != "string":
            return False
        if spec.get("indexable") is not True or spec.get("retrievable") is not True:
            return False
    return True


def ensure_uploads_schema(client: DiscoveryEngineClient, res: VaisResources) -> str:
    """'exists' | 'updated' — ensures ``default_schema`` indexes ``workspace_id`` and ``doc_name``."""
    schema_path = f"{res.data_store_path}/schemas/default_schema"
    existing = client.get_or_none(schema_path)
    if _schema_has_upload_fields(existing):
        return "exists"
    resp, _ = client.request("PATCH", schema_path, body={"jsonSchema": uploads_schema_json()})
    if isinstance(resp, dict) and isinstance(resp.get("name"), str) and ("done" in resp or "/operations/" in resp["name"]):
        client.poll_operation(resp, deadline_seconds=OPERATION_DEADLINE_SECONDS)
    return "updated"


def provision(client: DiscoveryEngineClient, settings: ArenaSettings) -> dict[str, str]:
    res = VaisResources(settings.gcp_project_id, settings.vais_location, DATA_STORE_ID, ENGINE_ID)
    store = ensure_data_store(client, res)
    engine = ensure_engine(client, res)
    return {"data_store": store, "engine": engine}


def provision_uploads(client: DiscoveryEngineClient, settings: ArenaSettings) -> dict[str, str]:
    """Provision the isolated v1.1 uploads data store + filterable schema + search app."""
    res = VaisResources(settings.gcp_project_id, settings.vais_location, UPLOADS_DATA_STORE_ID, UPLOADS_ENGINE_ID)
    store = ensure_data_store(client, res, UPLOADS_DATA_STORE_ID, UPLOADS_DISPLAY_NAME)
    schema = ensure_uploads_schema(client, res)
    engine = ensure_engine(client, res, UPLOADS_ENGINE_ID, UPLOADS_ENGINE_DISPLAY_NAME, UPLOADS_DATA_STORE_ID)
    return {"uploads_data_store": store, "uploads_schema": schema, "uploads_engine": engine}


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
        uploads_outcome = provision_uploads(client, settings)
    except Exception as exc:  # boundary: print the redacted error and exit non-zero (fail loud)
        print(f"ERROR {type(exc).__name__}: {redact_resource_text(str(exc), settings.gcp_project_id)}", file=sys.stderr)
        return 1
    print(f"data store {DATA_STORE_ID}: {outcome['data_store']}; engine {ENGINE_ID}: {outcome['engine']}", file=sys.stderr)
    print(
        f"uploads store {UPLOADS_DATA_STORE_ID}: {uploads_outcome['uploads_data_store']} "
        f"(schema: {uploads_outcome['uploads_schema']}); engine {UPLOADS_ENGINE_ID}: {uploads_outcome['uploads_engine']}",
        file=sys.stderr,
    )
    print(f"VAIS_LOCATION={settings.vais_location}")
    print(f"VAIS_DATA_STORE_ID={DATA_STORE_ID}")
    print(f"VAIS_ENGINE_ID={ENGINE_ID}")
    print(f"VAIS_UPLOADS_DATA_STORE_ID={UPLOADS_DATA_STORE_ID}")
    print(f"VAIS_UPLOADS_ENGINE_ID={UPLOADS_ENGINE_ID}")
    return 0


def _env() -> dict[str, str]:
    import os

    from app.core.config import _PROJECT_ROOT, _load_dotenv_file

    return {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}


if __name__ == "__main__":
    raise SystemExit(main())
