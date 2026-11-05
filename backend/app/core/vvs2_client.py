"""Vertex AI Vector Search 2.0 (``vectorsearch.googleapis.com/v1beta``) REST client and schema builders.

Provides bounded, fail-loud HTTP calls with Google ADC bearer tokens, project/identity redaction on errors,
deterministic RFC1035 ``dataObjectId`` derivation, and CRC32 sublinear-TF sparse token embeddings for hybrid
dense ScaNN + sparse Reciprocal Rank Fusion (``dataObjects:batchSearch``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
import sys
import time
from typing import Any, Callable
import urllib.parse
import zlib

import httpx

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token
from app.recording.redaction import redact_text, redaction_tokens

VVS2_HOST = "https://vectorsearch.googleapis.com"
VVS2_API_VERSION = "v1beta"
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
MAX_SPARSE_DIMENSIONS = 128
_MAX_ERROR_BODY_CHARS = 500
_LOCATION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "under",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
    }
)

TransportFn = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class Vvs2HttpError(RuntimeError):
    """Non-2xx response from ``vectorsearch.googleapis.com/v1beta`` (after bounded retries on transient statuses)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Vvs2Resources:
    """Validated Vector Search 2.0 resource paths for a project, regional location, collection, and index."""

    project_id: str
    location: str
    collection_id: str
    index_id: str

    @classmethod
    def from_settings(cls, settings: ArenaSettings) -> "Vvs2Resources":
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        project = settings.gcp_project_id.strip()
        location = settings.vvs2_location.strip()
        collection = settings.vvs2_collection.strip()
        index_id = settings.vvs2_index_id.strip()
        if not project:
            raise ValueError("GCP_PROJECT_ID is required for Vector Search 2.0")
        if not _LOCATION_RE.fullmatch(location):
            raise ValueError(
                f"VVS2_LOCATION must be a regional location (e.g. 'us-central1', never 'global'), got {location!r}"
            )
        if not collection or not _ID_RE.fullmatch(collection):
            raise ValueError(
                "VVS2_COLLECTION is not configured or invalid — set VVS2_COLLECTION (matching [a-z][a-z0-9-]{0,62}) "
                "via scripts/ephemeral_vvs2.sh --up or use DEMO_MODE=replay"
            )
        if not index_id or not _ID_RE.fullmatch(index_id):
            raise ValueError(f"VVS2_INDEX_ID must match [a-z][a-z0-9-]{{0,62}}, got {index_id!r}")
        return cls(project_id=project, location=location, collection_id=collection, index_id=index_id)

    @property
    def parent(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}"

    @property
    def collection_name(self) -> str:
        return f"{self.parent}/collections/{self.collection_id}"

    @property
    def index_name(self) -> str:
        return f"{self.collection_name}/indexes/{self.index_id}"

    @property
    def redacted_collection_name(self) -> str:
        return f"projects/${{GCP_PROJECT_ID}}/locations/{self.location}/collections/{self.collection_id}"

    @property
    def redacted_index_name(self) -> str:
        return f"{self.redacted_collection_name}/indexes/{self.index_id}"


def data_object_id(chunk_id: str) -> str:
    """Derive a deterministic RFC1035-compliant ``dataObjectId`` (``chk-<24hex>``) from an arbitrary ``chunk_id``."""
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ValueError("chunk_id must be a non-empty string")
    digest = hashlib.sha256(chunk_id.strip().encode("utf-8")).hexdigest()[:24]
    return f"chk-{digest}"


