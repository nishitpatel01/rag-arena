"""Google Cloud Model Armor REST v1 client, strict response parser, and pricing helpers.

Endpoints (regional REP host ``https://modelarmor.<location>.rep.googleapis.com``):
* ``POST /v1/projects/<p>/locations/<l>/templates/<t>:sanitizeUserPrompt``
  body ``{"userPromptData": {"text": "<prompt or untrusted chunk>"}}``
* ``POST /v1/projects/<p>/locations/<l>/templates/<t>:sanitizeModelResponse``
  body ``{"modelResponseData": {"text": "<answer>"}, "userPrompt": "<optional prompt>"}``
* ``GET | POST /v1/projects/<p>/locations/<l>/templates`` (idempotent ``$0/mo`` template provisioning)

Honesty invariants (``spec.md §2``):
* Zero local regex/keyword fallbacks — every guardrail check calls the real Google Cloud Model Armor REST API.
* When ``guardrail == "model_armor"`` in ``live``/``record`` mode and ``MODEL_ARMOR_TEMPLATE_ID`` is unset,
  ``ensure_model_armor_configured`` fails loud with setup instructions pointing to
  ``scripts/provision_model_armor.py --apply``.
* Retries are bounded (``<= MAX_ATTEMPTS``) and occur ONLY on transient ``429/500/502/503/504`` or HTTP timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable

import httpx

from app.core.config import ArenaSettings

MODEL_ARMOR_SERVICE_LABEL = "Google Cloud Model Armor"
MODEL_ARMOR_PRICING_SOURCE = "https://cloud.google.com/security-command-center/pricing#model-armor"
MODEL_ARMOR_PRICE_AS_OF = "2026-11-17"
MODEL_ARMOR_TEMPLATE_MONTHLY_USD: float = 0.0
MODEL_ARMOR_FREE_TIER_TOKENS: int = 2_000_000
MODEL_ARMOR_USD_PER_1M_TOKENS: float = 1.50

RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.4
MAX_SANITIZE_TEXT_CHARS = 16_000
_MIN_ATTEMPT_SECONDS = 0.5
_MAX_ERROR_BODY_CHARS = 500

_LOCATION_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_SHORT_TEMPLATE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_RESOURCE_TEMPLATE_RE = re.compile(
    r"^projects/[A-Za-z0-9._-]+/locations/[a-z][a-z0-9-]{1,30}/templates/([a-zA-Z0-9][a-zA-Z0-9_-]{0,62})$"
)
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{6,}")
_VALID_MATCH_STATES: frozenset[str] = frozenset({"NO_MATCH_FOUND", "MATCH_FOUND"})


class ModelArmorConfigurationError(RuntimeError):
    """Raised in live/record mode when ``guardrail == 'model_armor'`` has no configured template ID."""


class ModelArmorHTTPError(RuntimeError):
    """Non-2xx HTTP response from Google Cloud Model Armor (after bounded retries if transient)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelArmorResponseError(ValueError):
    """Raised when a 200 response from Model Armor violates the documented ``sanitizationResult`` contract."""


@dataclass(frozen=True)
class FilterVerdict:
    """Normalized execution + match verdict for one Model Armor filter family."""

    filter_name: str
    match_state: str
    execution_state: str = "EXECUTION_SUCCESS"
    confidence_level: str = ""
    details: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.match_state == "MATCH_FOUND"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "filter": self.filter_name,
            "match_state": self.match_state,
            "execution_state": self.execution_state,
        }
        if self.confidence_level:
            out["confidence_level"] = self.confidence_level
        if self.details:
            out["details"] = list(self.details)
        return out


