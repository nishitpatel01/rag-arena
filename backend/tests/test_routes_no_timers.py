"""Spec P1 lint-as-test: routes.py forwards engine telemetry and owns no stage timers or synthesized stages."""

from __future__ import annotations

import re
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "app" / "api" / "routes.py"
_FORBIDDEN_SUBSTRINGS = (
    "asyncio.sleep",
    "asyncio.wait(",
    "// 3",
    "_build_stage_event",
    "pipeline_stage",
    "get_engine_blueprint",
    "battlefield",
    "progress_pct",
)


def test_routes_source_contains_no_timers_or_stage_synthesis() -> None:
    source = _ROUTES.read_text(encoding="utf-8")
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in source, f"routes.py must not contain {needle!r} (spec P1)"
    # Stage events may only be *forwarded*; routes never builds a stage_started/completed dict itself.
    assert not re.search(r'"event":\s*"stage_(started|completed|failed|skipped)"', source)


def test_routes_emits_only_the_v3_envelope_events() -> None:
    source = _ROUTES.read_text(encoding="utf-8")
    emitted = set(re.findall(r'"event":\s*"([a-z_]+)"', source))
    assert emitted == {
        "run_started",
        "answer_complete",
        "eval_complete",
        "engine_error",
        "stream_warning",
        "battle_complete",
        "recording_saved",
    }