def build_sparse_token_embedding(text: str, *, max_dimensions: int = MAX_SPARSE_DIMENSIONS) -> dict[str, list[Any]]:
    """Build deterministic positive-int32 sparse token weights (compound + split sub-tokens, sublinear TF)."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_dimensions < 1 or max_dimensions > 2048:
        raise ValueError("max_dimensions must be in [1, 2048]")
    counts: Counter[str] = Counter()
    for match in _TOKEN_RE.finditer(text.lower()):
        tok = match.group(0)
        if tok not in _STOPWORDS and len(tok) >= 2:
            counts[tok] += 1
        if "-" in tok:
            for part in tok.split("-"):
                if part and part not in _STOPWORDS and len(part) >= 2:
                    counts[part] += 1
    if not counts:
        return {"indices": [], "values": []}
    top_terms = counts.most_common(max_dimensions)
    merged: dict[int, float] = {}
    for term, tf in top_terms:
        idx = int(zlib.crc32(term.encode("utf-8")) & 0x7FFFFFFF)
        weight = 1.0 + math.log(float(tf))
        merged[idx] = merged.get(idx, 0.0) + weight
    ordered = sorted(merged.items(), key=lambda kv: kv[0])
    return {
        "indices": [idx for idx, _ in ordered],
        "values": [round(val, 6) for _, val in ordered],
    }


def collection_body(*, display_name: str, vector_dim: int = 3072) -> dict[str, Any]:
    """Build ``GoogleCloudVectorsearchV1betaCollection`` JSON payload (Draft-07 ``dataSchema`` + ``vectorSchema``)."""
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("display_name must be non-empty")
    if not isinstance(vector_dim, int) or vector_dim < 1 or vector_dim > 8192:
        raise ValueError("vector_dim must be an integer in [1, 8192]")
    return {
        "displayName": display_name.strip(),
        "dataSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "doc_name": {"type": "string"},
                "doc_type": {"type": "string"},
                "solicitation_id": {"type": "string"},
                "section_id": {"type": "string"},
                "content": {"type": "string"},
                "element_type": {"type": "string"},
                "effective_date": {"type": "string"},
                "amends_section": {"type": "string"},
                "page_number": {"type": "integer"},
                "amendment_number": {"type": "integer"},
                "bbox_json": {"type": "string"},
            },
        },
        "vectorSchema": {
            "dense_embedding": {"denseVector": {"dimensions": vector_dim}},
            "sparse_embedding": {"sparseVector": {}},
        },
    }


def dense_scann_index_body(*, display_name: str) -> dict[str, Any]:
    """Build ``GoogleCloudVectorsearchV1betaIndex`` payload for a COSINE ScaNN ANN index on ``dense_embedding``."""
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("display_name must be non-empty")
    return {
        "displayName": display_name.strip(),
        "indexField": "dense_embedding",
        "distanceMetric": "COSINE_DISTANCE",
        "denseScann": {"featureNormType": "UNIT_L2_NORM"},
        "filterFields": ["solicitation_id"],
        "dedicatedInfrastructure": {
            "mode": "PERFORMANCE_OPTIMIZED",
            "autoscalingSpec": {"minReplicaCount": 1, "maxReplicaCount": 2},
        },
    }


def data_object_request(
    *,
    collection_name: str,
    chunk_row: dict[str, Any],
    dense_embedding: list[float],
) -> dict[str, Any]:
    """Build one ``CreateDataObjectRequest`` entry for ``dataObjects:batchCreate``."""
    if not collection_name or not dense_embedding:
        raise ValueError("collection_name and non-empty dense_embedding are required")
    cid = str(chunk_row.get("chunk_id") or "").strip()
    content = str(chunk_row.get("content") or "").strip()
    if not cid or not content:
        raise ValueError("chunk_row requires non-empty 'chunk_id' and 'content'")
    sparse_text = " ".join(
        p for p in (str(chunk_row.get("section_id") or ""), str(chunk_row.get("doc_name") or ""), content) if p
    )
    sparse = build_sparse_token_embedding(sparse_text)
    bbox = chunk_row.get("bbox")
    bbox_json = json.dumps(bbox, separators=(",", ":")) if isinstance(bbox, dict) else str(chunk_row.get("bbox_json") or "")
    data: dict[str, Any] = {
        "chunk_id": cid,
        "doc_name": str(chunk_row.get("doc_name") or ""),
        "doc_type": str(chunk_row.get("doc_type") or "SOLICITATION"),
        "solicitation_id": str(chunk_row.get("solicitation_id") or ""),
        "section_id": str(chunk_row.get("section_id") or ""),
        "content": content,
        "element_type": str(chunk_row.get("element_type") or "narrative_text"),
        "effective_date": str(chunk_row.get("effective_date") or ""),
        "amends_section": str(chunk_row.get("amends_section") or ""),
        "page_number": int(chunk_row.get("page_number") or 1),
        "amendment_number": int(chunk_row.get("amendment_number") or 0),
        "bbox_json": bbox_json,
    }
    vectors: dict[str, Any] = {
        "dense_embedding": {"dense": {"values": [float(v) for v in dense_embedding]}},
    }
    if sparse["indices"]:
        vectors["sparse_embedding"] = {"sparse": sparse}
    return {
        "parent": collection_name,
        "dataObjectId": data_object_id(cid),
        "dataObject": {"data": data, "vectors": vectors},
    }


def batch_search_body(
    *,
    query_vector: list[float],
    query_text: str,
    top_k: int,
    solicitation_filter: str | None = None,
    index_name: str | None = None,
) -> dict[str, Any]:
    """Build ``BatchSearchDataObjectsRequest`` fusing dense ScaNN + sparse token search via server-side RRF."""
    if not query_vector:
        raise ValueError("query_vector must be non-empty")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer in [1, 100]")
    flt = {"solicitation_id": {"$eq": solicitation_filter.strip()}} if solicitation_filter and solicitation_filter.strip() else None

    dense_leg: dict[str, Any] = {
        "searchField": "dense_embedding",
        "vector": {"values": [float(v) for v in query_vector]},
        "distanceMetric": "COSINE_DISTANCE",
        "topK": top_k,
        "outputFields": {"dataFields": ["*"]},
    }
    if flt:
        dense_leg["filter"] = flt
    if index_name:
        dense_leg["searchHint"] = {"indexHint": {"name": index_name}}

    searches: list[dict[str, Any]] = [{"vectorSearch": dense_leg}]
    sparse = build_sparse_token_embedding(query_text)
    if sparse["indices"]:
        sparse_leg: dict[str, Any] = {
            "searchField": "sparse_embedding",
            "sparseVector": sparse,
            "topK": top_k,
            "outputFields": {"dataFields": ["*"]},
            "searchHint": {"knnHint": {}},
        }
        if flt:
            sparse_leg["filter"] = flt
        searches.append({"vectorSearch": sparse_leg})

    body: dict[str, Any] = {
        "searches": searches,
        "metadataOptions": {"searchSignalsEnabled": True},
    }
    if len(searches) > 1:
        body["combine"] = {
            "ranker": {"rrf": {"weights": [1.0] * len(searches)}},
            "topK": top_k,
            "outputFields": {"dataFields": ["*"]},
        }
    return body


def _default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    resp = httpx.request(method, url, headers=headers, content=body, timeout=timeout)
    return resp.status_code, resp.content


class Vvs2Client:
    """Bounded-retry REST v1beta client for ``vectorsearch.googleapis.com`` with error redaction."""

    def __init__(
        self,
        settings: ArenaSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        transport: TransportFn | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        self.settings = settings
        self._token_provider = token_provider or (lambda: get_access_token(settings))
        self._transport = transport or _default_transport
        self._sleep = sleep_fn
        self._tokens = redaction_tokens(settings)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
        }
        if self.settings.gcp_project_id.strip():
            headers["x-goog-user-project"] = self.settings.gcp_project_id.strip()
        return headers

    def _redact(self, message: str) -> str:
        return redact_text(message, self._tokens)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Issue one v1beta REST call with bounded retries on 429/5xx/timeouts; return ``(json_dict, attempts)``."""
        clean_path = path.lstrip("/")
        if not clean_path.startswith(f"{VVS2_API_VERSION}/"):
            clean_path = f"{VVS2_API_VERSION}/{clean_path}"
        url = f"{VVS2_HOST}/{clean_path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        payload = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        timeout = float(timeout_seconds or self.settings.vvs2_timeout_seconds)

        last_err: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                status, raw = self._transport(method.upper(), url, self._headers(), payload, timeout)
            except httpx.TimeoutException as exc:
                last_err = TimeoutError(self._redact(f"Vector Search 2.0 timeout on {method} {clean_path}: {exc}"))
            else:
                if 200 <= status < 300:
                    doc = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(doc, dict):
                        raise ValueError("Vector Search 2.0 returned a non-object JSON payload")
                    return doc, attempt
                snippet = self._redact(raw.decode("utf-8", errors="replace")[:_MAX_ERROR_BODY_CHARS])
                safe_path = self._redact(clean_path)
                last_err = Vvs2HttpError(status, f"Vector Search 2.0 HTTP {status} on {method} {safe_path}: {snippet}")
                if status not in RETRYABLE_STATUS:
                    raise last_err
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        assert last_err is not None
        raise last_err

    def wait_operation(
        self,
        operation_name: str,
        *,
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Poll a ``GoogleLongrunningOperation`` until ``done`` with an explicit wall-clock deadline."""
        if not isinstance(operation_name, str) or not operation_name.strip():
            raise ValueError("operation_name must be non-empty")
        budget = max(5.0, float(timeout_seconds))
        started = time.monotonic()
        deadline = started + budget
        next_log = started + 60.0
        while time.monotonic() < deadline:
            op, _ = self.request("GET", operation_name.strip())
            if op.get("done") is True:
                if "error" in op and op["error"]:
                    raise RuntimeError(self._redact(f"Vector Search 2.0 LRO failed: {json.dumps(op['error'])[:400]}"))
                return op
            now = time.monotonic()
            if now >= next_log:
                print(
                    f"[vvs2] LRO still running ({int(now - started)}s / {int(budget)}s)...",
                    file=sys.stderr,
                    flush=True,
                )
                next_log = now + 60.0
            self._sleep(poll_interval_seconds)
        raise TimeoutError(self._redact(f"Vector Search 2.0 LRO timed out after {timeout_seconds:.0f}s: {operation_name}"))


__all__ = [
    "MAX_SPARSE_DIMENSIONS",
    "VVS2_API_VERSION",
    "VVS2_HOST",
    "Vvs2Client",
    "Vvs2HttpError",
    "Vvs2Resources",
    "batch_search_body",
    "build_sparse_token_embedding",
    "collection_body",
    "data_object_id",
    "data_object_request",
    "dense_scann_index_body",
]