@dataclass(frozen=True)
class SanitizationOutcome:
    """Parsed result of ``:sanitizeUserPrompt`` or ``:sanitizeModelResponse`` with measured call provenance."""

    match_state: str
    invocation_result: str
    matched_filters: tuple[str, ...]
    filter_verdicts: tuple[FilterVerdict, ...]
    tokens_screened: int = 0
    attempts: int = 1
    duration_ms: int = 0
    truncated: bool = False

    @property
    def matched(self) -> bool:
        return self.match_state == "MATCH_FOUND"

    def summary_dict(self) -> dict[str, Any]:
        return {
            "match_state": self.match_state,
            "matched": self.matched,
            "invocation_result": self.invocation_result,
            "matched_filters": list(self.matched_filters),
            "filter_verdicts": [v.to_dict() for v in self.filter_verdicts],
            "tokens_screened": self.tokens_screened,
            "cost_usd": model_armor_cost_usd(self.tokens_screened),
            "attempts": self.attempts,
            "latency_ms": self.duration_ms,
            "truncated": self.truncated,
        }


def validate_location(location: str) -> str:
    if not isinstance(location, str) or not _LOCATION_RE.fullmatch(location.strip()):
        raise ValueError(f"MODEL_ARMOR_LOCATION must be a valid GCP region (e.g. 'us-central1'), got {location!r}")
    return location.strip()


def extract_short_template_id(raw_template_id: str) -> str:
    """Accept either a short template ID or ``projects/<p>/locations/<l>/templates/<id>``; return ``<id>``."""
    if not isinstance(raw_template_id, str) or not raw_template_id.strip():
        raise ValueError("template_id must be a non-empty string")
    cleaned = raw_template_id.strip()
    if _SHORT_TEMPLATE_ID_RE.fullmatch(cleaned):
        return cleaned
    match = _RESOURCE_TEMPLATE_RE.fullmatch(cleaned)
    if match:
        return match.group(1)
    raise ValueError(
        "MODEL_ARMOR_TEMPLATE_ID must be a short template id ([a-zA-Z0-9_-]{1,63}) "
        f"or 'projects/<project>/locations/<location>/templates/<id>', got {raw_template_id!r}"
    )


def model_armor_host(location: str) -> str:
    loc = validate_location(location)
    return f"https://modelarmor.{loc}.rep.googleapis.com"


