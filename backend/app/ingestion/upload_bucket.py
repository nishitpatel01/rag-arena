"""Private upload bucket for user documents (v1.1): ensure it exists, create it if allowed, verify it is private.

Owner decisions (2026-10-26): the app ATTEMPTS to create the bucket by default; if the identity lacks permission or an
organization policy blocks it, the caller gets a typed outcome with a remedy — never a silent fallback. Uploads are kept
until manually deleted, so no lifecycle rule is set.

Privacy rule: public access prevention ``enforced`` + uniform bucket-level access ``on`` are required. The bucket IAM
policy is also checked for ``allUsers`` / ``allAuthenticatedUsers`` when the identity may read it; when it may not
(the Cloud Run service account only holds object + bucket-read roles) that is noted, because enforced public access
prevention already blocks every public grant.

Messages and remedies never contain the project id, bucket name or account emails (placeholders only); callers show
the bucket name separately and mask it in safe mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import enum
import json
import re
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

STORAGE_HOST = "https://storage.googleapis.com"
BUCKET_SUFFIX = "-rag-arena-uploads"
MODES: frozenset[str] = frozenset({"auto", "existing"})
MAX_ATTEMPTS = 3
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
PUBLIC_MEMBERS: tuple[str, ...] = ("allUsers", "allAuthenticatedUsers")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")
_CONSTRAINT_RE = re.compile(r"constraints/[A-Za-z0-9_.]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
_PROJECT_NUM_RE = re.compile(r"projects/\d+")
_MAX_DETAIL_CHARS = 300

_CREATE_CMD = ("gcloud storage buckets create gs://${UPLOAD_BUCKET} --project=${GCP_PROJECT_ID} "
               "--location=${GCP_LOCATION} --uniform-bucket-level-access --public-access-prevention")


class BucketState(str, enum.Enum):
    OK = "ok"
    CREATED = "created"
    PERMISSION_DENIED = "permission_denied"
    ORG_POLICY = "org_policy"
    NAME_CONFLICT = "name_conflict"
    NOT_FOUND = "not_found"
    UNSAFE_BUCKET = "unsafe_bucket"
    UNREACHABLE = "unreachable"
    ERROR = "error"


@dataclass(frozen=True)
class BucketStatus:
    state: BucketState
    bucket: str
    message: str
    remedy: str = ""
    problems: tuple[str, ...] = ()
    created: bool = False

    @property
    def usable(self) -> bool:
        return self.state in (BucketState.OK, BucketState.CREATED)

    def to_dict(self, *, safe: bool) -> dict[str, Any]:
        return {
            "state": self.state.value, "usable": self.usable, "created": self.created,
            "bucket": "${UPLOAD_BUCKET}" if safe else self.bucket,
            "message": self.message, "remedy": self.remedy, "problems": list(self.problems),
        }


class _HttpFailure(Exception):
    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.text = text


def validate_bucket_name(name: str) -> str:
    """Cloud Storage naming rules (subset we accept); raises ``ValueError`` naming the rule broken."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("bucket name must be 3-63 chars of a-z, 0-9, '-', '_', '.', starting/ending alphanumeric")
    if name.startswith("goog") or ".." in name or "google" in name:
        raise ValueError("bucket name may not start with 'goog', contain 'google' or contain '..'")
    return name


def default_bucket_name(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required to derive the default upload bucket name")
    return validate_bucket_name(f"{project_id.strip()}{BUCKET_SUFFIX}")


def create_body(name: str, location: str, *, kms_key: str) -> dict[str, Any]:
    """Bucket resource for ``buckets.insert``: private by construction, no lifecycle rule (uploads are kept)."""
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location is required")
    body: dict[str, Any] = {
        "name": validate_bucket_name(name),
        "location": location.strip().upper(),
        "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"},
    }
    if kms_key:
        body["encryption"] = {"defaultKmsKeyName": kms_key}
    return body


def privacy_problems(meta: dict[str, Any], iam_policy: dict[str, Any] | None) -> tuple[str, ...]:
    """Every reason the bucket is not private enough (empty tuple = private). ``iam_policy`` None = not readable."""
    iam_cfg = (meta or {}).get("iamConfiguration") or {}
    problems: list[str] = []
    if iam_cfg.get("publicAccessPrevention") != "enforced":
        problems.append("public access prevention is not enforced")
    if not (iam_cfg.get("uniformBucketLevelAccess") or {}).get("enabled"):
        problems.append("uniform bucket-level access is off (object ACLs could make files public)")
    for binding in (iam_policy or {}).get("bindings", []) or []:
        for member in binding.get("members", []) or []:
            if member in PUBLIC_MEMBERS:
                problems.append(f"IAM grants {binding.get('role', '?')} to {member}")
    return tuple(problems)


