"""Document AI REST client (google-auth bearer + httpx; no documentai SDK) for the Level 3 Layout Parser.

Endpoints (cloud.google.com/document-ai/docs/reference/rest, read 2026-10-22):

* ``GET  https://{loc}-documentai.googleapis.com/v1/projects/{p}/locations/{loc}/processors`` (paged list)
* ``POST …/processors`` body ``{"type": "LAYOUT_PARSER_PROCESSOR", "displayName": …}`` (create)
* ``POST …/processors/{id}:process`` (or ``…/processorVersions/{v}:process``) body
  ``{"rawDocument": {"content": <base64>, "mimeType": "application/pdf"}, "processOptions": {"layoutConfig": …}}``

The client never PATCHes, deletes, enables or disables a processor. Retries are bounded (≤ ``MAX_ATTEMPTS``) and
only on 429/500/503/504 or transport timeouts; any other status raises ``DocumentAIHTTPError`` with the response
body truncated and identity values redacted (project id → ``${GCP_PROJECT_ID}``, ``projects/<digits>`` →
``projects/${GCP_PROJECT_NUMBER}``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re
import time
from typing import Any, Callable

import httpx

RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 10
MAX_PDF_BYTES = 20 * 1024 * 1024  # documented online request limit (20 MB)
_MAX_ERROR_BODY_CHARS = 600
_LOCATION_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_PROCESSOR_ID_RE = re.compile(r"^[a-z0-9]{1,64}$")
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PROJECT_NUMBER_RE = re.compile(r"projects/\d{3,}")

Poster = Callable[..., httpx.Response]
Getter = Callable[..., httpx.Response]


class DocumentAIHTTPError(RuntimeError):
    """Non-2xx Document AI answer (after bounded retries when the status is retryable)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProcessorInfo:
    """A listed processor reduced to non-identifying fields (short id only; never the project number)."""

    processor_id: str
    display_name: str
    type: str
    state: str
    default_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "processor_id": self.processor_id, "display_name": self.display_name, "type": self.type,
            "state": self.state, "default_version": self.default_version,
        }


def validate_location(location: str) -> str:
    if not isinstance(location, str) or not _LOCATION_RE.match(location.strip()):
        raise ValueError(f"Invalid Document AI location {location!r} (expected e.g. 'us' or 'eu')")
    return location.strip()


def validate_processor_id(processor_id: str) -> str:
    if not isinstance(processor_id, str) or not _PROCESSOR_ID_RE.match(processor_id.strip()):
        raise ValueError("DOCAI_PROCESSOR_ID must be the short processor id (lowercase letters/digits, ≤ 64 chars)")
    return processor_id.strip()


def short_id(resource_name: str) -> str:
    """Last path segment of ``projects/…/locations/…/processors/<id>`` (or ``…/processorVersions/<v>``)."""
    if not isinstance(resource_name, str) or "/" not in resource_name:
        raise ValueError("resource name must be a slash-separated Document AI resource path")
    return resource_name.rstrip("/").rsplit("/", 1)[-1]


def processor_info(raw: dict[str, Any]) -> ProcessorInfo:
    """Map one ``Processor`` JSON object (fields per REST reference) to ``ProcessorInfo``."""
    if not isinstance(raw, dict) or not raw.get("name"):
        raise ValueError("processor entry must be an object with a 'name'")
    version = str(raw.get("defaultProcessorVersion", "") or "")
    return ProcessorInfo(
        processor_id=short_id(str(raw["name"])),
        display_name=str(raw.get("displayName", "") or ""),
        type=str(raw.get("type", "") or ""),
        state=str(raw.get("state", "") or ""),
        default_version=short_id(version) if "/" in version else version,
    )


