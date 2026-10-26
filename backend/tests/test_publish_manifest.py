"""Public packaging tests: publish manifest scrub, infra artifacts, and frontend safety invariants."""

from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_PROJECT_IDS = {"", "your-gcp-project-id"}


def _dotenv_value(key: str) -> str:
    """Read one key from the git-ignored .env (identity values never live in the repo)."""
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return os.environ.get(key, "")
    for line in dotenv_path.read_text(encoding="utf-8").splitlines()[:200]:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(key, "")


def _identity_tokens() -> list[str]:
    """Identity-specific forbidden tokens: project id, buckets, and PUBLISH_SCRUB_TOKENS keys from .env."""
    tokens: list[str] = []
    for key in ("GCP_PROJECT_ID", "GCS_CORPUS_BUCKET", "UPLOAD_BUCKET"):
        value = _dotenv_value(key)
        if value and value not in _EXAMPLE_PROJECT_IDS:
            tokens.append(value)
    for pair in _dotenv_value("PUBLISH_SCRUB_TOKENS").split(",")[:64]:
        if "=" in pair and pair.split("=", 1)[0].strip():
            tokens.append(pair.split("=", 1)[0].strip())
    return tokens


def test_publish_manifest_and_public_docs_have_zero_internal_leaks() -> None:
    """Verify publish.json, README.md, docs/ARCHITECTURE.md, and docs/SETUP_GUIDE.md contain zero internal leaks."""
    publish_path = PROJECT_ROOT / "publish.json"
    assert publish_path.exists(), "publish.json must exist at repository root"

    publish_data = json.loads(publish_path.read_text(encoding="utf-8"))
    assert publish_data.get("strategy") == "mirror_and_scrub"
    forbidden_patterns: list[str] = publish_data.get("forbidden_patterns", [])
    assert len(forbidden_patterns) >= 4

    public_docs = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "ARCHITECTURE.md",
        PROJECT_ROOT / "docs" / "SETUP_GUIDE.md",
        PROJECT_ROOT / ".env.example",
    ]
    extra_forbidden = ["Cloudtop", "cloudtop", "gemini-2.5", "gemini-embedding-001", *_identity_tokens()]

    for doc_path in public_docs:
        assert doc_path.exists(), f"Missing public documentation file: {doc_path}"
        content = doc_path.read_text(encoding="utf-8")
        assert len(content.strip()) > 200, f"Documentation file {doc_path.name} is too short"
        for pattern in [*forbidden_patterns, *extra_forbidden]:
            assert (
                pattern.lower() not in content.lower()
            ), f"Forbidden internal or deprecated pattern '{pattern}' found in {doc_path.name}"


def test_frontend_sources_avoid_unsafe_execution_and_hardcoded_project_ids() -> None:
    """Safety invariants only (the v3 UI is rebuilt separately): no eval(), no raw HTML injection, no project ids."""
    src_dir = PROJECT_ROOT / "frontend" / "src"
    assert (PROJECT_ROOT / "frontend" / "package.json").exists()
    assert src_dir.is_dir()
    files = sorted(list(src_dir.rglob("*.ts")) + list(src_dir.rglob("*.tsx")))[:500]
    assert files, "frontend/src must contain TypeScript sources"
    identity_tokens = _identity_tokens()
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "eval(" not in text, f"Forbidden eval() found in {path}"
        assert "dangerouslySetInnerHTML" not in text, f"Forbidden dangerouslySetInnerHTML in {path}"
        for token in identity_tokens:
            assert token not in text, f"Hardcoded identity token in {path} (read it from /api/v1/health)"


def test_infra_docker_and_bootstrap_artifacts_exist() -> None:
    """Verify scripts/bootstrap_gcp.sh, infra/main.tf, the root Dockerfile, and docker-compose.yml exist and are valid."""
    bootstrap_path = PROJECT_ROOT / "scripts" / "bootstrap_gcp.sh"
    assert bootstrap_path.exists(), "Missing scripts/bootstrap_gcp.sh"
    assert os.access(bootstrap_path, os.X_OK), "scripts/bootstrap_gcp.sh must be executable"
    bootstrap_text = bootstrap_path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in bootstrap_text
    for api_name in (
        "aiplatform.googleapis.com",
        "bigquery.googleapis.com",
        "discoveryengine.googleapis.com",
        "documentai.googleapis.com",
        "storage.googleapis.com",
        "alloydb.googleapis.com",
    ):
        assert api_name in bootstrap_text, f"Missing API {api_name} in bootstrap_gcp.sh"
    assert "gcloud storage buckets create" in bootstrap_text
    assert "--uniform-bucket-level-access" in bootstrap_text
    assert "--public-access-prevention" in bootstrap_text
    assert "--soft-delete-duration" not in bootstrap_text
    assert "gcloud storage cp --recursive" in bootstrap_text
    assert "rag_arena" in bootstrap_text
    assert "CREATE VECTOR INDEX IF NOT EXISTS" in bootstrap_text
    assert "embedding ARRAY<FLOAT64>" in bootstrap_text
    assert "bbox_json" not in bootstrap_text

    tf_path = PROJECT_ROOT / "infra" / "main.tf"
    assert tf_path.exists(), "Missing infra/main.tf"
    tf_text = tf_path.read_text(encoding="utf-8")
    assert "google_project_service" in tf_text
    assert "google_storage_bucket" in tf_text
    assert "google_bigquery_dataset" in tf_text
    assert "google_bigquery_table" in tf_text
    assert '"embedding"' in tf_text or 'name = "embedding"' in tf_text
    assert "bbox_json" not in tf_text
    assert "text_embedding" not in tf_text

    dockerfile_path = PROJECT_ROOT / "Dockerfile"  # v3 single image (SPA + API); the pre-v3 backend/Dockerfile is gone
    assert dockerfile_path.exists(), "Missing root Dockerfile"
    assert not (PROJECT_ROOT / "backend" / "Dockerfile").exists(), "stale pre-v3 backend/Dockerfile must not return"
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    assert "8080" in dockerfile_text
    assert "app.main:app" in dockerfile_text

    compose_path = PROJECT_ROOT / "docker-compose.yml"
    assert compose_path.exists(), "Missing docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    assert "8080:8080" in compose_text
    assert "dockerfile: Dockerfile" in compose_text
    assert "DEMO_MODE=replay" in compose_text


def test_mirror_and_scrub_pipeline_exports_clean_bundle() -> None:
    """Verify scripts/publish_mirror_scrub.py exports a scrubbed public bundle with pruned internal files removed."""
    import importlib.util
    import tempfile

    script_path = PROJECT_ROOT / "scripts" / "publish_mirror_scrub.py"
    assert script_path.exists(), "Missing scripts/publish_mirror_scrub.py"
    spec = importlib.util.spec_from_file_location("publish_mirror_scrub", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir) / "public_export"
        summary = mod.run_mirror_and_scrub(PROJECT_ROOT, output_dir=out_dir)
        assert summary["status"] == "exported"
        assert int(summary["copied_files"]) >= 40
        assert (out_dir / "README.md").exists()
        assert (out_dir / "backend" / "app" / "main.py").exists()
        assert (out_dir / "frontend" / "package.json").exists()
        assert not (out_dir / "THE_RAG_ARENA_SPEC.md").exists()
        assert not (out_dir / "spec.md").exists()
        assert not (out_dir / "task.md").exists()
        assert not (out_dir / "publish.json").exists()

