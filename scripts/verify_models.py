#!/usr/bin/env python3
"""Live availability probe for every model in the catalog → ``data/model_verification.json``.

One bounded live call per model (no retries), 60 s timeout each:

* Gemini (``vertex_gemini``): google-genai ``models.generate_content`` on Vertex AI.
* Claude (``anthropic_vertex``): Vertex ``:rawPredict`` (Anthropic Messages, ``anthropic_version=vertex-2023-10-16``,
  never ``temperature``).

The output contains only model ids, the ok flag, the HTTP status, a redacted message and ``verified_at`` — no
identity values (project id/number, bucket, credential paths are redacted and the file is scanned before
writing). It never changes any project or publisher configuration.

Usage (identity from the git-ignored .env): ``PYTHONPATH=backend .venv/bin/python scripts/verify_models.py``
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.core.anthropic_vertex import ANTHROPIC_VERTEX_VERSION, message_text, raw_predict_url  # noqa: E402
from app.core.config import ArenaSettings, get_settings  # noqa: E402
from app.core.gcp_clients import get_access_token, get_genai_client  # noqa: E402
from app.core.models_catalog import (  # noqa: E402
    PROVIDER_ANTHROPIC_VERTEX,
    PROVIDER_VERTEX_GEMINI,
    VERIFICATION_FORMAT_VERSION,
    ModelSpec,
    load_catalog,
)
from app.recording.redaction import assert_redacted, redact_text, redaction_tokens  # noqa: E402

CALL_TIMEOUT_SECONDS = 60
PROBE_PROMPT = "Reply with the single word OK."
PROBE_MAX_TOKENS = 64
_MAX_MESSAGE_CHARS = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_message(text: str) -> str:
    """``error.message`` from a Google/Anthropic JSON error body, else the raw text (bounded)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return text.strip()[:_MAX_MESSAGE_CHARS]
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"]).strip()[:_MAX_MESSAGE_CHARS]
    return text.strip()[:_MAX_MESSAGE_CHARS]


def probe_claude(settings: ArenaSettings, spec: ModelSpec) -> tuple[bool, int | None, str]:
    """One rawPredict call; returns (ok, http_status, message)."""
    body = {
        "anthropic_version": ANTHROPIC_VERTEX_VERSION,
        "max_tokens": PROBE_MAX_TOKENS,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
    }
    headers = {"Authorization": f"Bearer {get_access_token(settings)}", "Content-Type": "application/json; charset=utf-8"}
    url = raw_predict_url(settings.gcp_project_id, spec.location, spec.id)
    response = httpx.post(url, headers=headers, json=body, timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS))
    if response.status_code != 200:
        return False, response.status_code, _error_message(response.text)
    text = message_text(response.json()).strip()
    return True, 200, (text[:80] or "200 OK (empty text)")


def probe_gemini(settings: ArenaSettings, spec: ModelSpec) -> tuple[bool, int | None, str]:
    """One generate_content call; google-genai raises ``APIError`` (with ``.code``) on non-2xx."""
    client = get_genai_client(settings, location=spec.location)
    resp = client.models.generate_content(
        model=spec.id,
        contents=PROBE_PROMPT,
        config={"max_output_tokens": 256, "http_options": {"timeout": CALL_TIMEOUT_SECONDS * 1000}},
    )
    text = str(getattr(resp, "text", "") or "").strip()
    return True, 200, (text[:80] or "200 OK (empty text)")


def _probe(settings: ArenaSettings, spec: ModelSpec) -> dict[str, Any]:
    probes: dict[str, Callable[[ArenaSettings, ModelSpec], tuple[bool, int | None, str]]] = {
        PROVIDER_VERTEX_GEMINI: probe_gemini,
        PROVIDER_ANTHROPIC_VERTEX: probe_claude,
    }
    try:
        ok, status, message = probes[spec.provider](settings, spec)
    except Exception as exc:  # boundary: every failure is recorded verbatim (redacted) as ok=false, never hidden
        code = getattr(exc, "code", None)
        status = code if isinstance(code, int) else None
        ok, message = False, f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:_MAX_MESSAGE_CHARS]
    return {"id": spec.id, "ok": ok, "http_status": status, "message": message, "verified_at": _utc_now()}


def verify_all(settings: ArenaSettings) -> dict[str, Any]:
    """Probe every catalog model once (sequential, bounded by the catalog size) and redact the result."""
    tokens = redaction_tokens(settings)
    entries = []
    for spec in load_catalog(settings).models[:16]:
        entry = _probe(settings, spec)
        entry["message"] = redact_text(str(entry["message"]), tokens)
        entries.append(entry)
        print(f"{spec.id:20s} ok={entry['ok']!s:5s} http={entry['http_status']} {entry['message'][:160]}")
    doc = {"format_version": VERIFICATION_FORMAT_VERSION, "verified_at": _utc_now(), "models": entries}
    assert_redacted(doc, tokens)
    return doc


def write_verification(doc: dict[str, Any], path: Path) -> None:
    """Atomic write (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 when any model fails verification")
    args = parser.parse_args(argv)
    base = get_settings()
    settings = base if base.is_live else replace(base, demo_mode="live")
    doc = verify_all(settings)
    write_verification(doc, settings.model_verification_path)
    print(f"wrote {settings.model_verification_path.relative_to(_PROJECT_ROOT)}")
    failed = [e["id"] for e in doc["models"] if not e["ok"]]
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
