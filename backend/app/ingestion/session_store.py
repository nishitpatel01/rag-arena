"""Bounded session store for user-uploaded PDF document chunks across RAG tiers with multi-worker persistence."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from app.core.models import BoundingBox, DocumentChunk

_MAX_SESSIONS = 50
_MAX_CHUNKS_PER_TIER = 200
_VALID_TIERS = ("naive", "hybrid", "multimodal")
_SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SESSION_DIR = Path(tempfile.gettempdir()) / "rag_arena_uploaded_sessions"

_UPLOADED_SESSIONS: dict[str, dict[str, list[DocumentChunk]]] = {}


def _canonical_sol_id(raw_id: str) -> str:
    """Convert a session_id or solicitation_id into a canonical 'upload_<prefix>' key."""
    cleaned = raw_id.strip()
    if cleaned.startswith("upload_"):
        return cleaned
    return f"upload_{cleaned[:8]}"


def _dict_to_chunk(raw: dict[str, Any], default_tier: str) -> DocumentChunk:
    """Deserialize a dictionary into a validated DocumentChunk."""
    bbox_raw = raw.get("bbox") if isinstance(raw.get("bbox"), dict) else {}
    page_num = int(raw.get("page_number", 1))
    bbox = BoundingBox(
        page=int(bbox_raw.get("page", page_num)),
        x0=float(bbox_raw.get("x0", 0.08)),
        y0=float(bbox_raw.get("y0", 0.20)),
        x1=float(bbox_raw.get("x1", 0.92)),
        y1=float(bbox_raw.get("y1", 0.60)),
    )
    return DocumentChunk(
        chunk_id=str(raw["chunk_id"]),
        tier_profile=str(raw.get("tier_profile", default_tier)),
        solicitation_id=str(raw.get("solicitation_id", "upload_session")),
        doc_name=str(raw.get("doc_name", "uploaded_document.pdf")),
        doc_type=str(raw.get("doc_type", "BASE_RFP")),
        element_type=str(raw.get("element_type", "narrative_text")),
        section_id=str(raw["section_id"]) if raw.get("section_id") else None,
        amendment_number=int(raw.get("amendment_number", 0)),
        effective_date=str(raw.get("effective_date", "2026-01-15")),
        amends_section=str(raw["amends_section"]) if raw.get("amends_section") else None,
        page_number=page_num,
        bbox=bbox,
        content=str(raw.get("content", "")),
        score=float(raw.get("score", 0.85)),
    )


def _persist_session_to_disk(sol_id: str, normalized: dict[str, list[DocumentChunk]]) -> None:
    """Write normalized session chunks to a bounded temp directory for multi-worker sharing."""
    if not _SAFE_KEY_RE.match(sol_id):
        return
    try:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        existing_files = sorted(_SESSION_DIR.glob("upload_*.json"), key=lambda p: p.stat().st_mtime)
        while len(existing_files) >= _MAX_SESSIONS:
            oldest_file = existing_files.pop(0)
            oldest_file.unlink(missing_ok=True)
        payload = {
            tier: [c.to_dict() for c in chunks[:_MAX_CHUNKS_PER_TIER]]
            for tier, chunks in normalized.items()
        }
        target_path = (_SESSION_DIR / f"{sol_id}.json").resolve()
        if target_path.parent == _SESSION_DIR.resolve():
            target_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _load_session_from_disk(sol_id: str) -> dict[str, list[DocumentChunk]] | None:
    """Load a persisted session from disk when a different worker process handles the query."""
    if not _SAFE_KEY_RE.match(sol_id):
        return None
    try:
        target_path = (_SESSION_DIR / f"{sol_id}.json").resolve()
        if target_path.parent != _SESSION_DIR.resolve() or not target_path.is_file():
            return None
        raw_data = json.loads(target_path.read_text(encoding="utf-8"))
        if not isinstance(raw_data, dict):
            return None
        loaded: dict[str, list[DocumentChunk]] = {}
        for tier in _VALID_TIERS:
            items = raw_data.get(tier, [])
            loaded[tier] = [
                _dict_to_chunk(item, tier)
                for item in items[:_MAX_CHUNKS_PER_TIER]
                if isinstance(item, dict) and "chunk_id" in item
            ]
        return loaded
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store_uploaded_session_chunks(
    session_id: str,
    chunks_by_tier: Mapping[str, list[DocumentChunk]],
) -> str:
    """Persist parsed tier chunks under a single canonical key and return solicitation_id."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(chunks_by_tier, Mapping) or len(chunks_by_tier) == 0:
        raise ValueError("chunks_by_tier must be a non-empty mapping of tier_profile to chunks")

    sol_id = _canonical_sol_id(session_id)

    normalized: dict[str, list[DocumentChunk]] = {}
    for tier in _VALID_TIERS:
        raw_list = chunks_by_tier.get(tier, [])
        if not isinstance(raw_list, list):
            raise TypeError(f"Chunks for tier '{tier}' must be a list")
        normalized[tier] = list(raw_list[:_MAX_CHUNKS_PER_TIER])

    if sol_id in _UPLOADED_SESSIONS:
        _UPLOADED_SESSIONS.pop(sol_id, None)
    while len(_UPLOADED_SESSIONS) >= _MAX_SESSIONS:
        oldest_key = next(iter(_UPLOADED_SESSIONS))
        _UPLOADED_SESSIONS.pop(oldest_key, None)

    _UPLOADED_SESSIONS[sol_id] = normalized
    _persist_session_to_disk(sol_id, normalized)
    return sol_id


def get_uploaded_session_chunks(
    session_or_solicitation_id: str,
    tier_profile: str = "multimodal",
) -> list[DocumentChunk]:
    """Retrieve stored DocumentChunks for an uploaded session or solicitation ID and tier."""
    if not isinstance(session_or_solicitation_id, str) or not session_or_solicitation_id.strip():
        return []
    if tier_profile not in _VALID_TIERS:
        raise ValueError(f"tier_profile must be one of {_VALID_TIERS}, got '{tier_profile}'")

    sol_id = _canonical_sol_id(session_or_solicitation_id)
    tier_map = _UPLOADED_SESSIONS.get(sol_id)
    if tier_map is None:
        tier_map = _load_session_from_disk(sol_id)
        if tier_map is not None:
            while len(_UPLOADED_SESSIONS) >= _MAX_SESSIONS:
                oldest_key = next(iter(_UPLOADED_SESSIONS))
                _UPLOADED_SESSIONS.pop(oldest_key, None)
            _UPLOADED_SESSIONS[sol_id] = tier_map
    if tier_map is None:
        return []
    return list(tier_map.get(tier_profile, []))


def clear_uploaded_sessions() -> None:
    """Clear all in-memory and disk-persisted uploaded session chunks for test isolation."""
    _UPLOADED_SESSIONS.clear()
    try:
        if _SESSION_DIR.exists():
            for path in _SESSION_DIR.glob("upload_*.json"):
                path.unlink(missing_ok=True)
    except OSError:
        pass
