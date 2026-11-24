"""Pure release history & documentation compiler for The RAG Arena (/releases).

Compiles ``CHANGELOG.md``, ``docs/ARCHITECTURE.md``, ``docs/SETUP_GUIDE.md``, and committed recording counts into a
typed manifest served by ``GET /api/v1/releases`` and validated offline by ``scripts/build_docs_manifest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from app.core.config import ALL_LEVEL_IDS, ArenaSettings
from app.engines.replay_engine import recording_store_for

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MAX_DOC_BYTES = 524_288
_MAX_DOC_LINES = 8_000
_MAX_RELEASES = 100
_MAX_TOC_ITEMS = 250

# Supports both Keep-a-Changelog ("## [1.4.0] — 2026-11-04") and release-please
# ("## [1.5.0](https://github.com/.../compare/v1.4.0...v1.5.0) (2026-11-16)" or "## 1.5.0 (2026-11-16)").
_RELEASE_HEADING_RE = re.compile(
    r"^##\s+\[?(?P<version>\d+\.\d+\.\d+(?:-[A-Za-z0-9._-]+)?)\]?"
    r"(?:\((?P<url>https://[^\s)]+)\))?"
    r"(?:\s*[—–-]\s*|\s+\()?(?P<date>\d{4}-\d{2}-\d{2})?\)?\s*$"
)
_SECTION_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
_TOC_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_TOP_BULLET_RE = re.compile(r"^[*-]\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
_NON_SLUG_RE = re.compile(r"[^a-z0-9_ -]")
_SPACE_HYPHEN_RE = re.compile(r"[\s-]+")
_CONFIG_VERSION_RE = re.compile(r'(?:DEFAULT_APP_VERSION|app_version):\s*str\s*=\s*"([^"]+)"')

_LEVEL_ROUTE_LABELS: dict[str, str] = {
    "managed_vertex": "1 · Vertex Search",
    "rag_engine": "2 · RAG Engine",
    "level1_naive": "3 · BQ Vector",
    "level2_hybrid": "4 · BQ Hybrid",
    "cloudsql_pgvector": "5 · Cloud SQL pgvector",
    "alloydb_scann": "6 · AlloyDB ScaNN",
    "vertex_vector_v2": "7 · Vector Search 2.0",
    "level3_multimodal": "8 · Multimodal",
    "bigquery_graphrag": "9 · BQ GraphRAG (GQL)",
    "level4_agentic": "10 · Agentic ADK",
}
_STATIC_ROUTE_TOKENS: tuple[tuple[str, str, str], ...] = (
    ("/workbench", "/workbench", "Workbench"),
    ("/compare", "/compare", "Compare"),
    ("/leaderboard", "/leaderboard", "Leaderboard"),
    ("/ingest", "/ingest", "Ingest"),
)


class DocsManifestError(ValueError):
    """Raised when documentation files are missing, outside the project boundary, malformed, or out of sync."""


@dataclass(frozen=True)
class ReleaseSection:
    """Categorized bullet group inside a release entry (e.g., Added, Changed, Features, Bug Fixes)."""

    heading: str
    items: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"heading": self.heading, "items": list(self.items)}


@dataclass(frozen=True)
class ReleaseEntry:
    """One parsed release block from CHANGELOG.md."""

    version: str
    date: str
    compare_url: str
    summary: str
    sections: tuple[ReleaseSection, ...]
    route_links: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "date": self.date,
            "compare_url": self.compare_url,
            "summary": self.summary,
            "sections": [s.to_dict() for s in self.sections],
            "route_links": [dict(link) for link in self.route_links],
        }


def slugify_heading(title: str) -> str:
    """Convert a Markdown heading title into a deterministic GitHub-style URL anchor slug."""
    if not isinstance(title, str) or not title.strip():
        raise DocsManifestError("Heading title must be a non-empty string")
    cleaned = title.strip().lower().replace("`", "")
    cleaned = _NON_SLUG_RE.sub("", cleaned)
    slug = _SPACE_HYPHEN_RE.sub("-", cleaned).strip("-")
    return slug or "section"


def parse_markdown_toc(markdown: str) -> list[dict[str, Any]]:
    """Extract H2/H3 table-of-contents entries outside fenced code blocks with deduplicated anchor slugs."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a str")
    lines = markdown.replace("\r", "").split("\n")
    if len(lines) > _MAX_DOC_LINES:
        raise DocsManifestError(f"Markdown exceeds line bound ({len(lines)} > {_MAX_DOC_LINES})")

    in_fence = False
    seen_counts: dict[str, int] = {}
    toc: list[dict[str, Any]] = []
    for line in lines:
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _TOC_HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        raw_title = match.group(2).strip()
        base = slugify_heading(raw_title)
        count = seen_counts.get(base, 0) + 1
        seen_counts[base] = count
        slug = base if count == 1 else f"{base}-{count}"
        toc.append({"id": slug, "level": level, "title": raw_title})
        if len(toc) >= _MAX_TOC_ITEMS:
            break
    return toc


