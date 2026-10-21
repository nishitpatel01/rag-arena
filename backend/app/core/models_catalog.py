"""Selectable model catalog for synthesis and LLM-as-a-judge (owner request 2026-10-21).

The catalog is defined in code (ids, labels, providers); everything else is resolved, never guessed:

* **location** — ``MODEL_LOCATION_<ID>`` override (``<ID>`` = id upper-cased, non-alphanumerics → ``_``), default
  ``global`` (the only endpoint where all four models were probed).
* **price** — ``app.core.pricing.PRICE_TABLE`` (published Vertex AI rates with ``source`` + ``price_as_of``). A model
  without a verified price is ``available: false`` with reason ``"price not verified"``.
* **availability** — ``data/model_verification.json`` written by ``scripts/verify_models.py`` (one bounded live call
  per model). A model that is missing from the file or whose call failed is ``available: false`` and carries the
  recorded (redacted) error verbatim as ``unavailable_reason``.

``require`` is the single validation gate used by ``POST /api/v1/battle/stream``: unknown id, unavailable model,
role not allowed and judge == synthesis are all rejected (HTTP 400 before streaming). There is no fallback model.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from app.core.config import ArenaSettings
from app.core.pricing import PRICE_TABLE

PROVIDER_VERTEX_GEMINI = "vertex_gemini"
PROVIDER_ANTHROPIC_VERTEX = "anthropic_vertex"
PROVIDERS: tuple[str, ...] = (PROVIDER_VERTEX_GEMINI, PROVIDER_ANTHROPIC_VERTEX)
ROLE_SYNTHESIS = "synthesis"
ROLE_JUDGE = "judge"
ROLES: tuple[str, ...] = (ROLE_SYNTHESIS, ROLE_JUDGE)
DEFAULT_MODEL_LOCATION = "global"
PRICE_NOT_VERIFIED = "price not verified"
NOT_VERIFIED = "not verified: run scripts/verify_models.py (no entry in data/model_verification.json)"
VERIFICATION_FORMAT_VERSION = 1
_MAX_VERIFICATION_BYTES = 256 * 1024
_MAX_VERIFICATION_ENTRIES = 64
_MAX_REASON_CHARS = 600

# (id, label, provider) — the owner's initial catalog: Gemini 4 Argon, Gemini 3.8 Flash, Claude Opus 5.5, Claude Fable.
CATALOG_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("gemini-4-argon", "Gemini 4 Argon", PROVIDER_VERTEX_GEMINI),
    ("gemini-3.8-flash", "Gemini 3.8 Flash", PROVIDER_VERTEX_GEMINI),
    ("claude-opus-5-5", "Claude Opus 5.5", PROVIDER_ANTHROPIC_VERTEX),
    ("claude-fable-5-1", "Claude Fable 5.1", PROVIDER_ANTHROPIC_VERTEX),
)


class ModelSelectionError(ValueError):
    """A requested synthesis/judge model is unknown, unavailable, not allowed for the role, or judge == synthesis."""


class VerificationFileError(ValueError):
    """``data/model_verification.json`` exists but is malformed."""


@dataclass(frozen=True)
class ModelSpec:
    """One selectable model with its resolved location, published price and verified availability."""

    id: str
    label: str
    provider: str
    location: str
    roles: tuple[str, ...]
    input_usd_per_1m: float
    output_usd_per_1m: float
    price_source: str
    price_as_of: str
    available: bool
    unavailable_reason: str | None
    verified_at: str | None

    def __post_init__(self) -> None:
        if not self.id or not self.label:
            raise ValueError("ModelSpec id and label must be non-empty")
        if self.provider not in PROVIDERS:
            raise ValueError(f"ModelSpec.provider must be one of {PROVIDERS}, got {self.provider!r}")
        if not self.roles or any(role not in ROLES for role in self.roles):
            raise ValueError(f"ModelSpec.roles must be a non-empty subset of {ROLES}")
        if self.available == bool(self.unavailable_reason):
            raise ValueError("an available model has no unavailable_reason and an unavailable model must have one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "location": self.location,
            "roles": list(self.roles),
            "input_usd_per_1m": self.input_usd_per_1m,
            "output_usd_per_1m": self.output_usd_per_1m,
            "price_source": self.price_source,
            "price_as_of": self.price_as_of,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class RunModels:
    """The (synthesis, judge) pair selected for one run; an LLM never grades its own answers."""

    synthesis: ModelSpec
    judge: ModelSpec

    def __post_init__(self) -> None:
        if not isinstance(self.synthesis, ModelSpec) or not isinstance(self.judge, ModelSpec):
            raise TypeError("synthesis and judge must be ModelSpec instances")
        if self.synthesis.id == self.judge.id:
            raise ModelSelectionError(_same_model_message(self.synthesis.id))

    @property
    def pair(self) -> tuple[str, str]:
        return self.synthesis.id, self.judge.id


def _same_model_message(model_id: str) -> str:
    return f"judge_model must differ from synthesis_model (both '{model_id}'): an LLM must not grade its own answers"


@dataclass(frozen=True)
class ModelCatalog:
    """Immutable catalog with the request-validation gate."""

    models: tuple[ModelSpec, ...]

    def get(self, model_id: str) -> ModelSpec:
        for spec in self.models:
            if spec.id == model_id:
                return spec
        raise ModelSelectionError(f"Unknown model '{model_id}'; catalog models: {[m.id for m in self.models]}")

    def require(self, model_id: Any, role: str) -> ModelSpec:
        """Return the spec for ``model_id`` if it exists, is available and may serve ``role``; raise otherwise."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")
        field = f"{role}_model"
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelSelectionError(f"Field '{field}' must be a non-empty model id string")
        known = [m.id for m in self.models]
        if model_id.strip() not in known:
            raise ModelSelectionError(f"Unknown {field} '{model_id.strip()}'; catalog models: {known}")
        spec = self.get(model_id.strip())
        if role not in spec.roles:
            raise ModelSelectionError(f"Model '{spec.id}' is not allowed as {role} (roles: {list(spec.roles)})")
        if not spec.available:
            raise ModelSelectionError(f"Model '{spec.id}' is unavailable: {spec.unavailable_reason}")
        return spec

    def select(self, synthesis_model: Any, judge_model: Any) -> RunModels:
        """Validate both selections and the judge ≠ synthesis rule."""
        synthesis = self.require(synthesis_model, ROLE_SYNTHESIS)
        judge = self.require(judge_model, ROLE_JUDGE)
        if synthesis.id == judge.id:
            raise ModelSelectionError(_same_model_message(synthesis.id))
        return RunModels(synthesis=synthesis, judge=judge)


