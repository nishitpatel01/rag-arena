"""Phase 6 Cloud Run packaging: SPA serving with history fallback, replay health masking, image/context hygiene,
and the config-driven deploy script (validated offline in --dry-run mode with a fake gcloud that must never run)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from app.core.config import ArenaSettings, get_settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy_cloudrun.sh"
TEARDOWN_SCRIPT = PROJECT_ROOT / "scripts" / "teardown_cloudrun.sh"
_INDEX_HTML = "<!doctype html><html><body><div id='root'>spa-index</div></body></html>"


def _static_dir(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('spa');", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist


def _spa_client(static_dir: Path, mode: str = "replay") -> TestClient:
    settings = ArenaSettings(demo_mode=mode, static_dir=str(static_dir))
    return TestClient(create_app(settings=settings))


# --- SPA static serving -------------------------------------------------------------------------------------------


def test_api_routes_win_over_the_spa_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _spa_client(_static_dir(Path(tmp)))
        res = client.get("/api/v1/health")
        assert res.status_code == 200 and res.headers["content-type"].startswith("application/json")
        assert res.json()["status"] == "ok"


def test_unknown_api_path_is_404_json_not_index_html() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _spa_client(_static_dir(Path(tmp)))
        for method in ("get", "post"):
            res = getattr(client, method)("/api/v1/does-not-exist")
            assert res.status_code == 404, method
            assert res.headers["content-type"].startswith("application/json"), method
            assert res.json()["error"] == "not_found"
            assert "spa-index" not in res.text


def test_client_route_deep_links_return_index_html() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _spa_client(_static_dir(Path(tmp)))
        for path in ("/", "/levels/level4_agentic", "/compare", "/leaderboard?engine=x", "/ingest/level1_naive"):
            res = client.get(path)
            assert res.status_code == 200, path
            assert res.headers["content-type"].startswith("text/html"), path
            assert "spa-index" in res.text, path
            assert res.headers.get("cache-control") == "no-cache", path


def test_static_assets_are_served_and_missing_assets_are_404() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _spa_client(_static_dir(Path(tmp)))
        asset = client.get("/assets/app-abc123.js")
        assert asset.status_code == 200 and "console.log" in asset.text
        assert "immutable" in asset.headers.get("cache-control", "")
        assert client.get("/favicon.svg").status_code == 200
        missing = client.get("/assets/missing-999.js")
        assert missing.status_code == 404 and "spa-index" not in missing.text


def test_static_serving_rejects_path_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "secret.txt").write_text("top-secret", encoding="utf-8")
        client = _spa_client(_static_dir(root))
        for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
            res = client.get(path)
            assert "top-secret" not in res.text, path


def test_static_dir_setting_fails_loud_when_index_html_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            create_app(settings=ArenaSettings(demo_mode="replay", static_dir=tmp))
        except ValueError as exc:
            assert "index.html" in str(exc)
        else:
            raise AssertionError("STATIC_DIR without index.html must fail at startup")


def test_static_dir_is_read_from_the_environment() -> None:
    settings = get_settings({"DEMO_MODE": "replay", "STATIC_DIR": "/app/frontend/dist"})
    assert settings.static_dir == "/app/frontend/dist"
    assert get_settings({"DEMO_MODE": "replay"}).static_dir == ""


# --- Replay health must not reveal the owner's identity -----------------------------------------------------------


def test_replay_health_masks_project_and_bucket() -> None:
    settings = ArenaSettings(demo_mode="replay", gcp_project_id="owner-project-123", gcs_corpus_bucket="owner-bucket-xyz")
    res = TestClient(create_app(settings=settings)).get("/api/v1/health")
    body = res.json()
    assert body["demo_mode"] == "replay"
    assert body["project_id"] == "" and body["gcs_bucket"] == ""
    assert "owner-project-123" not in res.text and "owner-bucket-xyz" not in res.text


def test_live_health_still_reports_project_and_bucket() -> None:
    settings = ArenaSettings(demo_mode="live", gcp_project_id="owner-project-123", gcs_corpus_bucket="owner-bucket-xyz")
    app = create_app(settings=settings, query_run_writer=object())
    body = TestClient(app).get("/api/v1/health").json()
    assert body["project_id"] == "owner-project-123" and body["gcs_bucket"] == "owner-bucket-xyz"


# --- Image / upload context hygiene -------------------------------------------------------------------------------


def _ignore_lines(name: str) -> set[str]:
    path = PROJECT_ROOT / name
    assert path.is_file(), f"missing {name}"
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")}


def test_dockerignore_and_gcloudignore_exclude_secrets_and_local_state() -> None:
    for name in (".dockerignore", ".gcloudignore"):
        lines = _ignore_lines(name)
        for required in (".env", ".env.*", "deploy/*.env", ".venv", "**/node_modules", "frontend/dist", "dist", ".git",
                         "**/__pycache__", "*.pem", "*.key", "*credentials*.json", "scratch", "*.tfstate"):
            assert required in lines, f"{name} must exclude {required}"
        assert "!deploy/cloudrun.env.example" not in lines or name == ".gcloudignore"


def test_gitignore_excludes_the_deploy_env_file() -> None:
    lines = _ignore_lines(".gitignore")
    assert "deploy/cloudrun.env" in lines


def test_dockerfile_is_multistage_non_root_and_bakes_no_env() -> None:
    text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("FROM ") >= 2 and "npm ci" in text and "npm run build" in text
    assert "python:3.12-slim" in text and "USER " in text
    assert "STATIC_DIR=" in text and "${PORT" in text
    for forbidden in ("COPY .env", "COPY deploy", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "COPY . "):
        assert forbidden not in text, f"Dockerfile must not contain {forbidden!r}"


def test_deploy_env_example_has_only_placeholders() -> None:
    text = (PROJECT_ROOT / "deploy" / "cloudrun.env.example").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    assert values["GCP_PROJECT_ID"] in ("", "your-gcp-project-id")
    assert values["IAP_MEMBERS"] in ("", "user:you@example.com")
    for key in ("REGION", "SERVICE_NAME", "ACCESS_MODE", "DEPLOY_REPLAY", "REPLAY_SERVICE_NAME", "ARTIFACT_REPO",
                "RUNTIME_SA_NAME", "REPLAY_SA_NAME", "LIVE_INGEST", "MIN_INSTANCES", "MAX_INSTANCES",
                "REPLAY_MAX_INSTANCES", "CPU", "MEMORY", "REPLAY_MEMORY", "TIMEOUT", "REPLAY_TIMEOUT",
                "VAIS_UPLOADS_DATA_STORE_ID", "VAIS_UPLOADS_ENGINE_ID"):
        assert key in values, f"cloudrun.env.example is missing {key}"
    dotenv_text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    dotenv_values = dict(line.split("=", 1) for line in dotenv_text.splitlines() if "=" in line and not line.startswith("#"))
    assert "VAIS_UPLOADS_DATA_STORE_ID" in dotenv_values and "VAIS_UPLOADS_ENGINE_ID" in dotenv_values
    assert "CLOUDSDK_CONFIG" not in values and "GOOGLE_APPLICATION_CREDENTIALS" not in values


# --- deploy_cloudrun.sh (offline, --dry-run) ----------------------------------------------------------------------

_VALID_ENV = {
    "GCP_PROJECT_ID": "demo-project-1",
    "REGION": "us-central1",
    "SERVICE_NAME": "rag-arena",
    "ACCESS_MODE": "iap",
    "IAP_MEMBERS": "user:a@example.com,group:team@example.com",
    "DEPLOY_REPLAY": "true",
    "REPLAY_SERVICE_NAME": "rag-arena-replay",
    "ARTIFACT_REPO": "rag-arena",
    "RUNTIME_SA_NAME": "rag-arena-run",
    "REPLAY_SA_NAME": "rag-arena-replay",
    "LIVE_INGEST": "true",
    "GCS_CORPUS_BUCKET": "demo-project-1-rag-arena-corpus",
    "BQ_DATASET": "rag_arena",
    "MIN_INSTANCES": "0",
    "MAX_INSTANCES": "2",
    "REPLAY_MAX_INSTANCES": "3",
    "CPU": "1",
    "MEMORY": "2Gi",
    "REPLAY_MEMORY": "1Gi",
    "TIMEOUT": "900",
    "REPLAY_TIMEOUT": "300",
    "DEMO_MODE": "live",
    "ENABLED_LEVELS": "level1_naive,level2_hybrid",
    "QUERY_TIMEOUT_SECONDS": "120",
    "INGEST_JOB_TIMEOUT_SECONDS": "840",
}


def _run_deploy(overrides: dict[str, str | None], *args: str, script: Path = DEPLOY_SCRIPT) -> subprocess.CompletedProcess[str]:
    env_values = {**_VALID_ENV, **overrides}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        env_file = tmp_path / "cloudrun.env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_values.items() if v is not None) + "\n", encoding="utf-8"
        )
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        sentinel = tmp_path / "gcloud-called"
        fake_gcloud = fake_bin / "gcloud"
        fake_gcloud.write_text(f"#!/usr/bin/env bash\ntouch '{sentinel}'\nexit 97\n", encoding="utf-8")
        fake_gcloud.chmod(0o755)
        env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8"}
        proc = subprocess.run(
            ["bash", str(script), "--env-file", str(env_file), *args],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(PROJECT_ROOT), check=False,
        )
        assert not sentinel.exists(), f"--dry-run must never invoke gcloud\n{proc.stdout}\n{proc.stderr}"
        return proc


def test_deploy_script_is_strict_bash_and_executable() -> None:
    for script in (DEPLOY_SCRIPT, TEARDOWN_SCRIPT):
        assert os.access(script, os.X_OK), f"{script.name} must be executable"
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
        assert "--yes" in text and "--dry-run" in text


def test_deploy_dry_run_plans_exactly_the_expected_resources() -> None:
    proc = _run_deploy({}, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    plan = [line[len("PLAN ") :] for line in proc.stdout.splitlines() if line.startswith("PLAN ")]
    expected = [
        "enable-apis run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com iap.googleapis.com",
        "artifact-repo rag-arena (us-central1, docker)",
        "cloud-build us-central1-docker.pkg.dev/demo-project-1/rag-arena/rag-arena:<tag>",
        "service-account rag-arena-run",
        "iam project roles/aiplatform.user -> rag-arena-run",
        "iam project roles/bigquery.jobUser -> rag-arena-run",
        "iam dataset rag_arena roles/bigquery.dataEditor -> rag-arena-run",
        "iam project roles/documentai.apiUser -> rag-arena-run",
        "iam project roles/discoveryengine.editor -> rag-arena-run",
        "iam bucket demo-project-1-rag-arena-corpus roles/storage.objectUser -> rag-arena-run",
        "upload-bucket demo-project-1-rag-arena-uploads (auto: create if missing; private; us-central1)",
        "iam bucket demo-project-1-rag-arena-uploads roles/storage.objectUser -> rag-arena-run",
        "iam bucket demo-project-1-rag-arena-uploads roles/storage.legacyBucketReader -> rag-arena-run",
        "iam bucket demo-project-1-rag-arena-uploads roles/storage.objectViewer -> discoveryengine-service-agent",
        "service rag-arena (live, iap, sa=rag-arena-run, min=0, max=2, cpu=1, memory=2Gi, timeout=900s)",
        "iap-service-agent identity",
        "iam service rag-arena roles/run.invoker -> iap-service-agent",
        "iam iap rag-arena roles/iap.httpsResourceAccessor -> user:a@example.com",
        "iam iap rag-arena roles/iap.httpsResourceAccessor -> group:team@example.com",
        "service-account rag-arena-replay (no roles)",
        "service rag-arena-replay (replay, public, sa=rag-arena-replay, min=0, max=3, cpu=1, memory=1Gi, timeout=300s)",
    ]
    assert plan == expected, "\n".join(plan)
    assert "DRY RUN" in proc.stdout


def test_deploy_dry_run_public_without_replay_and_read_only_ingest() -> None:
    proc = _run_deploy({"ACCESS_MODE": "public", "IAP_MEMBERS": None, "DEPLOY_REPLAY": "false", "LIVE_INGEST": "false"}, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    plan = [line[len("PLAN ") :] for line in proc.stdout.splitlines() if line.startswith("PLAN ")]
    assert "iam project roles/discoveryengine.viewer -> rag-arena-run" in plan
    assert "iam bucket demo-project-1-rag-arena-corpus roles/storage.objectViewer -> rag-arena-run" in plan
    assert not any("iap" in line for line in plan[1:]), plan
    assert not any("replay" in line for line in plan), plan
    assert any(line.startswith("service rag-arena (live, public,") for line in plan)
    assert "WARNING" in proc.stdout + proc.stderr and "cost" in (proc.stdout + proc.stderr).lower()


def test_deploy_env_vars_never_forward_local_credentials() -> None:
    proc = _run_deploy(
        {
            "CLOUDSDK_CONFIG": "/home/x/.config/gcloud-x",
            "GOOGLE_APPLICATION_CREDENTIALS": "/home/x/adc.json",
            "VAIS_UPLOADS_DATA_STORE_ID": "rag-arena-uploads",
            "VAIS_UPLOADS_ENGINE_ID": "rag-arena-uploads-search",
        },
        "--dry-run",
    )
    assert proc.returncode == 0, proc.stderr
    assert "/home/x" not in proc.stdout
    env_lines = [line for line in proc.stdout.splitlines() if line.startswith("ENV live ")]
    assert env_lines and not any("CLOUDSDK_CONFIG" in l or "GOOGLE_APPLICATION_CREDENTIALS" in l for l in env_lines)
    assert "ENV live VAIS_UPLOADS_DATA_STORE_ID=rag-arena-uploads" in env_lines
    assert "ENV live VAIS_UPLOADS_ENGINE_ID=rag-arena-uploads-search" in env_lines
    replay_env = [line for line in proc.stdout.splitlines() if line.startswith("ENV replay ")]
    keys = {line.split()[2].split("=")[0] for line in replay_env}
    assert keys == {"DEMO_MODE", "ALLOW_DEV_FIXTURES", "ENABLED_LEVELS"}, keys
    assert "ENV replay DEMO_MODE=replay" in proc.stdout and "ENV live DEMO_MODE=live" in proc.stdout


def _assert_rejected(overrides: dict[str, str | None], needle: str) -> None:
    proc = _run_deploy(overrides, "--dry-run")
    assert proc.returncode != 0, f"expected failure for {overrides}\n{proc.stdout}"
    assert needle in proc.stderr, f"expected {needle!r} in stderr for {overrides}:\n{proc.stderr}"
    assert "PLAN " not in proc.stdout


def test_deploy_rejects_missing_and_invalid_keys() -> None:
    _assert_rejected({"GCP_PROJECT_ID": None}, "GCP_PROJECT_ID is required")
    _assert_rejected({"GCP_PROJECT_ID": "Bad_Project!"}, "GCP_PROJECT_ID")
    _assert_rejected({"REGION": "us central1"}, "REGION")
    _assert_rejected({"ACCESS_MODE": "open"}, "ACCESS_MODE must be iap or public")
    _assert_rejected({"IAP_MEMBERS": None}, "IAP_MEMBERS is required when ACCESS_MODE=iap")
    _assert_rejected({"IAP_MEMBERS": "a@example.com"}, "IAP_MEMBERS entry")
    _assert_rejected({"IAP_MEMBERS": "allUsers"}, "IAP_MEMBERS entry")
    _assert_rejected({"DEPLOY_REPLAY": "maybe"}, "DEPLOY_REPLAY must be true or false")
    _assert_rejected({"LIVE_INGEST": "1"}, "LIVE_INGEST must be true or false")
    _assert_rejected({"SERVICE_NAME": "Rag_Arena"}, "SERVICE_NAME")
    _assert_rejected({"REPLAY_SERVICE_NAME": "rag-arena"}, "REPLAY_SERVICE_NAME must differ from SERVICE_NAME")
    _assert_rejected({"REPLAY_SA_NAME": "rag-arena-run"}, "REPLAY_SA_NAME must differ from RUNTIME_SA_NAME")
    _assert_rejected({"RUNTIME_SA_NAME": "x"}, "RUNTIME_SA_NAME")
    _assert_rejected({"MAX_INSTANCES": "0"}, "MAX_INSTANCES")
    _assert_rejected({"MAX_INSTANCES": "50"}, "MAX_INSTANCES")
    _assert_rejected({"MIN_INSTANCES": "3"}, "MIN_INSTANCES")
    _assert_rejected({"TIMEOUT": "120"}, "TIMEOUT")
    _assert_rejected({"TIMEOUT": "4000"}, "TIMEOUT")
    _assert_rejected({"MEMORY": "2GB"}, "MEMORY")
    _assert_rejected({"DEMO_MODE": "replay"}, "DEMO_MODE must be live")
    _assert_rejected({"GCS_CORPUS_BUCKET": "Bad Bucket"}, "GCS_CORPUS_BUCKET")
    _assert_rejected({"UPLOAD_BUCKET": "Bad Bucket"}, "UPLOAD_BUCKET")
    _assert_rejected({"UPLOAD_BUCKET_MODE": "sometimes"}, "UPLOAD_BUCKET_MODE must be auto or existing")
    _assert_rejected({"UPLOAD_BUCKET_KMS_KEY": "not-a-key"}, "UPLOAD_BUCKET_KMS_KEY")


def test_deploy_requires_yes_or_dry_run_and_rejects_unknown_flags() -> None:
    proc = _run_deploy({})
    assert proc.returncode != 0 and "--yes" in proc.stderr
    proc = _run_deploy({}, "--bogus")
    assert proc.returncode != 0 and "unknown argument" in proc.stderr


def test_deploy_rejects_unknown_keys_in_env_file() -> None:
    _assert_rejected({"SERVCE_NAME": "typo"}, "unknown key SERVCE_NAME")


def test_teardown_dry_run_lists_only_script_owned_resources() -> None:
    proc = _run_deploy({}, "--dry-run", script=TEARDOWN_SCRIPT)
    assert proc.returncode == 0, proc.stderr
    plan = [line[len("DELETE ") :] for line in proc.stdout.splitlines() if line.startswith("DELETE ")]
    assert plan == [
        "service rag-arena-replay (us-central1)",
        "service rag-arena (us-central1)",
        "iam project/dataset/bucket bindings of rag-arena-run",
        "iam upload-bucket bindings of rag-arena-run + discoveryengine-service-agent",
        "service-account rag-arena-replay",
        "service-account rag-arena-run",
        "artifact-repo rag-arena (us-central1)",
    ], "\n".join(plan)
    proc = _run_deploy({}, script=TEARDOWN_SCRIPT)
    assert proc.returncode != 0 and "--yes" in proc.stderr


def test_frontend_lockfile_resolves_only_from_the_public_npm_registry() -> None:
    """Cloud Build and GitHub users cannot reach a corp npm proxy; the lockfile must point at registry.npmjs.org."""
    import json
    lock = json.loads((PROJECT_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    resolved = [p["resolved"] for p in lock.get("packages", {}).values() if p.get("resolved")]
    assert resolved, "lockfile must pin resolved tarball URLs"
    bad = [url for url in resolved if not url.startswith("https://registry.npmjs.org/")]
    assert not bad, f"{len(bad)} lockfile entries resolve outside registry.npmjs.org, e.g. {bad[0][:60]}"


def test_image_installs_the_public_pypi_lock_with_adk_2() -> None:
    root = Path(__file__).resolve().parents[2]
    lock = (root / "backend" / "requirements.lock").read_text(encoding="utf-8")
    assert "google-adk==2.12.0" in lock.splitlines()
    for bad in ("uplink" + ".goog", "air" + "lock", "artifact" + "-foundry"):
        assert bad not in lock, bad
    assert not any(line.startswith(("--index-url", "-i ", "--extra-index-url")) for line in lock.splitlines())
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir -r backend/requirements.lock" in dockerfile
    assert "pip install -r backend/requirements.txt" not in dockerfile