def _extract_route_links(raw_block: str) -> tuple[dict[str, str], ...]:
    """Derive allowlisted in-app deep links referenced by a changelog block."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for token, path, label in _STATIC_ROUTE_TOKENS:
        if token in raw_block and path not in seen:
            seen.add(path)
            found.append({"path": path, "label": label})
    for level_id in ALL_LEVEL_IDS:
        path = f"/levels/{level_id}"
        if level_id in raw_block and path not in seen:
            seen.add(path)
            found.append({"path": path, "label": _LEVEL_ROUTE_LABELS.get(level_id, level_id)})
    return tuple(found)


def _finalize_release(
    meta: tuple[str, str, str],
    summary_lines: list[str],
    sections: list[tuple[str, list[str]]],
    raw_lines: list[str],
) -> ReleaseEntry:
    version, date, url = meta
    built_sections = tuple(
        ReleaseSection(heading=h, items=tuple(item.strip() for item in items if item.strip()))
        for h, items in sections
        if items
    )
    summary = " ".join(s.strip() for s in summary_lines if s.strip())
    return ReleaseEntry(
        version=version,
        date=date,
        compare_url=url,
        summary=summary,
        sections=built_sections,
        route_links=_extract_route_links("\n".join(raw_lines)),
    )


def _consume_release_line(
    stripped_right: str,
    summary_lines: list[str],
    sections: list[tuple[str, list[str]]],
) -> None:
    """Append one non-release line inside a changelog block into summary or active section items."""
    sec_match = _SECTION_HEADING_RE.match(stripped_right)
    if sec_match is not None:
        sections.append((sec_match.group(1), []))
        return
    if not sections:
        if stripped_right.strip():
            summary_lines.append(stripped_right.strip())
        return
    items = sections[-1][1]
    top_bullet = _TOP_BULLET_RE.match(stripped_right)
    if top_bullet is not None:
        items.append(top_bullet.group(1).strip())
    elif stripped_right.strip() and items:
        items[-1] = f"{items[-1]}\n{stripped_right}"


def parse_changelog(text: str) -> list[ReleaseEntry]:
    """Parse Keep-a-Changelog or release-please Markdown into ordered ReleaseEntry items."""
    if not isinstance(text, str) or not text.strip():
        raise DocsManifestError("CHANGELOG.md must be a non-empty string")
    lines = text.replace("\r", "").split("\n")
    if len(lines) > _MAX_DOC_LINES:
        raise DocsManifestError(f"CHANGELOG.md exceeds {_MAX_DOC_LINES} lines")

    entries: list[ReleaseEntry] = []
    current_meta: tuple[str, str, str] | None = None
    summary_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    raw_lines: list[str] = []

    for line in lines:
        stripped = line.rstrip()
        rel_match = _RELEASE_HEADING_RE.match(stripped)
        if rel_match is not None:
            if current_meta is not None:
                entries.append(_finalize_release(current_meta, summary_lines, sections, raw_lines))
            if len(entries) >= _MAX_RELEASES:
                break
            current_meta = (
                rel_match.group("version"),
                rel_match.group("date") or "",
                rel_match.group("url") or "",
            )
            summary_lines, sections, raw_lines = [], [], []
            continue
        if current_meta is not None:
            raw_lines.append(stripped)
            _consume_release_line(stripped, summary_lines, sections)

    if current_meta is not None and len(entries) < _MAX_RELEASES:
        entries.append(_finalize_release(current_meta, summary_lines, sections, raw_lines))
    if not entries:
        raise DocsManifestError("CHANGELOG.md contains no valid ## [x.y.z] release entries")
    return entries


def _read_bounded_repo_file(project_root: Path, rel_path: str) -> tuple[str, int]:
    """Read an allowlisted UTF-8 file strictly inside ``project_root`` (rejects symlink escapes and oversized files)."""
    if not isinstance(project_root, Path) or not rel_path:
        raise TypeError("project_root must be a Path and rel_path non-empty")
    root_resolved = project_root.resolve()
    candidate = root_resolved / rel_path
    if not candidate.exists():
        raise DocsManifestError(f"Required documentation file is missing: {rel_path}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
        raise DocsManifestError(f"Documentation path escapes repository boundary: {rel_path}")
    stat = resolved.stat()
    if stat.st_size > _MAX_DOC_BYTES:
        raise DocsManifestError(f"Documentation file {rel_path} exceeds {_MAX_DOC_BYTES} bytes")
    return resolved.read_text(encoding="utf-8"), stat.st_mtime_ns


_MANIFEST_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def build_releases_manifest(settings: ArenaSettings, *, project_root: Path | None = None) -> dict[str, Any]:
    """Compile the /releases API payload from repo Markdown sources and committed recording counts."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    root = _PROJECT_ROOT if project_root is None else project_root
    chg_text, chg_mtime = _read_bounded_repo_file(root, "CHANGELOG.md")
    arch_text, arch_mtime = _read_bounded_repo_file(root, "docs/ARCHITECTURE.md")
    setup_text, setup_mtime = _read_bounded_repo_file(root, "docs/SETUP_GUIDE.md")

    cache_key = (
        str(root.resolve()),
        chg_mtime,
        arch_mtime,
        setup_mtime,
        settings.app_version,
        settings.demo_mode,
        settings.enabled_levels,
        settings.recordings_dir,
    )
    cached = _MANIFEST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    releases = parse_changelog(chg_text)
    store = recording_store_for(settings)
    recordings = store.list_recordings(include_dev=False)
    by_engine: dict[str, int] = {}
    for rec in recordings:
        by_engine[rec.engine] = by_engine.get(rec.engine, 0) + 1

    payload: dict[str, Any] = {
        "app_version": settings.app_version,
        "demo_mode": settings.demo_mode,
        "stats": {
            "architecture_count": len(ALL_LEVEL_IDS),
            "enabled_levels_count": len(settings.enabled_levels),
            "total_recordings": len(recordings),
            "recordings_by_engine": by_engine,
            "release_count": len(releases),
        },
        "releases": [r.to_dict() for r in releases],
        "documents": [
            {
                "id": "architecture",
                "title": "System Architecture",
                "source_path": "docs/ARCHITECTURE.md",
                "toc": parse_markdown_toc(arch_text),
                "markdown": arch_text,
            },
            {
                "id": "setup_guide",
                "title": "Setup & Deployment Guide",
                "source_path": "docs/SETUP_GUIDE.md",
                "toc": parse_markdown_toc(setup_text),
                "markdown": setup_text,
            },
        ],
    }
    if len(_MANIFEST_CACHE) >= 8:
        _MANIFEST_CACHE.clear()
    _MANIFEST_CACHE[cache_key] = payload
    return payload


