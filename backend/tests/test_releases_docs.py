"""Tests for Release v1.5.0 release history & documentation compiler, API endpoint, and drift CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.core.releases_docs import (
    DocsManifestError,
    build_releases_manifest,
    parse_changelog,
    parse_markdown_toc,
    slugify_heading,
    verify_repo_release_alignment,
)
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_SCRIPT = PROJECT_ROOT / "scripts" / "build_docs_manifest.py"


def test_parse_changelog_supports_keep_a_changelog_and_release_please_formats() -> None:
    sample = """# Changelog

All notable changes to this project are documented here.

## [1.5.0](https://github.com/example/rag-arena/compare/v1.4.0...v1.5.0) (2026-11-16)

In-app Releases & Docs tab (`/releases`) and deterministic `release-please` workflow.

### Features
* **Releases & Docs Tab (`/releases`)**: interactive timeline and `/workbench` deep links.
  * Nested sub-bullet detail for `level1_naive` and `/compare`.

### Bug Fixes
* tighten TOC slug deduplication on `/leaderboard`

## [1.4.0] — 2026-11-04

Ephemeral ScaNN benchmarks (`alloydb_scann` and `vertex_vector_v2`).

### Added
- **Level 7 (`alloydb_scann`)**: PostgreSQL + ScaNN hybrid RRF.
- **Level 8 (`vertex_vector_v2`)**: Vector Search 2.0 `batchSearch` RRF.
"""
    entries = parse_changelog(sample)
    assert len(entries) == 2

    v15 = entries[0]
    assert v15.version == "1.5.0"
    assert v15.date == "2026-11-16"
    assert v15.compare_url == "https://github.com/example/rag-arena/compare/v1.4.0...v1.5.0"
    assert "/releases" in v15.summary
    assert [s.heading for s in v15.sections] == ["Features", "Bug Fixes"]
    assert len(v15.sections[0].items) == 1
    assert "Nested sub-bullet" in v15.sections[0].items[0]
    routes_15 = {link["path"] for link in v15.route_links}
    assert {"/workbench", "/compare", "/leaderboard", "/levels/level1_naive"} <= routes_15

    v14 = entries[1]
    assert v14.version == "1.4.0"
    assert v14.date == "2026-11-04"
    assert v14.compare_url == ""
    routes_14 = {link["path"] for link in v14.route_links}
    assert {"/levels/alloydb_scann", "/levels/vertex_vector_v2"} <= routes_14


def test_parse_changelog_rejects_empty_or_versionless_input() -> None:
    for bad in ("", "# Changelog\n\nNo releases yet.\n"):
        try:
            parse_changelog(bad)
        except DocsManifestError:
            pass
        else:
            raise AssertionError(f"Expected DocsManifestError for {bad!r}")


def test_parse_markdown_toc_ignores_fenced_code_comments_and_deduplicates_slugs() -> None:
    md = """# Title H1

## 0. Design principles (v3)

```bash
# not a heading
## also inside code fence
```

