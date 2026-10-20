"""Recording store: ``data/recordings/<engine>/<question_id>.json`` with a provenance header (spec P3/P6).

Layout of one recording document::

    {"header": {...provenance...}, "events": [stage events with t_rel_ms], "result": EngineResult, "evaluation": {...}}

``_dev/`` recordings are synthetic development fixtures (header ``synthetic_dev_fixture: true``) and are
refused unless ``ALLOW_DEV_FIXTURES=1``; they must never be mistaken for measured live runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
_REQUIRED_HEADER_KEYS: tuple[str, ...] = (
    "format_version",
    "recorded_at",
    "project_id",
    "demo_mode",
    "model_ids",
    "git_sha",
    "app_version",
    "engine",
    "question_id",
)
_MAX_RECORDING_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 2000
_MAX_LISTED_FILES = 2000
DEV_FIXTURES_DIRNAME = "_dev"
RECORDING_FORMAT_VERSION = 1


class RecordingError(RuntimeError):
    """Base class for recording store failures."""


class RecordingNotFound(RecordingError):
    """No recording exists for (engine, question_id)."""


class RecordingRefused(RecordingError):
    """A recording exists but is refused (synthetic dev fixture without ALLOW_DEV_FIXTURES=1)."""


class RecordingInvalid(RecordingError):
    """A recording file is malformed."""


@dataclass(frozen=True)
class Recording:
    """One recorded engine run."""

    header: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    result: dict[str, Any]
    evaluation: dict[str, Any] | None
    path: str = ""

    @property
    def engine(self) -> str:
        return str(self.header["engine"])

    @property
    def question_id(self) -> str:
        return str(self.header["question_id"])

    @property
    def is_dev_fixture(self) -> bool:
        return bool(self.header.get("synthetic_dev_fixture", False))

    def to_json_dict(self) -> dict[str, Any]:
        return {"header": self.header, "events": list(self.events), "result": self.result, "evaluation": self.evaluation}


def validate_recording_id(value: str, name: str) -> str:
    """Ensure engine/question ids are safe path components."""
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise ValueError(f"{name} must match {_SAFE_ID_RE.pattern}, got {value!r}")
    return value


def validate_recording_document(doc: Any, path: str = "") -> Recording:
    """Validate the JSON structure of a recording and return a ``Recording``."""
    if not isinstance(doc, dict):
        raise RecordingInvalid(f"Recording {path} must be a JSON object")
    header = doc.get("header")
    if not isinstance(header, dict):
        raise RecordingInvalid(f"Recording {path} has no header object")
    missing = [k for k in _REQUIRED_HEADER_KEYS if k not in header]
    if missing:
        raise RecordingInvalid(f"Recording {path} header is missing provenance keys {missing}")
    events = doc.get("events")
    if not isinstance(events, list) or len(events) > _MAX_EVENTS:
        raise RecordingInvalid(f"Recording {path} events must be a list of at most {_MAX_EVENTS} items")
    last_t = -1
    for idx, ev in enumerate(events):
        if not isinstance(ev, dict) or "event" not in ev or not isinstance(ev.get("t_rel_ms"), int):
            raise RecordingInvalid(f"Recording {path} event #{idx} must be an object with 'event' and int 't_rel_ms'")
        if ev["t_rel_ms"] < last_t:
            raise RecordingInvalid(f"Recording {path} event #{idx} has decreasing t_rel_ms")
        last_t = ev["t_rel_ms"]
    result = doc.get("result")
    if not isinstance(result, dict):
        raise RecordingInvalid(f"Recording {path} has no result object")
    evaluation = doc.get("evaluation")
    if evaluation is not None and not isinstance(evaluation, dict):
        raise RecordingInvalid(f"Recording {path} evaluation must be an object or null")
    return Recording(header=header, events=tuple(events), result=result, evaluation=evaluation, path=path)


class RecordingStore:
    """Filesystem-backed store of recordings under ``root``."""

    def __init__(self, root: Path, *, allow_dev_fixtures: bool = False) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a pathlib.Path")
        if not isinstance(allow_dev_fixtures, bool):
            raise TypeError("allow_dev_fixtures must be a boolean")
        self.root = root
        self.allow_dev_fixtures = allow_dev_fixtures

    def path_for(self, engine: str, question_id: str, *, dev: bool = False) -> Path:
        engine_id = validate_recording_id(engine, "engine")
        qid = validate_recording_id(question_id, "question_id")
        base = self.root / DEV_FIXTURES_DIRNAME if dev else self.root
        return base / engine_id / f"{qid}.json"

    def _read(self, path: Path) -> Recording:
        size = path.stat().st_size
        if size > _MAX_RECORDING_BYTES:
            raise RecordingInvalid(f"Recording {path} exceeds {_MAX_RECORDING_BYTES} bytes")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecordingInvalid(f"Recording {path} is not valid JSON: {exc}") from exc
        return validate_recording_document(doc, path=str(path))

    def load(self, engine: str, question_id: str) -> Recording:
        """Load a recording; dev fixtures are only consulted/accepted when ``allow_dev_fixtures`` is True."""
        primary = self.path_for(engine, question_id)
        if primary.is_file():
            rec = self._read(primary)
            if rec.is_dev_fixture and not self.allow_dev_fixtures:
                raise RecordingRefused(f"{primary} is a synthetic dev fixture; set ALLOW_DEV_FIXTURES=1 to replay it")
            return rec
        dev_path = self.path_for(engine, question_id, dev=True)
        if dev_path.is_file():
            if not self.allow_dev_fixtures:
                raise RecordingRefused(f"Only a synthetic dev fixture exists for {engine}/{question_id}; set ALLOW_DEV_FIXTURES=1")
            rec = self._read(dev_path)
            if not rec.is_dev_fixture:
                raise RecordingInvalid(f"{dev_path} lives under {DEV_FIXTURES_DIRNAME}/ but lacks synthetic_dev_fixture=true")
            return rec
        raise RecordingNotFound(f"No recording for question '{question_id}' and engine '{engine}' under {self.root}")

    def save(self, recording: Recording, *, dev: bool = False) -> Path:
        """Atomically write a recording (tmp file + ``os.replace``)."""
        if not isinstance(recording, Recording):
            raise TypeError("recording must be a Recording")
        if dev and not recording.is_dev_fixture:
            raise ValueError("dev recordings must set header.synthetic_dev_fixture = true")
        target = self.path_for(recording.engine, recording.question_id, dev=dev)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(recording.to_json_dict(), indent=2, sort_keys=False), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def list_recordings(self, *, include_dev: bool = False) -> list[Recording]:
        """Return all recordings (bounded), skipping ``_dev`` unless requested and allowed."""
        if not self.root.is_dir():
            return []
        files = sorted(self.root.rglob("*.json"))[:_MAX_LISTED_FILES]
        out: list[Recording] = []
        for path in files:
            is_dev = DEV_FIXTURES_DIRNAME in path.relative_to(self.root).parts
            if is_dev and not (include_dev and self.allow_dev_fixtures):
                continue
            out.append(self._read(path))
        return out


__all__ = [
    "DEV_FIXTURES_DIRNAME",
    "RECORDING_FORMAT_VERSION",
    "Recording",
    "RecordingError",
    "RecordingInvalid",
    "RecordingNotFound",
    "RecordingRefused",
    "RecordingStore",
    "validate_recording_document",
    "validate_recording_id",
]
