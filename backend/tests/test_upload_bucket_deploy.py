"""v1.1 step 1c: deploy_cloudrun.sh creates/verifies the private upload bucket and grants every reader (fake gcloud)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile

from test_cloudrun_deploy import DEPLOY_SCRIPT, PROJECT_ROOT, TEARDOWN_SCRIPT, _VALID_ENV

_BUCKET = "demo-project-1-rag-arena-uploads"

# Scenario-driven fake gcloud: every call is logged; behaviour is chosen by $SCENARIO; "created" marker = state.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
echo "$*" >> "$STATE/calls.log"
case "$*" in
  "auth list"*) echo "deployer@example.com"; exit 0 ;;
  "projects describe"*) echo "123456789012"; exit 0 ;;
  "beta services identity create"*) echo "Service identity created: service-123456789012@gcp-sa-discoveryengine.iam.gserviceaccount.com"; exit 0 ;;
  "storage buckets add-iam-policy-binding"*) exit 0 ;;
  "storage buckets remove-iam-policy-binding"*) exit 0 ;;
  "storage buckets describe"*)
    if [[ "$SCENARIO" == exists_private || -f "$STATE/created" ]]; then
      echo '{"name": "x", "public_access_prevention": "enforced", "uniform_bucket_level_access": true}'; exit 0
    fi
    if [[ "$SCENARIO" == exists_unsafe ]]; then
      echo '{"name": "x", "public_access_prevention": "inherited", "uniform_bucket_level_access": false}'; exit 0
    fi
    if [[ "$SCENARIO" == describe_denied ]]; then
      echo "ERROR: (gcloud.storage.buckets.describe) HTTPError 403: deployer@example.com does not have storage.buckets.get access to the Google Cloud Storage bucket." >&2; exit 1
    fi
    echo "ERROR: (gcloud.storage.buckets.describe) gs://$BUCKET not found: 404." >&2; exit 1 ;;
  "storage buckets create"*)
    case "$SCENARIO" in
      create_ok) touch "$STATE/created"; echo "Creating gs://$BUCKET/..."; exit 0 ;;
      create_denied) echo "ERROR: (gcloud.storage.buckets.create) HTTPError 403: deployer@example.com does not have storage.buckets.create access to the Google Cloud project. Permission 'storage.buckets.create' denied on resource (or it may not exist)." >&2; exit 1 ;;
      create_orgpolicy) echo "ERROR: (gcloud.storage.buckets.create) HTTPError 412: Request violates constraint 'constraints/gcp.resourceLocations'" >&2; exit 1 ;;
      create_conflict) echo "ERROR: (gcloud.storage.buckets.create) HTTPError 409: The requested bucket name is not available." >&2; exit 1 ;;
    esac ;;
esac
echo "fake gcloud: unexpected call: $*" >&2
exit 97
"""


def _run_upload_only(scenario: str, overrides: dict[str, str | None] | None = None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    env_values = {**_VALID_ENV, **(overrides or {})}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        env_file = tmp_path / "cloudrun.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in env_values.items() if v is not None) + "\n", encoding="utf-8")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
        (fake_bin / "gcloud").chmod(0o755)
        env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8", "SCENARIO": scenario,
               "STATE": tmp, "BUCKET": _BUCKET}
        proc = subprocess.run(["bash", str(DEPLOY_SCRIPT), "--env-file", str(env_file), "--upload-bucket-only"],
                              capture_output=True, text=True, timeout=60, env=env, cwd=str(PROJECT_ROOT), check=False)
        log = tmp_path / "calls.log"
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc, calls


def _grants(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("storage buckets add-iam-policy-binding")]


def test_existing_private_bucket_is_verified_and_all_readers_granted() -> None:
    proc, calls = _run_upload_only("exists_private")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(c.startswith("storage buckets create") for c in calls)
    grants = _grants(calls)
    assert any("serviceAccount:rag-arena-run@demo-project-1.iam.gserviceaccount.com" in g
               and "roles/storage.objectUser" in g for g in grants)
    assert any("serviceAccount:rag-arena-run@demo-project-1.iam.gserviceaccount.com" in g
               and "roles/storage.legacyBucketReader" in g for g in grants)
    assert any("service-123456789012@gcp-sa-discoveryengine.iam.gserviceaccount.com" in g
               and "roles/storage.objectViewer" in g for g in grants)
    assert all(f"gs://{_BUCKET}" in g for g in grants)
    assert not any("replay" in g for g in grants), "the replay service account never gets bucket access"
    assert "UPLOAD BUCKET ok" in proc.stdout


