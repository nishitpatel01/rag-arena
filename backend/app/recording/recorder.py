"""Recorder: turns a finished live run (events + result + evaluation) into a provenance-stamped Recording."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

from app.core.config import ArenaSettings
from app.core.models import EngineResult, EvalOutcome
from app.core.telemetry import utc_now_iso
from app.recording.codec import eval_outcome_to_dict
from app.recording.redaction import assert_redacted, redact_value, redaction_tokens
from app.recording.store import RECORDING_FORMAT_VERSION, Recording, RecordingStore

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MAX_REF_DEPTH = 3
_REFTABLE_HEAD = "ref: refs/heads/.invalid"  # git writes this placeholder HEAD when refs live in a reftable
_GIT_TIMEOUT_SECONDS = 5
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_rev_parse(repo_root: Path) -> str:
    """Bounded ``git rev-parse HEAD`` for reftable repositories (binary ref storage); fails loud."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=True,
    )
    sha = proc.stdout.strip()
    if not _SHA_RE.match(sha):
        raise RuntimeError(f"git rev-parse HEAD returned an unexpected value: {sha[:80]!r}")
    return sha


def read_git_sha(repo_root: Path = _PROJECT_ROOT) -> str:
    """Resolve HEAD from ``.git`` with bounded reads; reftable repos use a bounded ``git rev-parse``.

    Returns ``"unknown"`` only when ``repo_root`` is not a git checkout.
    """
    head = repo_root / ".git" / "HEAD"
    if not head.is_file():
        return "unknown"
    ref = head.read_text(encoding="utf-8").strip()
    if ref == _REFTABLE_HEAD:
        return _git_rev_parse(repo_root)
    for _ in range(_MAX_REF_DEPTH):
        if not ref.startswith("ref:"):
            return ref[:40] or "unknown"
        ref_path = repo_root / ".git" / ref[4:].strip()
        if ref_path.is_file():
            ref = ref_path.read_text(encoding="utf-8").strip()
            continue
        packed = repo_root / ".git" / "packed-refs"
        if packed.is_file():
            needle = ref[4:].strip()
            for line in packed.read_text(encoding="utf-8").splitlines()[:5000]:
                if line.endswith(" " + needle):
                    return line.split(" ", 1)[0][:40]
        return "unknown"
    return "unknown"


def build_recording(
    *,
    settings: ArenaSettings,
    engine_id: str,
    question_id: str,
    query: str,
    run_id: str,
    events: tuple[dict[str, Any], ...],
    result: EngineResult,
    evaluation: EvalOutcome | None,
    synthetic_dev_fixture: bool = False,
) -> Recording:
    """Assemble a Recording with the spec P3 provenance header, redacted of every identity value (spec §10)."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if not isinstance(result, EngineResult):
        raise TypeError("result must be an EngineResult")
    if not isinstance(events, tuple) or not all(isinstance(e, dict) and "t_rel_ms" in e for e in events):
        raise ValueError("events must be a tuple of emitter event dicts carrying t_rel_ms")
    header: dict[str, Any] = {
        "format_version": RECORDING_FORMAT_VERSION,
        "recorded_at": utc_now_iso(),
        "project_id": "${GCP_PROJECT_ID}",
        "demo_mode": settings.demo_mode,
        "model_ids": {
            "generation": settings.gemini_fast_model,
            "reasoning": settings.gemini_pro_model,
            "embedding": settings.text_embedding_model,
            "judge": settings.judge_model,
            "judge_provider": settings.judge_provider,
        },
        "git_sha": read_git_sha(),
        "app_version": settings.app_version,
        "engine": engine_id,
        "question_id": question_id,
        "query": query,
        "run_id": run_id,
    }
    if synthetic_dev_fixture:
        header["synthetic_dev_fixture"] = True
        header["note"] = "SYNTHETIC development fixture generated with mocked SDK clients; not a measured live run."
    tokens = redaction_tokens(settings)
    doc = redact_value(
        {
            "header": header,
            "events": list(events),
            "result": result.to_dict(),
            "evaluation": eval_outcome_to_dict(evaluation) if evaluation is not None else None,
        },
        tokens,
    )
    assert_redacted(doc, tokens)
    return Recording(header=doc["header"], events=tuple(doc["events"]), result=doc["result"], evaluation=doc["evaluation"])


def record_run(store: RecordingStore, recording: Recording) -> Path:
    """Persist a Recording through the store (DEMO_MODE=record)."""
    if not isinstance(store, RecordingStore):
        raise TypeError("store must be a RecordingStore")
    return store.save(recording, dev=recording.is_dev_fixture)


__all__ = ["build_recording", "read_git_sha", "record_run"]
