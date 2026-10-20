"""Stage telemetry contract (spec §6.2): engines emit, routes forward, nothing is synthesized.

Every stage event originates inside the engine at the moment the stage starts/ends and carries a
``duration_ms`` measured here with ``time.perf_counter``.  ``routes.py`` only forwards the dicts that
``QueueStageEmitter`` puts on its queue.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, AsyncIterator, Protocol
import uuid

from app.core.models import StageRecord

MAX_PAYLOAD_STRING_CHARS = 8192
_MAX_PAYLOAD_DEPTH = 6
_MAX_PAYLOAD_ITEMS = 200
_MAX_CAPTURED_EVENTS = 2000
_MAX_OPEN_HANDLES = 64
_STAGE_EVENT_TYPES: tuple[str, ...] = (
    "stage_started",
    "stage_completed",
    "stage_failed",
    "stage_skipped",
    "agent_turn",
)


class StageTelemetryError(RuntimeError):
    """Raised when the emitter contract is violated (double completion, unknown handle, bad input)."""


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision and a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bound_value(value: Any, depth: int) -> tuple[Any, bool]:
    """Recursively bound strings/collections; return (bounded_value, truncated_flag)."""
    if depth > _MAX_PAYLOAD_DEPTH:
        return "<max depth exceeded>", True
    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_STRING_CHARS:
            return value[:MAX_PAYLOAD_STRING_CHARS], True
        return value, False
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        truncated = False
        for idx, (key, item) in enumerate(value.items()):
            if idx >= _MAX_PAYLOAD_ITEMS:
                truncated = True
                break
            bounded, flag = _bound_value(item, depth + 1)
            out[str(key)] = bounded
            truncated = truncated or flag
        if truncated:
            out["truncated"] = True
        return out, truncated
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        truncated = len(value) > _MAX_PAYLOAD_ITEMS
        for item in list(value)[:_MAX_PAYLOAD_ITEMS]:
            bounded, flag = _bound_value(item, depth + 1)
            items.append(bounded)
            truncated = truncated or flag
        return items, truncated
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return str(value)[:MAX_PAYLOAD_STRING_CHARS], False


def bound_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of ``payload`` with strings > 8 KB truncated and a ``truncated: true`` marker."""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")
    bounded, _ = _bound_value(payload, 0)
    return bounded


@dataclass(frozen=True)
class StageHandle:
    """Opaque token returned by ``started`` and required by ``completed``/``failed``."""

    handle_id: str
    stage: str
    label: str
    service: str
    started_at: str
    started_perf: float


class StageEmitter(Protocol):
    """Spec §6.2 emitter contract. Engines call these; implementations decide where events go."""

    async def started(self, stage: str, *, label: str, service: str, request: dict) -> StageHandle: ...

    async def completed(self, handle: StageHandle, *, payload: dict) -> None: ...

    async def failed(self, handle: StageHandle, *, error: BaseException) -> None: ...

    async def skipped(self, stage: str, *, reason: str) -> None: ...