class DocumentAIClient:
    """Bounded REST client for one project + Document AI location (ADC bearer token per request)."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        token_provider: Callable[[], str],
        timeout_seconds: float = 120.0,
        post: Poster | None = None,
        get: Getter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        if not isinstance(timeout_seconds, (int, float)) or not 1.0 <= float(timeout_seconds) <= 600.0:
            raise ValueError("timeout_seconds must be within [1, 600]")
        self.project_id = project_id.strip()
        self.location = validate_location(location)
        self._token_provider = token_provider
        self._timeout = float(timeout_seconds)
        self._post = post or httpx.post
        self._get = get or httpx.get
        self._sleep = sleep

    @property
    def base_url(self) -> str:
        return f"https://{self.location}-documentai.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}"

    def redact(self, text: str) -> str:
        """Strip the project id and project numbers from any text that may reach logs, payloads or errors."""
        return _PROJECT_NUMBER_RE.sub("projects/${GCP_PROJECT_NUMBER}", str(text).replace(self.project_id, "${GCP_PROJECT_ID}"))

    def _headers(self) -> dict[str, str]:
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise RuntimeError("ADC token provider returned an empty access token")
        return {"Authorization": f"Bearer {token}", "x-goog-user-project": self.project_id, "Content-Type": "application/json"}

    def _send(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
        """One logical call with ≤ MAX_ATTEMPTS attempts; returns (json, attempts)."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if method == "GET":
                    resp = self._get(url, headers=self._headers(), timeout=self._timeout)
                else:
                    resp = self._post(url, headers=self._headers(), json=body, timeout=self._timeout)
            except httpx.TimeoutException as exc:
                last_error = f"timeout after {self._timeout:g}s ({type(exc).__name__})"
            else:
                if 200 <= resp.status_code < 300:
                    return resp.json(), attempt
                message = self.redact(resp.text[:_MAX_ERROR_BODY_CHARS])
                if resp.status_code not in RETRYABLE_STATUS:
                    raise DocumentAIHTTPError(resp.status_code, f"Document AI {method} returned HTTP {resp.status_code}: {message}")
                last_error = f"HTTP {resp.status_code}: {message}"
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        raise DocumentAIHTTPError(503, f"Document AI {method} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def list_processors(self) -> list[ProcessorInfo]:
        """All processors in the location (≤ MAX_LIST_PAGES pages of LIST_PAGE_SIZE); read-only."""
        out: list[ProcessorInfo] = []
        token = ""
        for _ in range(MAX_LIST_PAGES):
            url = f"{self.base_url}/processors?pageSize={LIST_PAGE_SIZE}" + (f"&pageToken={token}" if token else "")
            doc, _attempts = self._send("GET", url)
            out.extend(processor_info(p) for p in doc.get("processors", []) or [])
            token = str(doc.get("nextPageToken", "") or "")
            if not token:
                return out
        raise RuntimeError(f"processor listing exceeded {MAX_LIST_PAGES} pages; refusing to guess")

    def create_processor(self, *, display_name: str, processor_type: str) -> ProcessorInfo:
        """``processors.create`` — the only mutating call this client can make."""
        if not display_name or not processor_type:
            raise ValueError("display_name and processor_type must be non-empty")
        doc, _attempts = self._send("POST", f"{self.base_url}/processors", {"type": processor_type, "displayName": display_name})
        return processor_info(doc)

    def process_url(self, processor_id: str, version: str = "") -> str:
        pid = validate_processor_id(processor_id)
        if version:
            if not _VERSION_RE.match(version):
                raise ValueError(f"invalid processor version {version!r}")
            return f"{self.base_url}/processors/{pid}/processorVersions/{version}:process"
        return f"{self.base_url}/processors/{pid}:process"

    def process_pdf(
        self, processor_id: str, pdf_bytes: bytes, process_options: dict[str, Any], version: str = ""
    ) -> tuple[dict[str, Any], int]:
        """Online ``:process`` of raw PDF bytes; returns (``document`` JSON, attempts)."""
        if not isinstance(pdf_bytes, (bytes, bytearray)) or not bytes(pdf_bytes).startswith(b"%PDF-"):
            raise ValueError("pdf_bytes must be PDF bytes (starting with %PDF-)")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise ValueError(f"PDF of {len(pdf_bytes)} bytes exceeds the 20 MB online limit")
        body = {
            "rawDocument": {"content": base64.b64encode(bytes(pdf_bytes)).decode("ascii"), "mimeType": "application/pdf"},
            "processOptions": process_options,
        }
        doc, attempts = self._send("POST", self.process_url(processor_id, version), body)
        document = doc.get("document")
        if not isinstance(document, dict):
            raise ValueError("Document AI :process response carries no 'document' object")
        return document, attempts


LAYOUT_PARSER_DISPLAY_NAME = "rag-arena-layout-parser"
LAYOUT_PARSER_TYPE = "LAYOUT_PARSER_PROCESSOR"


def find_layout_parser(processors: list[ProcessorInfo]) -> ProcessorInfo | None:
    """Our processor = exact displayName ``rag-arena-layout-parser`` AND type ``LAYOUT_PARSER_PROCESSOR``.

    Every other processor in the project is ignored (never reused, modified or deleted). A processor that carries our
    display name with another type, or more than one match, is an ambiguity that must be resolved by a human.
    """
    named = [p for p in processors if p.display_name == LAYOUT_PARSER_DISPLAY_NAME]
    wrong_type = [p for p in named if p.type != LAYOUT_PARSER_TYPE]
    if wrong_type:
        raise RuntimeError(
            f"a processor named {LAYOUT_PARSER_DISPLAY_NAME!r} exists with type {wrong_type[0].type!r}; refusing to reuse it"
        )
    if len(named) > 1:
        raise RuntimeError(f"{len(named)} processors are named {LAYOUT_PARSER_DISPLAY_NAME!r}; resolve the duplicate manually")
    return named[0] if named else None


def ensure_layout_parser(client: DocumentAIClient) -> tuple[ProcessorInfo, bool]:
    """Idempotent provisioning: return (processor, created). Lists first; creates only when absent."""
    existing = find_layout_parser(client.list_processors())
    if existing is not None:
        return existing, False
    created = client.create_processor(display_name=LAYOUT_PARSER_DISPLAY_NAME, processor_type=LAYOUT_PARSER_TYPE)
    if created.display_name != LAYOUT_PARSER_DISPLAY_NAME or created.type != LAYOUT_PARSER_TYPE:
        raise RuntimeError(f"processors.create returned an unexpected processor ({created.display_name!r}, {created.type!r})")
    return created, True


__all__ = [
    "DocumentAIClient",
    "DocumentAIHTTPError",
    "LAYOUT_PARSER_DISPLAY_NAME",
    "LAYOUT_PARSER_TYPE",
    "MAX_ATTEMPTS",
    "ProcessorInfo",
    "ensure_layout_parser",
    "find_layout_parser",
    "processor_info",
    "short_id",
    "validate_location",
    "validate_processor_id",
]
