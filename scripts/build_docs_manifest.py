#!/usr/bin/env python3
"""Validate release-version alignment, compile the /releases manifest, and enforce zero-leak hygiene.

Usage:
    PYTHONPATH=backend python3 scripts/build_docs_manifest.py --check
    PYTHONPATH=backend python3 scripts/build_docs_manifest.py --check --out /tmp/docs_manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from app.core.config import ArenaSettings
from app.core.releases_docs import DocsManifestError, build_releases_manifest, verify_repo_release_alignment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_IDS = {"", "your-gcp-project-id"}


def _dotenv_tokens(root: Path) -> list[str]:
    dotenv_path = root / ".env"
    tokens: list[str] = []
    lines = dotenv_path.read_text(encoding="utf-8").splitlines()[:200] if dotenv_path.is_file() else []
    raw_map: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k, v = stripped.split("=", 1)
            raw_map[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("GCP_PROJECT_ID", "GCS_CORPUS_BUCKET", "UPLOAD_BUCKET"):
        val = raw_map.get(key) or os.environ.get(key, "")
        if val and val not in _EXAMPLE_IDS:
            tokens.append(val)
    scrub = raw_map.get("PUBLISH_SCRUB_TOKENS") or os.environ.get("PUBLISH_SCRUB_TOKENS", "")
    for pair in scrub.split(",")[:64]:
        if "=" in pair and pair.split("=", 1)[0].strip():
            tokens.append(pair.split("=", 1)[0].strip())
    return tokens


def _scan_for_leaks(serialized: str, root: Path) -> None:
    publish_path = root / "publish.json"
    forbidden: list[str] = []
    if publish_path.is_file():
        forbidden.extend(json.loads(publish_path.read_text(encoding="utf-8")).get("forbidden_patterns", []))
    forbidden.extend(["Cloudtop", "cloudtop", *_dotenv_tokens(root)])
    lowered = serialized.lower()
    for token in forbidden:
        if token and token.lower() in lowered:
            raise DocsManifestError(f"Forbidden pattern {token!r} detected in compiled releases/docs manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile and validate RAG Arena release documentation manifest.")
    parser.add_argument("--check", action="store_true", help="Verify 5-way version alignment and zero-leak manifest.")
    parser.add_argument("--out", type=str, default="", help="Optional output file path for compiled manifest JSON.")
    args = parser.parse_args(argv)

    if not args.check and not args.out:
        parser.error("Pass --check and/or --out PATH")

    try:
        versions = verify_repo_release_alignment(PROJECT_ROOT) if args.check else {}
        manifest = build_releases_manifest(ArenaSettings(demo_mode="replay"), project_root=PROJECT_ROOT)
        serialized = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        _scan_for_leaks(serialized, PROJECT_ROOT)
    except DocsManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(serialized, encoding="utf-8")

    summary = {
        "status": "ok",
        "app_version": manifest["app_version"],
        "releases": manifest["stats"]["release_count"],
        "documents": [d["id"] for d in manifest["documents"]],
        "total_recordings": manifest["stats"]["total_recordings"],
        "aligned_files": len(versions),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