def _error_message(text: str) -> str:
    try:
        return str((json.loads(text).get("error") or {}).get("message") or "")
    except (ValueError, AttributeError):
        return text or ""


def classify_create_error(status_code: int, text: str) -> BucketState:
    message = _error_message(text).lower()
    if "constraints/" in message or "organization policy" in message or "org policy" in message:
        return BucketState.ORG_POLICY
    if status_code == 403:
        return BucketState.PERMISSION_DENIED
    if status_code == 409:
        return BucketState.NAME_CONFLICT
    if status_code in RETRYABLE_STATUS or status_code == 0:
        return BucketState.UNREACHABLE
    return BucketState.ERROR


class UploadBucketManager:
    """ensure() = read → (create if allowed) → verify privacy, for ONE bucket (bounded retries and timeouts)."""

    def __init__(
        self, *, project_id: str, bucket: str, location: str, mode: str, kms_key: str,
        token_provider: Callable[[], str], timeout_seconds: float,
        send: Callable[..., httpx.Response] | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        if mode not in MODES:
            raise ValueError(f"UPLOAD_BUCKET_MODE must be one of {sorted(MODES)}, got {mode!r}")
        if not callable(token_provider) or timeout_seconds <= 0:
            raise ValueError("token_provider must be callable and timeout_seconds > 0")
        self.project_id = project_id.strip()
        self.bucket = validate_bucket_name(bucket)
        self.location = location
        self.mode = mode
        self.kms_key = kms_key or ""
        self._token = token_provider
        self._timeout = float(timeout_seconds)
        self._send = send or httpx.request
        self._sleep = sleep

    # ---------- transport ----------
    def _redact(self, text: str) -> str:
        out = text.replace(self.bucket, "${UPLOAD_BUCKET}").replace(self.project_id, "${GCP_PROJECT_ID}")
        out = _PROJECT_NUM_RE.sub("projects/${PROJECT_NUMBER}", _EMAIL_RE.sub("${ACCOUNT}", out))
        return out[:_MAX_DETAIL_CHARS]

    def _bearer(self) -> str:
        try:
            return self._token()
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed ERROR status, never swallowed
            raise _HttpFailure(-1, self._redact(f"{type(exc).__name__}: {exc}")) from exc

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        last: _HttpFailure | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._send(method, url, headers=headers, timeout=httpx.Timeout(self._timeout), **kwargs)
            except httpx.TimeoutException:
                last = _HttpFailure(0, "request timed out")
            else:
                if resp.status_code == 200:
                    return resp.json() if resp.text else {}
                last = _HttpFailure(resp.status_code, resp.text)
                if resp.status_code not in RETRYABLE_STATUS:
                    raise last
            if attempt < MAX_ATTEMPTS:
                self._sleep(1.0 * (2 ** (attempt - 1)))
        raise last if last else _HttpFailure(0, "no attempt made")

    def _bucket_url(self, suffix: str = "") -> str:
        return f"{STORAGE_HOST}/storage/v1/b/{quote(self.bucket, safe='')}{suffix}"

    def _status(self, state: BucketState, message: str, remedy: str = "", **kw: Any) -> BucketStatus:
        return BucketStatus(state=state, bucket=self.bucket, message=message, remedy=remedy, **kw)

    def _failure_status(self, exc: _HttpFailure, action: str) -> BucketStatus:
        if exc.status_code == -1:
            return self._status(BucketState.ERROR, f"Cloud Storage credentials unavailable: {exc.text}",
                                "Locally run `gcloud auth application-default login`; on Cloud Run check the service account.")
        if exc.status_code == 0:
            return self._status(BucketState.UNREACHABLE, f"Cloud Storage {action} {exc.text} after {MAX_ATTEMPTS} attempts.",
                                "Check network access to storage.googleapis.com and retry.")
        if exc.status_code in RETRYABLE_STATUS:
            return self._status(BucketState.UNREACHABLE,
                                f"Cloud Storage {action} failed with HTTP {exc.status_code} after {MAX_ATTEMPTS} attempts.",
                                "Retry later; see Cloud Storage status if it persists.")
        detail = self._redact(_error_message(exc.text))
        return self._status(BucketState.ERROR, f"Cloud Storage {action} failed with HTTP {exc.status_code}: {detail}")

    # ---------- steps ----------
    def _read_meta(self) -> dict[str, Any]:
        return self._call("GET", self._bucket_url(), params={"fields": "name,location,iamConfiguration,encryption"})

    def _create(self) -> BucketStatus | None:
        body = create_body(self.bucket, self.location, kms_key=self.kms_key)
        try:
            self._call("POST", f"{STORAGE_HOST}/storage/v1/b", params={"project": self.project_id}, json=body)
        except _HttpFailure as exc:
            return self._create_failure(exc)
        return None

    def _create_failure(self, exc: _HttpFailure) -> BucketStatus:
        state = classify_create_error(exc.status_code, exc.text)
        if state is BucketState.PERMISSION_DENIED:
            return self._status(
                state, "Creating the upload bucket was denied: this identity cannot create buckets in the project.",
                "Needs permission storage.buckets.create on the project (for example role roles/storage.admin). "
                f"Ask a project admin to grant it, or to create the bucket once: {_CREATE_CMD}")
        if state is BucketState.ORG_POLICY:
            found = _CONSTRAINT_RE.findall(_error_message(exc.text))
            names = ", ".join(sorted(set(found))) or "an organization policy"
            return self._status(
                state, f"Creating the upload bucket was blocked by organization policy: {names}.",
                "Ask your organization admin to allow it for this project, or set UPLOAD_BUCKET to an approved "
                "existing private bucket and UPLOAD_BUCKET_MODE=existing.")
        if state is BucketState.NAME_CONFLICT:
            return self._status(
                state, "The upload bucket name is already taken by another project (bucket names are global).",
                "Set UPLOAD_BUCKET to a different, unique name and retry.")
        return self._failure_status(exc, "bucket create")

    def _verify(self, meta: dict[str, Any], *, created: bool) -> BucketStatus:
        note = ""
        try:
            policy: dict[str, Any] | None = self._call("GET", self._bucket_url("/iam"))
        except _HttpFailure as exc:
            if exc.status_code != 403:
                return self._failure_status(exc, "bucket IAM read")
            policy = None
            note = (" IAM policy not inspected (no storage.buckets.getIamPolicy); enforced public access "
                    "prevention blocks public grants.")
        problems = privacy_problems(meta, policy)
        if problems:
            return self._status(
                BucketState.UNSAFE_BUCKET, "The upload bucket is not private; uploads are disabled." + note,
                "Fix the listed problems (or choose another bucket); the app never changes an existing bucket.",
                problems=problems, created=created)
        state = BucketState.CREATED if created else BucketState.OK
        verb = "Created private upload bucket" if created else "Upload bucket is private"
        return self._status(state, f"{verb}: public access prevention enforced, uniform access on.{note}", created=created)

    def _read_or_status(self) -> dict[str, Any] | BucketStatus | None:
        """Bucket metadata, ``None`` when missing (404), or a failure status."""
        try:
            return self._read_meta()
        except _HttpFailure as exc:
            if exc.status_code == 404:
                return None
            if exc.status_code == 403:
                return self._status(
                    BucketState.PERMISSION_DENIED, "This identity cannot read the upload bucket.",
                    "Needs storage.buckets.get on the bucket (for example roles/storage.legacyBucketReader) plus "
                    "roles/storage.objectUser; re-run scripts/deploy_cloudrun.sh or ask an admin to grant them.")
            return self._failure_status(exc, "bucket read")

    def check(self) -> BucketStatus:
        """Read-only status (never creates): what the Ingest page shows before anything is uploaded."""
        found = self._read_or_status()
        if isinstance(found, BucketStatus):
            return found
        if found is not None:
            return self._verify(found, created=False)
        return self._missing_status()

    def _missing_status(self) -> BucketStatus:
        if self.mode == "existing":
            return self._status(BucketState.NOT_FOUND, "UPLOAD_BUCKET does not exist and UPLOAD_BUCKET_MODE=existing.",
                                "Create the bucket, fix UPLOAD_BUCKET, or set UPLOAD_BUCKET_MODE=auto.")
        return self._status(BucketState.NOT_FOUND, "The private upload bucket does not exist yet.",
                            "It is created automatically on first upload (or press Create bucket).")

    def ensure(self) -> BucketStatus:
        """check() + create when missing and UPLOAD_BUCKET_MODE=auto (typed failure, never a silent fallback)."""
        found = self._read_or_status()
        if isinstance(found, BucketStatus):
            return found
        if found is not None:
            return self._verify(found, created=False)
        if self.mode == "existing":
            return self._missing_status()
        failure = self._create()
        if failure is not None:
            return failure
        try:
            return self._verify(self._read_meta(), created=True)
        except _HttpFailure as exc:
            return self._failure_status(exc, "bucket read after create")


__all__ = [
    "BUCKET_SUFFIX",
    "BucketState",
    "BucketStatus",
    "UploadBucketManager",
    "classify_create_error",
    "create_body",
    "default_bucket_name",
    "privacy_problems",
    "validate_bucket_name",
]
