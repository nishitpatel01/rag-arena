"""Idempotent provisioning CLI for the $0/mo Google Cloud Model Armor guardrail template (REST v1).

Safety & cost invariants:
1. Defaults to ``--dry-run`` (0 GCP writes, 0 ADC calls required) so operators can inspect the exact
   ``filterConfig`` payload (Prompt Injection & Jailbreak + Responsible AI + Malicious URI + Basic SDP)
   before mutating any project resource.
2. Model Armor template storage is ``$0.00/mo`` (preserving RAG Arena's ``$0/mo`` standing idle rule);
   token screening includes a 2M tokens/month free tier ($1.50 / 1M tokens standalone pay-as-you-go).
3. With ``--apply``, performs a read-first ``GET`` on ``.../templates/<template_id>``:
   - If the template already exists (HTTP 200), reuses it untouched (0 writes; neighbor templates untouched).
   - If absent (HTTP 404), creates it via ``POST .../templates?template_id=<template_id>``.
4. Redacts project IDs and project numbers in all diagnostic/error messages and prints ONLY
   ``MODEL_ARMOR_LOCATION=...`` and ``MODEL_ARMOR_TEMPLATE_ID=...`` on stdout for the git-ignored ``.env``.

Usage:
  Dry-run (default): ``PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py``
  Apply live:        ``PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py --apply``
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from app.core.config import DEFAULT_MODEL_ARMOR_LOCATION, ArenaSettings, get_settings
from app.core.model_armor import (
    MODEL_ARMOR_TEMPLATE_MONTHLY_USD,
    ModelArmorClient,
    extract_short_template_id,
    model_armor_host,
    redact_model_armor_text,
    template_resource_url,
    templates_collection_url,
    validate_location,
)

DEFAULT_TEMPLATE_ID = "rag-arena-guardrail"
OPERATION_DEADLINE_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 2.0


def build_template_body() -> dict[str, Any]:
    """REST v1 ``Template`` body: PI & Jailbreak + RAI + Malicious URI + Basic SDP ($0/mo storage)."""
    return {
        "filterConfig": {
            "piAndJailbreakFilterSettings": {
                "filterEnforcement": "ENABLED",
                "confidenceLevel": "MEDIUM_AND_ABOVE",
            },
            "raiSettings": {
                "raiFilters": [
                    {"filterType": "HATE_SPEECH", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                    {"filterType": "HARASSMENT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                    {"filterType": "DANGEROUS", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                    {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                ]
            },
            "maliciousUriFilterSettings": {
                "filterEnforcement": "ENABLED",
            },
            "sdpSettings": {
                "basicConfig": {
                    "filterEnforcement": "ENABLED",
                }
            },
        }
    }


def _poll_if_operation(
    client: ModelArmorClient,
    doc: dict[str, Any],
    *,
    deadline_seconds: float = OPERATION_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """If ``POST`` returned a long-running operation resource, poll ``GET`` until ``done: true``."""
    name = doc.get("name")
    if not isinstance(name, str) or "/operations/" not in name or doc.get("done") is True:
        return doc.get("response") if isinstance(doc.get("response"), dict) else doc
    op_url = f"{model_armor_host(client.location)}/v1/{name.lstrip('/')}"
    deadline = time.monotonic() + deadline_seconds
    current = doc
    while not current.get("done"):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Model Armor template creation timed out after {deadline_seconds:.0f} s")
        time.sleep(POLL_INTERVAL_SECONDS)
        polled, _ = client.request("GET", op_url)
        if not isinstance(polled, dict):
            raise RuntimeError("Malformed operation status payload from Model Armor")
        current = polled
    if isinstance(current.get("error"), dict):
        err = redact_model_armor_text(json.dumps(current["error"]), client.project_id, client.template_id)
        raise RuntimeError(f"Model Armor template operation failed: {err}")
    return current.get("response") if isinstance(current.get("response"), dict) else current


def ensure_template(
    client: ModelArmorClient,
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> tuple[str, str]:
    """Return ``('exists' | 'created', short_template_id)`` idempotently."""
    short_id = extract_short_template_id(template_id)
    get_url = template_resource_url(client.project_id, client.location, short_id)
    existing = client.get_or_none(get_url)
    if existing is not None:
        resolved_id = extract_short_template_id(str(existing.get("name") or short_id))
        return "exists", resolved_id

    create_url = f"{templates_collection_url(client.project_id, client.location)}?template_id={short_id}"
    created_raw, _ = client.request("POST", create_url, body=build_template_body())
    if not isinstance(created_raw, dict):
        raise RuntimeError("Model Armor template POST did not return a JSON object")
    created = _poll_if_operation(client, created_raw)
    resolved_id = extract_short_template_id(str(created.get("name") or short_id))
    return "created", resolved_id


def _env() -> dict[str, str]:
    import os

    from app.core.config import _PROJECT_ROOT, _load_dotenv_file

    return {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently provision or inspect the $0/mo Google Cloud Model Armor template."
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print the planned REST v1 template payload without calling GCP (default).",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Verify or create the Model Armor template in GCP via REST v1.",
    )
    parser.add_argument(
        "--template-id",
        default="",
        help=f"Short template ID to verify/create (default: {DEFAULT_TEMPLATE_ID}).",
    )
    parser.add_argument(
        "--location",
        default="",
        help=f"Model Armor regional endpoint location (default: MODEL_ARMOR_LOCATION or {DEFAULT_MODEL_ARMOR_LOCATION}).",
    )
    args = parser.parse_args(argv)

    env_map = _env()
    location = validate_location(
        args.location.strip() or env_map.get("MODEL_ARMOR_LOCATION", DEFAULT_MODEL_ARMOR_LOCATION).strip()
    )
    short_id = extract_short_template_id(
        args.template_id.strip() or env_map.get("MODEL_ARMOR_TEMPLATE_ID", "").strip() or DEFAULT_TEMPLATE_ID
    )

    if not args.apply:
        redacted_url = (
            f"{model_armor_host(location)}/v1/projects/${{GCP_PROJECT_ID}}/"
            f"locations/{location}/templates?template_id={short_id}"
        )
        print(
            f"[dry-run] Standing template cost: ${MODEL_ARMOR_TEMPLATE_MONTHLY_USD:.2f}/mo; "
            f"target POST {redacted_url} (pass --apply to provision live)",
            file=sys.stderr,
        )
        print(json.dumps(build_template_body(), indent=2), file=sys.stderr)
        print(f"MODEL_ARMOR_LOCATION={location}")
        print(f"MODEL_ARMOR_TEMPLATE_ID={short_id}")
        return 0

    from app.core.gcp_clients import get_access_token

    merged_env = {**env_map, "DEMO_MODE": "live", "MODEL_ARMOR_LOCATION": location}
    try:
        settings: ArenaSettings = get_settings(merged_env)
        client = ModelArmorClient(
            project_id=settings.gcp_project_id,
            location=location,
            template_id=short_id,
            token_provider=lambda: get_access_token(settings),
            attempt_timeout_seconds=30.0,
            budget_seconds=90.0,
        )
        state, resolved_id = ensure_template(client, template_id=short_id)
    except Exception as exc:  # boundary: redact identity tokens and exit non-zero (fail loud)
        proj = env_map.get("GCP_PROJECT_ID", "")
        print(
            f"ERROR {type(exc).__name__}: {redact_model_armor_text(str(exc), proj, short_id)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Model Armor template ({location}, ${MODEL_ARMOR_TEMPLATE_MONTHLY_USD:.2f}/mo): {state}",
        file=sys.stderr,
    )
    print(f"MODEL_ARMOR_LOCATION={location}")
    print(f"MODEL_ARMOR_TEMPLATE_ID={resolved_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