def location_env_suffix(model_id: str) -> str:
    """``gemini-3.8-flash`` → ``GEMINI_3_8_FLASH`` (the ``MODEL_LOCATION_<ID>`` key suffix)."""
    return re.sub(r"[^A-Z0-9]", "_", model_id.upper())


def load_verification(path: Path) -> dict[str, dict[str, Any]]:
    """``model id → entry`` from the verification file; a missing file means nothing is verified (empty dict)."""
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    if not path.is_file():
        return {}
    if path.stat().st_size > _MAX_VERIFICATION_BYTES:
        raise VerificationFileError(f"{path.name} exceeds {_MAX_VERIFICATION_BYTES} bytes")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationFileError(f"{path.name} is not valid JSON: {exc}") from exc
    entries = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_VERIFICATION_ENTRIES:
        raise VerificationFileError(f"{path.name} must hold a 'models' list of at most {_MAX_VERIFICATION_ENTRIES} entries")
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("ok"), bool):
            raise VerificationFileError(f"{path.name} entries need a string 'id' and a boolean 'ok'")
        out[entry["id"]] = entry
    return out


def _availability(model_id: str, entry: dict[str, Any] | None) -> tuple[bool, str | None, str | None]:
    """(available, unavailable_reason, verified_at) from price + verification evidence."""
    verified_at = str(entry.get("verified_at")) if entry and entry.get("verified_at") else None
    price = PRICE_TABLE.get(model_id)
    if price is None or not price.source or not price.price_as_of or price.input_usd_per_1m <= 0:
        return False, PRICE_NOT_VERIFIED, verified_at
    if entry is None:
        return False, NOT_VERIFIED, None
    if not entry["ok"]:
        status = entry.get("http_status")
        message = str(entry.get("message") or "live verification call failed").strip()[:_MAX_REASON_CHARS]
        return False, (f"HTTP {status}: {message}" if status else message), verified_at
    return True, None, verified_at


def _spec(model_id: str, label: str, provider: str, settings: ArenaSettings, entry: dict[str, Any] | None) -> ModelSpec:
    locations = dict(settings.model_locations)
    available, reason, verified_at = _availability(model_id, entry)
    price = PRICE_TABLE.get(model_id)
    return ModelSpec(
        id=model_id,
        label=label,
        provider=provider,
        location=locations.get(location_env_suffix(model_id), DEFAULT_MODEL_LOCATION),
        roles=ROLES,
        input_usd_per_1m=float(price.input_usd_per_1m) if price else 0.0,
        output_usd_per_1m=float(price.output_usd_per_1m) if price else 0.0,
        price_source=price.source if price else "",
        price_as_of=price.price_as_of if price else "",
        available=available,
        unavailable_reason=reason,
        verified_at=verified_at,
    )


def load_catalog(settings: ArenaSettings, verification_path: Path | None = None) -> ModelCatalog:
    """Build the catalog: code-defined entries + env locations + published prices + verification evidence."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    verification = load_verification(verification_path or settings.model_verification_path)
    return ModelCatalog(
        models=tuple(_spec(mid, label, provider, settings, verification.get(mid)) for mid, label, provider in CATALOG_ENTRIES)
    )


def default_selection_ids(settings: ArenaSettings) -> tuple[str, str]:
    """Default (synthesis, judge) ids when a request omits them: ``GEMINI_FAST_MODEL`` and ``JUDGE_MODEL``."""
    return settings.gemini_fast_model, settings.judge_model


def spec_for_settings_model(settings: ArenaSettings, model_id: str) -> ModelSpec:
    """Catalog entry for a settings-configured model id (availability is the caller's concern); unknown ids raise."""
    return load_catalog(settings).get(model_id)


__all__ = [
    "CATALOG_ENTRIES",
    "DEFAULT_MODEL_LOCATION",
    "ModelCatalog",
    "ModelSelectionError",
    "ModelSpec",
    "NOT_VERIFIED",
    "PRICE_NOT_VERIFIED",
    "PROVIDER_ANTHROPIC_VERTEX",
    "PROVIDER_VERTEX_GEMINI",
    "ROLES",
    "ROLE_JUDGE",
    "ROLE_SYNTHESIS",
    "RunModels",
    "VERIFICATION_FORMAT_VERSION",
    "VerificationFileError",
    "default_selection_ids",
    "load_catalog",
    "load_verification",
    "location_env_suffix",
    "spec_for_settings_model",
]
