"""v1.1 step 1a: private upload bucket — ensure / create / verify with typed outcomes (offline; GCS REST mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.ingestion.upload_bucket import (
    BucketState,
    UploadBucketManager,
    classify_create_error,
    create_body,
    default_bucket_name,
    privacy_problems,
    validate_bucket_name,
)

_PROJECT = "acme-proj"
_BUCKET = "acme-proj-rag-arena-uploads"
_PRIVATE_META = {
    "name": _BUCKET,
    "location": "US-CENTRAL1",
    "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"},
}
_PRIVATE_IAM = {"bindings": [{"role": "roles/storage.objectUser", "members": ["serviceAccount:run@x.iam.gserviceaccount.com"]}]}


class FakeGcs:
    """Scripted Cloud Storage JSON API: (method, path) → list of (status, json body) consumed in order."""

    def __init__(self, script: dict[tuple[str, str], list[tuple[int, Any]]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path
        self.calls.append((method, path, kwargs))
        queue = self.script.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        status, body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        text = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(status, text=text, request=httpx.Request(method, url))


def _err(code: int, message: str, reason: str = "") -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}}


def _manager(fake: FakeGcs, *, mode: str = "auto", kms: str = "") -> UploadBucketManager:
    return UploadBucketManager(
        project_id=_PROJECT, bucket=_BUCKET, location="us-central1", mode=mode, kms_key=kms,
        token_provider=lambda: "tok", timeout_seconds=5, send=fake, sleep=lambda _s: None,
    )


_B = f"/storage/v1/b/{_BUCKET}"


# ---------- pure helpers ----------

def test_default_bucket_name_is_project_scoped_and_valid() -> None:
    assert default_bucket_name("acme-proj") == "acme-proj-rag-arena-uploads"
    long_project = "p" * 30
    name = default_bucket_name(long_project)
    validate_bucket_name(name)
    assert len(name) <= 63


def test_validate_bucket_name_rejects_bad_names() -> None:
    for bad in ["", "UPPER", "a", "has/slash", "gs://x", "x" * 64, "-lead", "goog-thing", "a..b"]:
        with pytest.raises(ValueError):
            validate_bucket_name(bad)


def test_create_body_is_private_without_lifecycle() -> None:
    body = create_body(_BUCKET, "us-central1", kms_key="")
    assert body["name"] == _BUCKET
    assert body["location"] == "US-CENTRAL1"
    assert body["iamConfiguration"] == {
        "uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}
    assert "lifecycle" not in body  # owner decision D2: keep uploads until manually deleted
    assert "encryption" not in body


def test_create_body_with_cmek() -> None:
    key = "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k"
    assert create_body(_BUCKET, "us-central1", kms_key=key)["encryption"] == {"defaultKmsKeyName": key}


def test_privacy_problems_clean_bucket() -> None:
    assert privacy_problems(_PRIVATE_META, _PRIVATE_IAM) == ()


def test_privacy_problems_lists_every_issue() -> None:
    meta = {"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": False}, "publicAccessPrevention": "inherited"}}
    iam = {"bindings": [{"role": "roles/storage.objectViewer", "members": ["allUsers"]},
                        {"role": "roles/storage.legacyBucketReader", "members": ["allAuthenticatedUsers"]}]}
    problems = privacy_problems(meta, iam)
    assert any("public access prevention" in p for p in problems)
    assert any("uniform bucket-level access" in p for p in problems)
    assert any("allUsers" in p for p in problems)
    assert any("allAuthenticatedUsers" in p for p in problems)


def test_privacy_problems_iam_not_inspectable_is_not_a_failure_when_pap_enforced() -> None:
    assert privacy_problems(_PRIVATE_META, None) == ()


_CLASSIFY_CASES = [
    (403, _err(403, "x does not have storage.buckets.create access to the Google Cloud project.", "forbidden"),
     BucketState.PERMISSION_DENIED),
    (412, _err(412, "Request violates constraint 'constraints/gcp.resourceLocations'", "conditionNotMet"),
     BucketState.ORG_POLICY),
    (403, _err(403, "Request violates constraint 'constraints/storage.uniformBucketLevelAccess'", "forbidden"),
     BucketState.ORG_POLICY),
    (409, _err(409, "The requested bucket name is not available.", "conflict"), BucketState.NAME_CONFLICT),
    (400, _err(400, "Invalid bucket name", "invalid"), BucketState.ERROR),
]


def test_classify_create_error() -> None:
    for status, body, state in _CLASSIFY_CASES:
        assert classify_create_error(status, json.dumps(body)) is state, (status, state)


# ---------- ensure(): existing bucket ----------

def test_existing_private_bucket_is_ok_without_create() -> None:
    fake = FakeGcs({("GET", _B): [(200, _PRIVATE_META)], ("GET", f"{_B}/iam"): [(200, _PRIVATE_IAM)]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.OK and status.usable and not status.created
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_existing_unsafe_bucket_is_refused_never_modified() -> None:
    meta = {**_PRIVATE_META, "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True},
                                                  "publicAccessPrevention": "inherited"}}
    fake = FakeGcs({("GET", _B): [(200, meta)], ("GET", f"{_B}/iam"): [(200, _PRIVATE_IAM)]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.UNSAFE_BUCKET and not status.usable
    assert status.problems and "public access prevention" in status.problems[0]
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_iam_policy_forbidden_is_noted_not_fatal() -> None:
    fake = FakeGcs({("GET", _B): [(200, _PRIVATE_META)],
                    ("GET", f"{_B}/iam"): [(403, _err(403, "no storage.buckets.getIamPolicy", "forbidden"))]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.OK
    assert "IAM policy not inspected" in status.message


def test_bucket_metadata_forbidden_is_permission_denied_with_remedy() -> None:
    fake = FakeGcs({("GET", _B): [(403, _err(403, "no storage.buckets.get access", "forbidden"))]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.PERMISSION_DENIED
    assert "storage.buckets.get" in status.remedy


# ---------- ensure(): auto-create ----------

def test_missing_bucket_is_created_private_then_verified() -> None:
    fake = FakeGcs({
        ("GET", _B): [(404, _err(404, "Not Found", "notFound")), (200, _PRIVATE_META)],
        ("POST", "/storage/v1/b"): [(200, _PRIVATE_META)],
        ("GET", f"{_B}/iam"): [(200, _PRIVATE_IAM)],
    })
    status = _manager(fake).ensure()
    assert status.state is BucketState.CREATED and status.usable and status.created
    post = next(c for c in fake.calls if c[0] == "POST")
    assert post[2]["params"]["project"] == _PROJECT
    assert post[2]["json"]["iamConfiguration"]["publicAccessPrevention"] == "enforced"


def test_create_permission_denied_shows_role_and_command_without_identity_values() -> None:
    fake = FakeGcs({
        ("GET", _B): [(404, _err(404, "Not Found", "notFound"))],
        ("POST", "/storage/v1/b"): [(403, _err(
            403, "run@acme-proj.iam.gserviceaccount.com does not have storage.buckets.create access to the "
                 "Google Cloud project acme-proj.", "forbidden"))],
    })
    status = _manager(fake).ensure()
    assert status.state is BucketState.PERMISSION_DENIED and not status.usable and not status.created
    assert "storage.buckets.create" in status.remedy
    assert "roles/storage.admin" in status.remedy
    assert "gcloud storage buckets create" in status.remedy
    for leak in (_PROJECT, _BUCKET, "iam.gserviceaccount.com"):
        assert leak not in status.remedy and leak not in status.message


def test_create_org_policy_names_constraint() -> None:
    fake = FakeGcs({
        ("GET", _B): [(404, _err(404, "Not Found", "notFound"))],
        ("POST", "/storage/v1/b"): [(412, _err(
            412, "Request violates constraint 'constraints/gcp.resourceLocations'", "conditionNotMet"))],
    })
    status = _manager(fake).ensure()
    assert status.state is BucketState.ORG_POLICY
    assert "constraints/gcp.resourceLocations" in status.message


def test_create_name_conflict_tells_user_to_set_upload_bucket() -> None:
    fake = FakeGcs({
        ("GET", _B): [(404, _err(404, "Not Found", "notFound"))],
        ("POST", "/storage/v1/b"): [(409, _err(409, "The requested bucket name is not available.", "conflict"))],
    })
    status = _manager(fake).ensure()
    assert status.state is BucketState.NAME_CONFLICT
    assert "UPLOAD_BUCKET" in status.remedy


def test_existing_mode_never_creates() -> None:
    fake = FakeGcs({("GET", _B): [(404, _err(404, "Not Found", "notFound"))]})
    status = _manager(fake, mode="existing").ensure()
    assert status.state is BucketState.NOT_FOUND and not status.usable
    assert not any(m == "POST" for m, _, _ in fake.calls)


def test_transient_errors_retry_bounded_then_unreachable() -> None:
    fake = FakeGcs({("GET", _B): [(503, "busy"), (503, "busy"), (503, "busy")]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.UNREACHABLE
    assert len(fake.calls) == 3


def test_timeouts_are_unreachable_not_swallowed() -> None:
    boom = httpx.ReadTimeout("slow")
    fake = FakeGcs({("GET", _B): [(0, boom), (0, boom), (0, boom)]})
    status = _manager(fake).ensure()
    assert status.state is BucketState.UNREACHABLE
    assert "timed out" in status.message


def test_manager_rejects_bad_inputs() -> None:
    fake = FakeGcs({})
    with pytest.raises(ValueError):
        UploadBucketManager(project_id="", bucket=_BUCKET, location="us-central1", mode="auto", kms_key="",
                            token_provider=lambda: "t", timeout_seconds=5, send=fake)
    with pytest.raises(ValueError):
        UploadBucketManager(project_id=_PROJECT, bucket=_BUCKET, location="us-central1", mode="sometimes",
                            kms_key="", token_provider=lambda: "t", timeout_seconds=5, send=fake)
    with pytest.raises(ValueError):
        UploadBucketManager(project_id=_PROJECT, bucket="Bad/Name", location="us-central1", mode="auto",
                            kms_key="", token_provider=lambda: "t", timeout_seconds=5, send=fake)


def test_status_to_dict_masks_bucket_in_safe_mode() -> None:
    fake = FakeGcs({("GET", _B): [(200, _PRIVATE_META)], ("GET", f"{_B}/iam"): [(200, _PRIVATE_IAM)]})
    status = _manager(fake).ensure()
    assert status.to_dict(safe=True)["bucket"] == "${UPLOAD_BUCKET}"
    assert status.to_dict(safe=False)["bucket"] == _BUCKET
    assert status.to_dict(safe=True)["state"] == "ok"