### Handler contract
### Handler contract
"""
    toc = parse_markdown_toc(md)
    assert [(item["level"], item["title"], item["id"]) for item in toc] == [
        (2, "0. Design principles (v3)", "0-design-principles-v3"),
        (3, "Handler contract", "handler-contract"),
        (3, "Handler contract", "handler-contract-2"),
    ]
    assert slugify_heading("3b. Level 2 query path (`engines/level2_hybrid.py`)") == "3b-level-2-query-path-engineslevel2_hybridpy"


def test_releases_api_endpoint_serves_real_repo_docs_and_no_store_header() -> None:
    app = create_app(settings=ArenaSettings(demo_mode="replay"))
    client = TestClient(app)
    res = client.get("/api/v1/releases")
    assert res.status_code == 200, res.text
    assert res.headers.get("cache-control") == "no-store"
    body = res.json()

    assert body["app_version"] == ArenaSettings().app_version
    assert body["demo_mode"] == "replay"
    assert body["stats"]["architecture_count"] == 10
    assert body["stats"]["total_recordings"] >= 135
    versions = [r["version"] for r in body["releases"]]
    assert versions[0] == ArenaSettings().app_version
    for expected_ver in ("1.4.0", "1.3.0", "1.2.0", "1.1.1", "1.1.0", "1.0.0"):
        assert expected_ver in versions, f"Missing {expected_ver} in {versions}"

    docs = {d["id"]: d for d in body["documents"]}
    assert set(docs.keys()) == {"architecture", "setup_guide"}
    assert len(docs["architecture"]["toc"]) >= 8
    assert len(docs["setup_guide"]["toc"]) >= 5
    assert "## 0. Design principles" in docs["architecture"]["markdown"]


def test_releases_manifest_rejects_symlink_escape_outside_project_root() -> None:
    with tempfile.TemporaryDirectory() as outer_tmp:
        outer = Path(outer_tmp)
        secret = outer / "outside_secret.md"
        secret.write_text("## [9.9.9] — 2026-01-01\n### Added\n- leaked\n", encoding="utf-8")
        repo = outer / "repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "CHANGELOG.md").symlink_to(secret)
        (repo / "docs" / "ARCHITECTURE.md").write_text("# Arch\n## Section\ntext\n", encoding="utf-8")
        (repo / "docs" / "SETUP_GUIDE.md").write_text("# Setup\n## Section\ntext\n", encoding="utf-8")
        try:
            build_releases_manifest(ArenaSettings(demo_mode="replay"), project_root=repo)
        except DocsManifestError as exc:
            assert "outside" in str(exc).lower() or "boundary" in str(exc).lower()
        else:
            raise AssertionError("Expected symlink outside project_root to be rejected")


def test_verify_alignment_detects_version_drift_and_cli_script_succeeds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp)
        (fake_root / "backend" / "app" / "core").mkdir(parents=True)
        (fake_root / "frontend").mkdir(parents=True)
        (fake_root / "docs").mkdir(parents=True)
        dev_dir = fake_root / "data" / "recordings" / "_dev" / "level1_naive" / "gemini-4-argon__claude-opus-5-5"
        dev_dir.mkdir(parents=True)

        (fake_root / "CHANGELOG.md").write_text("## [1.5.0] — 2026-11-16\n### Added\n- feature\n", encoding="utf-8")
        (fake_root / "docs" / "ARCHITECTURE.md").write_text("# Arch\n## S1\nBody text.\n", encoding="utf-8")
        (fake_root / "docs" / "SETUP_GUIDE.md").write_text("# Setup\n## S1\nBody text.\n", encoding="utf-8")
        (fake_root / "publish.json").write_text(json.dumps({"forbidden_patterns": ["internal-secret-token"]}), encoding="utf-8")
        (fake_root / "backend" / "app" / "core" / "config.py").write_text('app_version: str = "1.4.0"\n', encoding="utf-8")
        (fake_root / "frontend" / "package.json").write_text(json.dumps({"version": "1.5.0"}), encoding="utf-8")
        (fake_root / "frontend" / "package-lock.json").write_text(
            json.dumps({"version": "1.5.0", "packages": {"": {"version": "1.5.0"}}}), encoding="utf-8"
        )
        (dev_dir / "dev_q01.json").write_text(json.dumps({"header": {"app_version": "1.5.0"}, "events": []}), encoding="utf-8")

        try:
            verify_repo_release_alignment(fake_root)
        except DocsManifestError as exc:
            assert "1.4.0" in str(exc) and "1.5.0" in str(exc)
        else:
            raise AssertionError("Expected version drift between config.py and CHANGELOG.md to fail")

    assert MANIFEST_SCRIPT.is_file(), "Missing scripts/build_docs_manifest.py"
    with tempfile.TemporaryDirectory() as out_tmp:
        out_json = Path(out_tmp) / "docs_manifest.json"
        proc = subprocess.run(
            [sys.executable, str(MANIFEST_SCRIPT), "--check", "--out", str(out_json)],
            cwd=str(PROJECT_ROOT),
            env={"PYTHONPATH": str(PROJECT_ROOT / "backend"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        exported = json.loads(out_json.read_text(encoding="utf-8"))
        assert exported["app_version"] == ArenaSettings().app_version
        assert len(exported["releases"]) >= 6


def test_release_please_and_workflow_configs_are_valid_and_secret_free() -> None:
    rp_cfg_path = PROJECT_ROOT / "release-please-config.json"
    rp_man_path = PROJECT_ROOT / ".release-please-manifest.json"
    wf_path = PROJECT_ROOT / ".github" / "workflows" / "release-docs.yml"
    for p in (rp_cfg_path, rp_man_path, wf_path):
        assert p.is_file(), f"Missing {p.relative_to(PROJECT_ROOT)}"

    rp_cfg = json.loads(rp_cfg_path.read_text(encoding="utf-8"))
    rp_man = json.loads(rp_man_path.read_text(encoding="utf-8"))
    pkg = rp_cfg["packages"]["."]
    assert pkg["release-type"] == "simple"
    extra_paths = {entry["path"] for entry in pkg["extra-files"]}
    assert {
        "backend/app/core/config.py",
        "frontend/package.json",
        "frontend/package-lock.json",
        "data/recordings/_dev/level1_naive/gemini-4-argon__claude-opus-5-5/dev_q01.json",
    } <= extra_paths
    assert rp_man["."] == ArenaSettings().app_version

    wf_text = wf_path.read_text(encoding="utf-8")
    assert "build_docs_manifest.py --check" in wf_text
    assert "release-please-action@" in wf_text
    assert "permissions:" in wf_text
    assert "GEMINI_API_KEY" not in wf_text
