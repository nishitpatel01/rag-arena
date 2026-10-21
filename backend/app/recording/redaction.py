"""Recording redaction (spec §10 leak rule): identity values are replaced before any recording is written.

Exact (substring) occurrences of the project id, corpus bucket, ``CLOUDSDK_CONFIG``,
``GOOGLE_APPLICATION_CREDENTIALS`` and the user's home directory are replaced by ``${NAME}`` placeholders in
every string and dict key, recursively; project NUMBERS in resource names (``projects/<digits>``) become
``projects/${GCP_PROJECT_NUMBER}``. ``assert_redacted`` then proves none remain (raises otherwise).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from app.core.config import ArenaSettings

_MAX_DEPTH = 64
_MIN_TOKEN_CHARS = 4
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{6,}")
PROJECT_NUMBER_PLACEHOLDER = "projects/${GCP_PROJECT_NUMBER}"


class RedactionError(RuntimeError):
    """Raised when an identity value would survive into a recording."""


def redaction_tokens(settings: ArenaSettings, *, home: str | None = None) -> tuple[tuple[str, str], ...]:
    """``(value, placeholder)`` pairs, longest value first so nested values (bucket ⊃ project) redact correctly."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    home_dir = str(Path.home()) if home is None else home
    candidates = (
        (settings.gcp_project_id, "${GCP_PROJECT_ID}"),
        (settings.gcs_corpus_bucket, "${GCS_CORPUS_BUCKET}"),
        (settings.cloudsdk_config, "${CLOUDSDK_CONFIG}"),
        (settings.google_application_credentials, "${GOOGLE_APPLICATION_CREDENTIALS}"),
        (home_dir, "${HOME}"),
    )
    pairs = {value.strip(): label for value, label in candidates if isinstance(value, str) and len(value.strip()) >= _MIN_TOKEN_CHARS}
    return tuple(sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True))


def _redact_str(text: str, tokens: tuple[tuple[str, str], ...]) -> str:
    for value, label in tokens:
        text = text.replace(value, label)
    return _PROJECT_NUMBER_RE.sub(PROJECT_NUMBER_PLACEHOLDER, text)


def redact_text(text: str, tokens: tuple[tuple[str, str], ...]) -> str:
    """Redact one string (identity tokens + project numbers)."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _redact_str(text, tokens)


def redact_value(value: Any, tokens: tuple[tuple[str, str], ...], _depth: int = 0) -> Any:
    """Recursively redact strings, dict keys, lists and tuples (tuples become lists, as in JSON)."""
    if _depth > _MAX_DEPTH:
        raise RedactionError(f"recording nesting exceeds {_MAX_DEPTH} levels; refusing to write an unscanned document")
    if isinstance(value, str):
        return _redact_str(value, tokens)
    if isinstance(value, dict):
        return {_redact_str(str(k), tokens): redact_value(v, tokens, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, tokens, _depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise RedactionError(f"unsupported value type in recording: {type(value).__name__}")


def assert_redacted(value: Any, tokens: tuple[tuple[str, str], ...]) -> None:
    """Raise ``RedactionError`` naming the placeholder of any identity value still present."""
    blob = json.dumps(value, ensure_ascii=False)
    leaked = [label for token, label in tokens if token in blob]
    if _PROJECT_NUMBER_RE.search(blob):
        leaked.append("${GCP_PROJECT_NUMBER}")
    if leaked:
        raise RedactionError(f"recording still contains identity values for {leaked}")


__all__ = [
    "PROJECT_NUMBER_PLACEHOLDER",
    "RedactionError",
    "assert_redacted",
    "redact_text",
    "redact_value",
    "redaction_tokens",
]
