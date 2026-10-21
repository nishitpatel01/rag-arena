"""Recording store: ``data/recordings/<engine>/<synthesis_model>__<judge_model>/<question_id>.json`` (spec P3/P6).

Recordings are keyed by the (synthesis, judge) model pair that produced them: replay can only play a pair that
was recorded live, and the leaderboard never averages across pairs. The pair is read from the header
(``synthesis_model``/``judge_model``, falling back to ``model_ids.generation``/``model_ids.judge`` for recordings
made before model selection existed) and must match the directory it was loaded from.

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
PAIR_SEPARATOR = "__"
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

    @property
    def pair(self) -> tuple[str, str]:
        """(synthesis_model, judge_model) that produced this recording."""
        ids = self.header.get("model_ids") if isinstance(self.header.get("model_ids"), dict) else {}
        synthesis = self.header.get("synthesis_model") or ids.get("generation")
        judge = self.header.get("judge_model") or ids.get("judge")
        if not isinstance(synthesis, str) or not synthesis or not isinstance(judge, str) or not judge:
            raise RecordingInvalid(f"Recording {self.path} header names no synthesis/judge model pair")
        return synthesis, judge

    def to_json_dict(self) -> dict[str, Any]:
        return {"header": self.header, "events": list(self.events), "result": self.result, "evaluation": self.evaluation}


def validate_recording_id(value: str, name: str) -> str:
    """Ensure engine/question ids are safe path components."""
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise ValueError(f"{name} must match {_SAFE_ID_RE.pattern}, got {value!r}")
    return value


def pair_dirname(pair: tuple[str, str]) -> str:
    """``(synthesis, judge)`` → ``<synthesis>__<judge>`` (both validated as safe path components)."""
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise ValueError(f"pair must be a (synthesis_model, judge_model) tuple, got {pair!r}")
    synthesis = validate_recording_id(pair[0], "synthesis_model")
    judge = validate_recording_id(pair[1], "judge_model")
    if PAIR_SEPARATOR in synthesis or PAIR_SEPARATOR in judge:
        raise ValueError(f"model ids must not contain '{PAIR_SEPARATOR}'")
    return f"{synthesis}{PAIR_SEPARATOR}{judge}"


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

    def path_for(self, engine: str, question_id: str, *, pair: tuple[str, str], dev: bool = False) -> Path:
        engine_id = validate_recording_id(engine, "engine")
        qid = validate_recording_id(question_id, "question_id")
        base = self.root / DEV_FIXTURES_DIRNAME if dev else self.root
        return base / engine_id / pair_dirname(pair) / f"{qid}.json"

    def _read(self, path: Path) -> Recording:
        size = path.stat().st_size
        if size > _MAX_RECORDING_BYTES:
            raise RecordingInvalid(f"Recording {path} exceeds {_MAX_RECORDING_BYTES} bytes")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecordingInvalid(f"Recording {path} is not valid JSON: {exc}") from exc
        return validate_recording_document(doc, path=str(path))

    def _read_pair(self, path: Path, pair: tuple[str, str]) -> Recording:
        rec = self._read(path)
        if rec.pair != pair:
            raise RecordingInvalid(f"Recording {path} was produced by {rec.pair}, not the requested pair {pair}")
        return rec

    def load(self, engine: str, question_id: str, *, pair: tuple[str, str]) -> Recording:
        """Load the recording for (engine, pair, question); dev fixtures only when ``allow_dev_fixtures`` is True."""
        primary = self.path_for(engine, question_id, pair=pair)
        if primary.is_file():
            rec = self._read_pair(primary, pair)
            if rec.is_dev_fixture and not self.allow_dev_fixtures:
                raise RecordingRefused(f"{primary} is a synthetic dev fixture; set ALLOW_DEV_FIXTURES=1 to replay it")
            return rec
        dev_path = self.path_for(engine, question_id, pair=pair, dev=True)
        if dev_path.is_file():
            if not self.allow_dev_fixtures:
                raise RecordingRefused(f"Only a synthetic dev fixture exists for {engine}/{question_id}; set ALLOW_DEV_FIXTURES=1")
            rec = self._read_pair(dev_path, pair)
            if not rec.is_dev_fixture:
                raise RecordingInvalid(f"{dev_path} lives under {DEV_FIXTURES_DIRNAME}/ but lacks synthetic_dev_fixture=true")
            return rec
        raise RecordingNotFound(
            f"No recording for question '{question_id}' with synthesis={pair[0]}, judge={pair[1]} and engine '{engine}'"
        )

    def save(self, recording: Recording, *, dev: bool = False) -> Path:
        """Atomically write a recording (tmp file + ``os.replace``) under its own (synthesis, judge) pair."""
        if not isinstance(recording, Recording):
            raise TypeError("recording must be a Recording")
        if dev and not recording.is_dev_fixture:
            raise ValueError("dev recordings must set header.synthetic_dev_fixture = true")
        target = self.path_for(recording.engine, recording.question_id, pair=recording.pair, dev=dev)
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

    def recorded_pairs(self) -> list[dict[str, Any]]:
        """``[{engine, synthesis_model, judge_model, question_ids}]`` — what replay can play (dev only if allowed)."""
        grouped: dict[tuple[str, str, str], set[str]] = {}
        for rec in self.list_recordings(include_dev=self.allow_dev_fixtures):
            synthesis, judge = rec.pair
            grouped.setdefault((rec.engine, synthesis, judge), set()).add(rec.question_id)
        return [
            {"engine": engine, "synthesis_model": synthesis, "judge_model": judge, "question_ids": sorted(qids)}
            for (engine, synthesis, judge), qids in sorted(grouped.items())
        ]


__all__ = [
    "DEV_FIXTURES_DIRNAME",
    "PAIR_SEPARATOR",
    "RECORDING_FORMAT_VERSION",
    "Recording",
    "RecordingError",
    "RecordingInvalid",
    "RecordingNotFound",
    "RecordingRefused",
    "RecordingStore",
    "pair_dirname",
    "validate_recording_document",
    "validate_recording_id",
]