def verify_repo_release_alignment(project_root: Path | None = None) -> dict[str, Any]:
    """Verify 5-way version alignment (config.py, package.json, package-lock.json, dev_q01.json, CHANGELOG.md)."""
    root = (_PROJECT_ROOT if project_root is None else project_root).resolve()
    chg_text, _ = _read_bounded_repo_file(root, "CHANGELOG.md")
    releases = parse_changelog(chg_text)
    latest_changelog = releases[0].version

    cfg_text, _ = _read_bounded_repo_file(root, "backend/app/core/config.py")
    cfg_match = _CONFIG_VERSION_RE.search(cfg_text)
    if cfg_match is None:
        raise DocsManifestError("Could not locate app_version in backend/app/core/config.py")
    cfg_version = cfg_match.group(1)

    pkg_text, _ = _read_bounded_repo_file(root, "frontend/package.json")
    pkg_version = str(json.loads(pkg_text).get("version", ""))

    lock_text, _ = _read_bounded_repo_file(root, "frontend/package-lock.json")
    lock_json = json.loads(lock_text)
    lock_version = str(lock_json.get("version", ""))
    lock_root_pkg = str((lock_json.get("packages") or {}).get("", {}).get("version", ""))

    dev_rel = "data/recordings/_dev/level1_naive/gemini-4-argon__claude-opus-5-5/dev_q01.json"
    dev_text, _ = _read_bounded_repo_file(root, dev_rel)
    dev_version = str((json.loads(dev_text).get("header") or {}).get("app_version", ""))

    versions = {
        "CHANGELOG.md": latest_changelog,
        "backend/app/core/config.py": cfg_version,
        "frontend/package.json": pkg_version,
        "frontend/package-lock.json": lock_version,
        "frontend/package-lock.json[packages.'']": lock_root_pkg,
        dev_rel: dev_version,
    }
    if len(set(versions.values())) != 1:
        raise DocsManifestError(f"Release version drift detected across repository files: {versions}")
    return versions