class BaseStageEmitter:
    """Shared emitter logic: handle bookkeeping, perf_counter durations, StageRecord log, event capture."""

    def __init__(self, *, run_id: str, engine: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(engine, str) or not engine.strip():
            raise ValueError("engine must be a non-empty string")
        self.run_id = run_id
        self.engine = engine
        self._open: dict[str, StageHandle] = {}
        self._records: list[StageRecord] = []
        self._events: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()

    @property
    def records(self) -> tuple[StageRecord, ...]:
        """StageRecords for every completed/failed/skipped stage, in emission order."""
        return tuple(self._records)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Captured event dicts (bounded), each with ``t_rel_ms`` relative to emitter creation."""
        return tuple(self._events)

    @property
    def open_stage(self) -> str | None:
        """The stage currently running (most recently started and not yet closed), if any."""
        if not self._open:
            return None
        return next(reversed(self._open.values())).stage

    def _base(self, event: str, **fields: Any) -> dict[str, Any]:
        return {"event": event, "run_id": self.run_id, "engine": self.engine, **fields, "ts": utc_now_iso()}

    async def _dispatch(self, event: dict[str, Any]) -> None:
        if len(self._events) < _MAX_CAPTURED_EVENTS:
            self._events.append({**event, "t_rel_ms": int((time.perf_counter() - self._t0) * 1000)})
        await self._emit(event)

    async def _emit(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    def _close(self, handle: StageHandle) -> int:
        if not isinstance(handle, StageHandle):
            raise StageTelemetryError("handle must be a StageHandle returned by started()")
        if handle.handle_id not in self._open:
            raise StageTelemetryError(
                f"Stage handle for '{handle.stage}' is unknown or already closed on this emitter"
            )
        del self._open[handle.handle_id]
        return max(0, int((time.perf_counter() - handle.started_perf) * 1000))

    async def started(self, stage: str, *, label: str, service: str, request: dict) -> StageHandle:
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be a non-empty string")
        if not isinstance(label, str) or not isinstance(service, str):
            raise TypeError("label and service must be strings")
        if not isinstance(request, dict):
            raise TypeError("request must be a dictionary")
        if len(self._open) >= _MAX_OPEN_HANDLES:
            raise StageTelemetryError(f"Too many open stages (> {_MAX_OPEN_HANDLES}); close stages before opening more")
        handle = StageHandle(
            handle_id=uuid.uuid4().hex,
            stage=stage,
            label=label,
            service=service,
            started_at=utc_now_iso(),
            started_perf=time.perf_counter(),
        )
        self._open[handle.handle_id] = handle
        await self._dispatch(
            self._base("stage_started", stage=stage, label=label, service=service, request=bound_payload(request))
        )
        return handle

    async def completed(self, handle: StageHandle, *, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        duration_ms = self._close(handle)
        bounded = bound_payload(payload)
        self._records.append(
            StageRecord(
                stage=handle.stage,
                label=handle.label,
                service=handle.service,
                status="completed",
                started_at=handle.started_at,
                duration_ms=duration_ms,
                payload=bounded,
            )
        )
        await self._dispatch(
            self._base("stage_completed", stage=handle.stage, duration_ms=duration_ms, payload=bounded)
        )

    async def failed(self, handle: StageHandle, *, error: BaseException) -> None:
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception instance")
        duration_ms = self._close(handle)
        error_type = type(error).__name__
        message = (str(error).strip() or error_type)[:MAX_PAYLOAD_STRING_CHARS]
        self._records.append(
            StageRecord(
                stage=handle.stage,
                label=handle.label,
                service=handle.service,
                status="failed",
                started_at=handle.started_at,
                duration_ms=duration_ms,
                error_type=error_type,
                error=message,
            )
        )
        await self._dispatch(
            self._base(
                "stage_failed", stage=handle.stage, duration_ms=duration_ms, error_type=error_type, error=message
            )
        )

    async def skipped(self, stage: str, *, reason: str) -> None:
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        self._records.append(
            StageRecord(
                stage=stage,
                label=stage,
                service="",
                status="skipped",
                started_at=utc_now_iso(),
                duration_ms=0,
                payload={"reason": reason},
            )
        )
        await self._dispatch(self._base("stage_skipped", stage=stage, reason=reason))

    async def agent_turn(self, **fields: Any) -> None:
        """Emit a Level 4 ``agent_turn`` event (spec §6.3); fields are bounded like payloads."""
        await self._dispatch(self._base("agent_turn", **bound_payload(dict(fields))))

    async def replay_recorded(self, event: dict[str, Any]) -> None:
        """Forward a recorded stage event verbatim (recorded ``duration_ms`` kept), stamped ``replayed: true``."""
        if not isinstance(event, dict) or event.get("event") not in _STAGE_EVENT_TYPES:
            raise StageTelemetryError(f"replay_recorded accepts only {_STAGE_EVENT_TYPES}, got {event!r}"[:400])
        forwarded = {k: v for k, v in event.items() if k not in ("run_id", "engine", "ts", "t_rel_ms")}
        forwarded.update(
            {"run_id": self.run_id, "engine": self.engine, "replayed": True, "recorded_ts": event.get("ts")}
        )
        record = _record_from_replayed(event)
        if record is not None:
            self._records.append(record)
        await self._dispatch({**forwarded, "ts": utc_now_iso()})


def _record_from_replayed(event: dict[str, Any]) -> StageRecord | None:
    """Build a StageRecord from a recorded completed/failed/skipped event (started events carry no record)."""
    kind = event.get("event")
    if kind == "stage_completed":
        return StageRecord(
            stage=str(event.get("stage", "")),
            label=str(event.get("label", event.get("stage", ""))),
            service=str(event.get("service", "")),
            status="completed",
            started_at=str(event.get("ts", "")),
            duration_ms=int(event.get("duration_ms", 0)),
            payload=dict(event.get("payload") or {}),
        )
    if kind == "stage_failed":
        return StageRecord(
            stage=str(event.get("stage", "")),
            label=str(event.get("label", event.get("stage", ""))),
            service=str(event.get("service", "")),
            status="failed",
            started_at=str(event.get("ts", "")),
            duration_ms=int(event.get("duration_ms", 0)),
            error_type=str(event.get("error_type", "")),
            error=str(event.get("error", "")),
        )
    if kind == "stage_skipped":
        return StageRecord(
            stage=str(event.get("stage", "")),
            label=str(event.get("stage", "")),
            service="",
            status="skipped",
            started_at=str(event.get("ts", "")),
            duration_ms=0,
            payload={"reason": str(event.get("reason", ""))},
        )
    return None


class QueueStageEmitter(BaseStageEmitter):
    """Emitter used by ``routes.py``: every event is put on an ``asyncio.Queue`` for SSE forwarding."""

    def __init__(self, queue: asyncio.Queue, *, run_id: str, engine: str) -> None:
        if not isinstance(queue, asyncio.Queue):
            raise TypeError("queue must be an asyncio.Queue")
        super().__init__(run_id=run_id, engine=engine)
        self._queue = queue

    async def _emit(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)


class StageScope:
    """Mutable scratch yielded by ``stage_scope``; the body fills ``payload`` before exit."""

    __slots__ = ("stage", "payload")

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.payload: dict[str, Any] = {}


@asynccontextmanager
async def stage_scope(
    emitter: StageEmitter, stage_id: str, *, label: str, service: str, request: dict[str, Any]
) -> AsyncIterator[StageScope]:
    """``started`` on entry, ``completed`` (with ``scope.payload``) on exit, ``failed`` + re-raise on error."""
    handle = await emitter.started(stage_id, label=label, service=service, request=request)
    scope = StageScope(stage_id)
    try:
        yield scope
    except BaseException as exc:
        await emitter.failed(handle, error=exc)
        raise
    await emitter.completed(handle, payload=scope.payload)


class RecordingStageEmitter(BaseStageEmitter):
    """Emitter that only captures events in memory (tests, recorder unit tests)."""

    async def _emit(self, event: dict[str, Any]) -> None:
        return None


class NullStageEmitter(BaseStageEmitter):
    """Emitter that keeps StageRecords but drops events (tests that only care about results)."""

    async def _dispatch(self, event: dict[str, Any]) -> None:
        return None

    async def _emit(self, event: dict[str, Any]) -> None:
        return None


__all__ = [
    "BaseStageEmitter",
    "MAX_PAYLOAD_STRING_CHARS",
    "NullStageEmitter",
    "QueueStageEmitter",
    "RecordingStageEmitter",
    "StageEmitter",
    "StageHandle",
    "StageRecord",
    "StageScope",
    "StageTelemetryError",
    "bound_payload",
    "stage_scope",
    "utc_now_iso",
]
