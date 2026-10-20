#!/usr/bin/env python3
"""Mirror & Scrub export pipeline for The RAG Arena public GitHub release."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

_MAX_FILES_TO_PROCESS = 2000
_TEXT_EXTENSIONS = {
    ".md",
    ".py",
    ".sh",
    ".json",
    ".toml",
    ".txt",
    ".ts",
    ".tsx",
    ".js",
    ".html",
    ".yml",
    ".yaml",
    ".tf",
    ".ini",
}


def _load_env_scrub_tokens() -> dict[str, str]:
    """Load identity-specific scrub tokens from PUBLISH_SCRUB_TOKENS ("old=new,old2=new2").

    Identity tokens (usernames, project ids, bucket names) are never stored in the repo; they
    come from the git-ignored .env / environment so the manifest itself stays leak-free.
    """
    raw = os.environ.get("PUBLISH_SCRUB_TOKENS", "").strip()
    if not raw:
        return {}
    tokens: dict[str, str] = {}
    for pair in raw.split(",")[:64]:
        if "=" not in pair:
            raise ValueError("PUBLISH_SCRUB_TOKENS entries must be 'old=new'")
        old_word, new_word = (part.strip() for part in pair.split("=", 1))
        if not old_word or not new_word:
            raise ValueError("PUBLISH_SCRUB_TOKENS entries must have non-empty old and new values")
        tokens[old_word] = new_word
    return tokens


def _should_prune(rel_posix: str, prune_paths: list[str]) -> bool:
    if not isinstance(rel_posix, str) or not isinstance(prune_paths, list):
        raise TypeError("rel_posix must be str and prune_paths must be list")
    parts = rel_posix.split("/")
    if any(
        p in {"__pycache__", ".pytest_cache", ".DS_Store", ".git", ".venv", "node_modules"}
        for p in parts
    ):
        return True
    for prune_rule in prune_paths:
        norm_rule = prune_rule.strip("/")
        if rel_posix == norm_rule or rel_posix.startswith(f"{norm_rule}/"):
            return True
    return False


def _scrub_and_verify_text(
    text: str,
    replacements: dict[str, str],
    forbidden_patterns: list[str],
    rel_posix: str,
) -> str:
    if not isinstance(text, str) or not isinstance(replacements, dict):
        raise TypeError("text must be str and replacements must be dict")
    scrubbed = text
    for old_word, new_word in replacements.items():
        scrubbed = scrubbed.replace(old_word, new_word)
    lower_scrubbed = scrubbed.lower()
    for pattern in forbidden_patterns:
        if pattern.lower() in lower_scrubbed:
            raise ValueError(
                f"Forbidden pattern '{pattern}' detected after scrub in '{rel_posix}'"
            )
    return scrubbed


def run_mirror_and_scrub(
    project_root: Path,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Execute Mirror & Scrub export according to publish.json."""
    if not isinstance(project_root, Path) or not project_root.exists():
        raise ValueError(f"Invalid project_root: {project_root}")

    manifest_path = project_root / "publish.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing publish.json at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prune_paths: list[str] = list(manifest.get("prune_paths", []))
    replacements: dict[str, str] = dict(manifest.get("keyword_replacements", {}))
    replacements.update(_load_env_scrub_tokens())
    forbidden_patterns: list[str] = list(manifest.get("forbidden_patterns", []))
    target_rel: str = str(
        manifest.get("target_path", "customer-work/public/2026/gcp-showcase/rag-arena")
    )

    dest_root = output_dir if output_dir is not None else (project_root / "dist" / target_rel)
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    pruned_items = 0

    for root_str, dirs, files in os.walk(project_root):
        root_path = Path(root_str)
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            rel_dir = (root_path / d).relative_to(project_root).as_posix()
            if _should_prune(rel_dir, prune_paths):
                pruned_items += 1
            else:
                kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in sorted(files):
            src_path = root_path / f
            rel_posix = src_path.relative_to(project_root).as_posix()
            if _should_prune(rel_posix, prune_paths):
                pruned_items += 1
                continue
            if copied_files >= _MAX_FILES_TO_PROCESS:
                raise RuntimeError(f"Exported file count exceeds bound {_MAX_FILES_TO_PROCESS}")

            dst_path = dest_root / rel_posix
            dst_path.parent.mkdir(parents=True, exist_ok=True)

            if src_path.suffix.lower() in _TEXT_EXTENSIONS or src_path.name == "Dockerfile":
                raw_text = src_path.read_text(encoding="utf-8")
                clean_text = _scrub_and_verify_text(
                    raw_text, replacements, forbidden_patterns, rel_posix
                )
                dst_path.write_text(clean_text, encoding="utf-8")
                shutil.copymode(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
            copied_files += 1

    return {
        "status": "exported",
        "destination": str(dest_root),
        "copied_files": copied_files,
        "pruned_items": pruned_items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Mirror & Scrub export")
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Override destination directory"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    summary = run_mirror_and_scrub(root, output_dir=args.output_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