def test_missing_bucket_is_created_private_by_default() -> None:
    proc, calls = _run_upload_only("create_ok")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    create = next(c for c in calls if c.startswith("storage buckets create"))
    for flag in (f"gs://{_BUCKET}", "--location=us-central1", "--uniform-bucket-level-access",
                 "--public-access-prevention"):
        assert flag in create, create
    assert "--lifecycle" not in create  # D2: uploads are kept until manually deleted
    assert len(_grants(calls)) == 3
    assert "UPLOAD BUCKET created" in proc.stdout


def test_cmek_key_is_passed_on_create() -> None:
    key = "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k"
    proc, calls = _run_upload_only("create_ok", {"UPLOAD_BUCKET_KMS_KEY": key})
    assert proc.returncode == 0, proc.stderr
    assert f"--default-encryption-key={key}" in next(c for c in calls if c.startswith("storage buckets create"))


def test_create_permission_denied_shows_role_and_command_and_grants_nothing() -> None:
    proc, calls = _run_upload_only("create_denied")
    assert proc.returncode == 3
    out = proc.stdout + proc.stderr
    assert "UPLOAD BUCKET permission_denied" in out
    assert "storage.buckets.create" in out and "roles/storage.admin" in out
    assert "gcloud storage buckets create" in out
    assert _grants(calls) == []


def test_create_blocked_by_org_policy_names_constraint() -> None:
    proc, calls = _run_upload_only("create_orgpolicy")
    assert proc.returncode == 3
    out = proc.stdout + proc.stderr
    assert "UPLOAD BUCKET org_policy" in out and "constraints/gcp.resourceLocations" in out
    assert _grants(calls) == []


def test_create_name_conflict_asks_for_a_different_name() -> None:
    proc, _ = _run_upload_only("create_conflict")
    assert proc.returncode == 3
    assert "UPLOAD BUCKET name_conflict" in proc.stdout + proc.stderr and "UPLOAD_BUCKET" in proc.stderr


def test_existing_mode_never_creates() -> None:
    proc, calls = _run_upload_only("missing", {"UPLOAD_BUCKET_MODE": "existing"})
    assert proc.returncode == 3
    assert "UPLOAD BUCKET not_found" in proc.stdout + proc.stderr
    assert not any(c.startswith("storage buckets create") for c in calls)


def test_unsafe_existing_bucket_is_refused_and_never_modified() -> None:
    proc, calls = _run_upload_only("exists_unsafe")
    assert proc.returncode == 3
    out = proc.stdout + proc.stderr
    assert "UPLOAD BUCKET unsafe_bucket" in out and "public access prevention" in out
    assert _grants(calls) == [] and not any("update" in c for c in calls)


def test_describe_denied_is_permission_denied_not_created() -> None:
    proc, calls = _run_upload_only("describe_denied")
    assert proc.returncode == 3
    assert "UPLOAD BUCKET permission_denied" in proc.stdout + proc.stderr
    assert not any(c.startswith("storage buckets create") for c in calls)


def test_upload_bucket_must_differ_from_corpus_bucket() -> None:
    proc, calls = _run_upload_only("exists_private", {"UPLOAD_BUCKET": _VALID_ENV["GCS_CORPUS_BUCKET"]})
    assert proc.returncode != 0 and "UPLOAD_BUCKET must differ from GCS_CORPUS_BUCKET" in proc.stderr
    assert calls == []


def test_teardown_removes_upload_grants_but_keeps_the_bucket() -> None:
    from test_cloudrun_deploy import _run_deploy

    proc = _run_deploy({}, "--dry-run", script=TEARDOWN_SCRIPT)
    assert proc.returncode == 0, proc.stderr
    assert f"KEEP upload bucket {_BUCKET} (your documents; delete it yourself if you want)" in proc.stdout
