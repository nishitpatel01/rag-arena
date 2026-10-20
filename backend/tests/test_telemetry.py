"""Tests for the v3 StageEmitter telemetry contract (spec §6.2 / §6.3)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.models import StageRecord
from app.core.telemetry import (
    NullStageEmitter,
    QueueStageEmitter,
    RecordingStageEmitter,
    StageHandle,
    StageTelemetryError,
    bound_payload,
)


def _run(coro):
    return asyncio.run(coro)


def test_queue_emitter_emits_started_and_completed_with_measured_duration() -> None:
    async def scenario() -> list[dict]:
        queue: asyncio.Queue = asyncio.Queue()
        emitter = QueueStageEmitter(queue, run_id="run_1", engine="level1_naive")
        handle = await emitter.started(
            "retrieve", label="Vector search", service="BigQuery", request={"sql": "SELECT 1"}
        )
        assert isinstance(handle, StageHandle)
        time.sleep(0.02)
        await emitter.completed(handle, payload={"rows_returned": 5})
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert emitter.records and isinstance(emitter.records[0], StageRecord)
        return items

    events = _run(scenario())
    assert [e["event"] for e in events] == ["stage_started", "stage_completed"]
    started, completed = events
    assert started["run_id"] == "run_1" and started["engine"] == "level1_naive"
    assert started["stage"] == "retrieve" and started["service"] == "BigQuery"
    assert started["request"] == {"sql": "SELECT 1"}
    assert started["ts"].endswith("Z") and "T" in started["ts"]
    assert completed["duration_ms"] >= 15
    assert completed["payload"] == {"rows_returned": 5}


def test_failed_stage_emits_error_type_and_message() -> None:
    async def scenario() -> list[dict]:
        emitter = RecordingStageEmitter(run_id="run_2", engine="level1_naive")
        handle = await emitter.started("embed_query", label="Embed", service="Vertex", request={})
        await emitter.failed(handle, error=RuntimeError("quota exceeded"))
        return list(emitter.events)

    events = _run(scenario())
    assert events[-1]["event"] == "stage_failed"
    assert events[-1]["error_type"] == "RuntimeError"
    assert events[-1]["error"] == "quota exceeded"
    assert events[-1]["duration_ms"] >= 0
    assert all("t_rel_ms" in e for e in events)


def test_skipped_stage_carries_reason() -> None:
    async def scenario() -> list[dict]:
        emitter = RecordingStageEmitter(run_id="run_3", engine="level2_hybrid")
        await emitter.skipped("rerank", reason="Ranking API disabled")
        return list(emitter.events)

    events = _run(scenario())
    assert events == [
        {
            "event": "stage_skipped",
            "run_id": "run_3",
            "engine": "level2_hybrid",
            "stage": "rerank",
            "reason": "Ranking API disabled",
            "ts": events[0]["ts"],
            "t_rel_ms": events[0]["t_rel_ms"],
        }
    ]


def test_completing_twice_or_unknown_handle_raises() -> None:
    async def scenario() -> None:
        emitter = NullStageEmitter(run_id="run_4", engine="level1_naive")
        handle = await emitter.started("query_prep", label="Prep", service="local", request={})
        await emitter.completed(handle, payload={})
        with pytest.raises(StageTelemetryError):
            await emitter.completed(handle, payload={})
        foreign = StageHandle(
            handle_id="nope", stage="retrieve", label="x", service="y", started_at="t", started_perf=0.0
        )
        with pytest.raises(StageTelemetryError):
            await emitter.completed(foreign, payload={})
        with pytest.raises(StageTelemetryError):
            await emitter.failed(foreign, error=ValueError("x"))

    _run(scenario())


def test_started_validates_inputs() -> None:
    async def scenario() -> None:
        emitter = NullStageEmitter(run_id="run_5", engine="level1_naive")
        with pytest.raises(ValueError):
            await emitter.started("", label="x", service="y", request={})
        with pytest.raises(TypeError):
            await emitter.started("retrieve", label="x", service="y", request="not-a-dict")  # type: ignore[arg-type]

    _run(scenario())


def test_payload_strings_over_8kb_are_truncated_with_marker() -> None:
    big = "x" * 10_000
    bounded = bound_payload({"sql": big, "nested": {"text": big, "n": 3}, "ok": "short"})
    assert len(bounded["sql"]) == 8192
    assert bounded["truncated"] is True
    assert bounded["nested"]["truncated"] is True
    assert len(bounded["nested"]["text"]) == 8192
    assert bounded["nested"]["n"] == 3
    assert bound_payload({"ok": "short"}) == {"ok": "short"}
    assert bound_payload({"xs": [big]})["xs"][0] == big[:8192]