def templates_collection_url(project_id: str, location: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    loc = validate_location(location)
    return f"{model_armor_host(loc)}/v1/projects/{project_id.strip()}/locations/{loc}/templates"


def template_resource_url(project_id: str, location: str, template_id: str) -> str:
    short_id = extract_short_template_id(template_id)
    return f"{templates_collection_url(project_id, location)}/{short_id}"


def sanitize_user_prompt_url(project_id: str, location: str, template_id: str) -> str:
    return f"{template_resource_url(project_id, location, template_id)}:sanitizeUserPrompt"


def sanitize_model_response_url(project_id: str, location: str, template_id: str) -> str:
    return f"{template_resource_url(project_id, location, template_id)}:sanitizeModelResponse"


def redact_model_armor_text(text: str, project_id: str = "", template_id: str = "") -> str:
    """Redact project ID, template ID, and 6+ digit project numbers from diagnostic/error strings."""
    out = str(text or "")
    for raw_val, placeholder in (
        (template_id.strip() if isinstance(template_id, str) else "", "${MODEL_ARMOR_TEMPLATE_ID}"),
        (project_id.strip() if isinstance(project_id, str) else "", "${GCP_PROJECT_ID}"),
    ):
        if raw_val:
            out = out.replace(raw_val, placeholder)
    return _PROJECT_NUMBER_RE.sub("projects/${GCP_PROJECT_NUMBER}", out)


def model_armor_tokens(text: str) -> int:
    """Published Model Armor token count: 4 non-whitespace UTF-8 code points per token (rounded up)."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    non_ws = sum(1 for ch in text if not ch.isspace())
    if non_ws == 0:
        return 0
    return -(-non_ws // 4)


def model_armor_cost_usd(tokens: int, *, apply_free_tier: bool = False) -> float:
    """USD cost of screening ``tokens`` via Google Cloud Model Armor ($0/mo template; $1.50 / 1M tokens list)."""
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise ValueError(f"tokens must be an integer >= 0, got {tokens!r}")
    billable = max(0, tokens - MODEL_ARMOR_FREE_TIER_TOKENS) if apply_free_tier else tokens
    return round((billable / 1_000_000.0) * MODEL_ARMOR_USD_PER_1M_TOKENS, 8)


def model_armor_pricing_summary(tokens: int) -> dict[str, Any]:
    """Transparent pricing breakdown included in guardrail stage payloads."""
    return {
        "tokens_screened": tokens,
        "cost_usd": model_armor_cost_usd(tokens, apply_free_tier=False),
        "free_tier_adjusted_cost_usd": model_armor_cost_usd(tokens, apply_free_tier=True),
        "usd_per_1m_tokens": MODEL_ARMOR_USD_PER_1M_TOKENS,
        "free_tier_tokens_per_month": MODEL_ARMOR_FREE_TIER_TOKENS,
        "template_monthly_usd": MODEL_ARMOR_TEMPLATE_MONTHLY_USD,
        "pricing_source": MODEL_ARMOR_PRICING_SOURCE,
        "price_as_of": MODEL_ARMOR_PRICE_AS_OF,
    }


def ensure_model_armor_configured(settings: ArenaSettings) -> tuple[str, str]:
    """Return ``(location, short_template_id)`` or fail loud with ``provision_model_armor.py --apply`` guidance."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    loc = validate_location(settings.model_armor_location)
    raw_tpl = settings.model_armor_template_id.strip()
    if not raw_tpl:
        raise ModelArmorConfigurationError(
            "MODEL_ARMOR_TEMPLATE_ID is not set for live Google Cloud Model Armor screening. "
            "Provision the $0/mo guardrail template with "
            "`PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py --apply` "
            "and add MODEL_ARMOR_TEMPLATE_ID to your git-ignored .env."
        )
    return loc, extract_short_template_id(raw_tpl)


def _find_nested_dict(filter_results: dict[str, Any], target_key: str) -> dict[str, Any] | None:
    direct = filter_results.get(target_key)
    if isinstance(direct, dict):
        return direct
    for entry in filter_results.values():
        if isinstance(entry, dict) and isinstance(entry.get(target_key), dict):
            return entry[target_key]
    return None


def _parse_pi_verdict(block: dict[str, Any]) -> tuple[FilterVerdict, tuple[str, ...]]:
    state = str(block.get("matchState") or "NO_MATCH_FOUND")
    if state not in _VALID_MATCH_STATES:
        raise ModelArmorResponseError(f"Invalid piAndJailbreakFilterResult.matchState: {state!r}")
    conf = str(block.get("confidenceLevel") or "")
    exec_state = str(block.get("executionState") or "EXECUTION_SUCCESS")
    verdict = FilterVerdict("pi_and_jailbreak", state, exec_state, conf)
    return verdict, (("pi_and_jailbreak",) if verdict.matched else ())


def _parse_rai_verdict(block: dict[str, Any]) -> tuple[FilterVerdict, tuple[str, ...]]:
    state = str(block.get("matchState") or "NO_MATCH_FOUND")
    if state not in _VALID_MATCH_STATES:
        raise ModelArmorResponseError(f"Invalid raiFilterResult.matchState: {state!r}")
    exec_state = str(block.get("executionState") or "EXECUTION_SUCCESS")
    sub = block.get("raiFilterTypeResults")
    categories: list[str] = []
    items: list[Any] = []
    if isinstance(sub, dict):
        items = list(sub.values())
    elif isinstance(sub, list):
        items = sub
    for item in items[:32]:
        if isinstance(item, dict) and item.get("matchState") == "MATCH_FOUND":
            cat = str(item.get("filterType") or item.get("name") or "RAI").strip()
            if cat:
                categories.append(cat)
    if categories and state != "MATCH_FOUND":
        state = "MATCH_FOUND"
    matched_tags = tuple(f"rai:{c}" for c in categories) if categories else (("rai",) if state == "MATCH_FOUND" else ())
    return FilterVerdict("rai", state, exec_state, details=tuple(categories)), matched_tags


def _parse_sdp_verdict(block: dict[str, Any]) -> tuple[FilterVerdict, tuple[str, ...]]:
    inner = (
        block.get("inspectResult")
        if isinstance(block.get("inspectResult"), dict)
        else block.get("deidentifyResult")
        if isinstance(block.get("deidentifyResult"), dict)
        else block
    )
    state = str(inner.get("matchState") or block.get("matchState") or "NO_MATCH_FOUND")
    if state not in _VALID_MATCH_STATES:
        raise ModelArmorResponseError(f"Invalid sdpFilterResult.matchState: {state!r}")
    exec_state = str(inner.get("executionState") or block.get("executionState") or "EXECUTION_SUCCESS")
    info_types: list[str] = []
    findings = inner.get("findings")
    if isinstance(findings, list):
        for finding in findings[:32]:
            if isinstance(finding, dict):
                it = str(finding.get("infoType") or "").strip()
                if it and it not in info_types:
                    info_types.append(it)
    if info_types and state != "MATCH_FOUND":
        state = "MATCH_FOUND"
    matched_tags = tuple(f"sdp:{it}" for it in info_types) if info_types else (("sdp",) if state == "MATCH_FOUND" else ())
    return FilterVerdict("sdp", state, exec_state, details=tuple(info_types)), matched_tags


def _parse_malicious_uri_verdict(block: dict[str, Any]) -> tuple[FilterVerdict, tuple[str, ...]]:
    state = str(block.get("matchState") or "NO_MATCH_FOUND")
    if state not in _VALID_MATCH_STATES:
        raise ModelArmorResponseError(f"Invalid maliciousUriFilterResult.matchState: {state!r}")
    exec_state = str(block.get("executionState") or "EXECUTION_SUCCESS")
    matched_items = block.get("maliciousUriMatchedItems")
    count = len(matched_items) if isinstance(matched_items, list) else 0
    if count > 0 and state != "MATCH_FOUND":
        state = "MATCH_FOUND"
    details = (f"matched_uris={count}",) if count > 0 else ()
    return (
        FilterVerdict("malicious_uri", state, exec_state, details=details),
        ("malicious_uri",) if state == "MATCH_FOUND" else (),
    )


def parse_sanitization_response(
    doc: Any,
    *,
    tokens_screened: int = 0,
    attempts: int = 1,
    duration_ms: int = 0,
    truncated: bool = False,
) -> SanitizationOutcome:
    """Strictly validate and normalize a Model Armor ``SanitizeUserPrompt`` / ``SanitizeModelResponse`` payload."""
    if not isinstance(doc, dict) or not isinstance(doc.get("sanitizationResult"), dict):
        raise ModelArmorResponseError("Model Armor response is missing object field 'sanitizationResult'")
    sr = doc["sanitizationResult"]
    match_state = sr.get("filterMatchState")
    if not isinstance(match_state, str) or match_state not in _VALID_MATCH_STATES:
        raise ModelArmorResponseError(f"Invalid sanitizationResult.filterMatchState: {match_state!r}")
    invocation = str(sr.get("invocationResult") or "SUCCESS")
    if invocation == "FAILURE":
        raise ModelArmorResponseError("Model Armor reported invocationResult='FAILURE'; refusing to treat as safe")

    raw_filters = sr.get("filterResults", {})
    if raw_filters is None:
        raw_filters = {}
    if not isinstance(raw_filters, dict):
        raise ModelArmorResponseError("sanitizationResult.filterResults must be a JSON object")

    verdicts: list[FilterVerdict] = []
    matched_labels: list[str] = []
    parsers: tuple[tuple[str, Callable[[dict[str, Any]], tuple[FilterVerdict, tuple[str, ...]]]], ...] = (
        ("piAndJailbreakFilterResult", _parse_pi_verdict),
        ("raiFilterResult", _parse_rai_verdict),
        ("sdpFilterResult", _parse_sdp_verdict),
        ("maliciousUriFilterResult", _parse_malicious_uri_verdict),
    )
    for field_name, parser in parsers:
        block = _find_nested_dict(raw_filters, field_name)
        if block is not None:
            verdict, tags = parser(block)
            verdicts.append(verdict)
            for tag in tags:
                if tag not in matched_labels:
                    matched_labels.append(tag)

    effective_state = "MATCH_FOUND" if (match_state == "MATCH_FOUND" or bool(matched_labels)) else "NO_MATCH_FOUND"
    if effective_state == "MATCH_FOUND" and not matched_labels:
        matched_labels.append("policy_violation")

    return SanitizationOutcome(
        match_state=effective_state,
        invocation_result=invocation,
        matched_filters=tuple(matched_labels),
        filter_verdicts=tuple(verdicts),
        tokens_screened=tokens_screened,
        attempts=attempts,
        duration_ms=duration_ms,
        truncated=truncated,
    )


class ModelArmorClient:
    """Bounded-retry Google Cloud Model Armor REST v1 client (sanitization + template provisioning)."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        template_id: str,
        token_provider: Callable[[], str],
        attempt_timeout_seconds: float = 10.0,
        budget_seconds: float = 25.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        post: Callable[..., httpx.Response] | None = None,
        send: Callable[..., httpx.Response] | None = None,
    ) -> None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if attempt_timeout_seconds <= 0 or budget_seconds <= 0:
            raise ValueError("attempt_timeout_seconds and budget_seconds must be > 0")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self.project_id = project_id.strip()
        self.location = validate_location(location)
        self.template_id = extract_short_template_id(template_id)
        self.user_prompt_url = sanitize_user_prompt_url(self.project_id, self.location, self.template_id)
        self.model_response_url = sanitize_model_response_url(self.project_id, self.location, self.template_id)
        self._attempt_timeout = float(attempt_timeout_seconds)
        self._budget = float(budget_seconds)
        self._token_provider = token_provider
        self._sleep = sleep
        self._clock = clock
        self._post = post
        self._send = send

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-goog-user-project": self.project_id,
            "Content-Type": "application/json",
        }

    def _prepare_text(self, text: str, field_name: str) -> tuple[str, bool]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        body = text.strip()
        return body[:MAX_SANITIZE_TEXT_CHARS], len(body) > MAX_SANITIZE_TEXT_CHARS

    def sanitize_user_prompt(self, text: str) -> SanitizationOutcome:
        """Screen untrusted user prompt or retrieved document text via ``:sanitizeUserPrompt``."""
        bounded, truncated = self._prepare_text(text, "user_prompt")
        tokens = model_armor_tokens(bounded)
        payload = {"userPromptData": {"text": bounded}}
        start = self._clock()
        doc, attempts = self.request("POST", self.user_prompt_url, body=payload)
        elapsed_ms = max(0, int((self._clock() - start) * 1000))
        return parse_sanitization_response(
            doc, tokens_screened=tokens, attempts=attempts, duration_ms=elapsed_ms, truncated=truncated
        )

    def sanitize_model_response(self, text: str, *, user_prompt: str | None = None) -> SanitizationOutcome:
        """Screen synthesized LLM output via ``:sanitizeModelResponse`` before returning it to the user."""
        bounded, truncated = self._prepare_text(text, "model_response")
        tokens = model_armor_tokens(bounded)
        payload: dict[str, Any] = {"modelResponseData": {"text": bounded}}
        if isinstance(user_prompt, str) and user_prompt.strip():
            prompt_bounded = user_prompt.strip()[:MAX_SANITIZE_TEXT_CHARS]
            payload["userPrompt"] = prompt_bounded
            tokens += model_armor_tokens(prompt_bounded)
        start = self._clock()
        doc, attempts = self.request("POST", self.model_response_url, body=payload)
        elapsed_ms = max(0, int((self._clock() - start) * 1000))
        return parse_sanitization_response(
            doc, tokens_screened=tokens, attempts=attempts, duration_ms=elapsed_ms, truncated=truncated
        )

    def get_or_none(self, url: str) -> dict[str, Any] | None:
        """Perform a bounded GET; return ``None`` on HTTP 404 or raise ``ModelArmorHTTPError`` otherwise."""
        try:
            doc, _ = self.request("GET", url)
        except ModelArmorHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(doc, dict):
            raise ModelArmorResponseError("Expected JSON object from Model Armor GET")
        return doc

    def _send_once(self, method: str, url: str, body: dict[str, Any] | None, timeout: float) -> httpx.Response:
        if self._send is not None:
            return self._send(method, url, json=body, headers=self.headers(), timeout=timeout)
        if method == "POST" and self._post is not None:
            return self._post(url, json=body, headers=self.headers(), timeout=timeout)
        return httpx.request(method, url, json=body, headers=self.headers(), timeout=timeout)

    def _http_error(self, response: httpx.Response) -> ModelArmorHTTPError:
        redacted = redact_model_armor_text(
            response.text[:_MAX_ERROR_BODY_CHARS], self.project_id, self.template_id
        )
        return ModelArmorHTTPError(response.status_code, f"Model Armor HTTP {response.status_code}: {redacted}")

    def request(self, method: str, url: str, *, body: dict[str, Any] | None = None) -> tuple[Any, int]:
        """Execute one REST request with bounded retries on transient 429/5xx/timeouts."""
        deadline = self._clock() + self._budget
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._clock()
            if remaining < _MIN_ATTEMPT_SECONDS:
                break
            try:
                response = self._send_once(method, url, body, min(self._attempt_timeout, remaining))
            except httpx.TimeoutException as exc:
                last_error = exc
            else:
                if 200 <= response.status_code < 300:
                    return response.json(), attempt
                last_error = self._http_error(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        if last_error is None:
            raise TimeoutError(f"Model Armor budget ({self._budget:.0f} s) exhausted before first attempt")
        raise last_error


def build_model_armor_client(settings: ArenaSettings) -> ModelArmorClient:
    """Construct a live ``ModelArmorClient`` from ``ArenaSettings`` (fails loud when unconfigured)."""
    from app.core.gcp_clients import get_access_token

    loc, short_tpl = ensure_model_armor_configured(settings)
    budget = float(min(30, max(5, settings.query_timeout_seconds // 2)))
    return ModelArmorClient(
        project_id=settings.gcp_project_id,
        location=loc,
        template_id=short_tpl,
        token_provider=lambda: get_access_token(settings),
        attempt_timeout_seconds=min(10.0, budget),
        budget_seconds=budget,
    )


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "FilterVerdict",
    "MAX_ATTEMPTS",
    "MAX_SANITIZE_TEXT_CHARS",
    "MODEL_ARMOR_FREE_TIER_TOKENS",
    "MODEL_ARMOR_PRICE_AS_OF",
    "MODEL_ARMOR_PRICING_SOURCE",
    "MODEL_ARMOR_SERVICE_LABEL",
    "MODEL_ARMOR_TEMPLATE_MONTHLY_USD",
    "MODEL_ARMOR_USD_PER_1M_TOKENS",
    "ModelArmorClient",
    "ModelArmorConfigurationError",
    "ModelArmorHTTPError",
    "ModelArmorResponseError",
    "RETRYABLE_STATUS",
    "SanitizationOutcome",
    "build_model_armor_client",
    "ensure_model_armor_configured",
    "extract_short_template_id",
    "model_armor_cost_usd",
    "model_armor_host",
    "model_armor_pricing_summary",
    "model_armor_tokens",
    "parse_sanitization_response",
    "redact_model_armor_text",
    "sanitize_model_response_url",
    "sanitize_user_prompt_url",
    "template_resource_url",
    "templates_collection_url",
    "validate_location",
]
