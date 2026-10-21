"""Replay engine (spec §6.4 ``replay``): streams recorded stage events with their original relative timing.

No credentials, no improvised answers. Recordings are keyed by (engine, synthesis model, judge model, question);
a missing one raises ``ReplayUnavailableError("No recording for question <qid> with synthesis=<s>, judge=<j> in
replay mode")``, which ``routes.py`` converts into ``engine_error``.
Recorded ``duration_ms`` values are forwarded verbatim via ``emitter.replay_recorded`` (never re-measured),
and every replayed frame carries ``replayed: true``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import ArenaSettings
from app.core.models import EngineResult, EvalOutcome
from app.core.models_catalog import RunModels, default_selection_ids
from app.core.telemetry import BaseStageEmitter, StageEmitter
from app.engines.base import BaseRAGEngine
from app.recording.codec import engine_result_from_dict, eval_outcome_from_dict
from app.recording.store import Recording, RecordingError, RecordingNotFound, RecordingStore

_MAX_SINGLE_SLEEP_SECONDS = 30.0
_MAX_TOTAL_SLEEP_SECONDS = 600.0
_EVALUATE_STAGE = "evaluate"
_REPLAYABLE_EVENTS: tuple[str, ...] = ("stage_started", "stage_completed", "stage_failed", "stage_skipped", "agent_turn")


class ReplayUnavailableError(RuntimeError):
    """Raised when replay cannot serve a question (missing/refused/invalid recording)."""


def recording_store_for(settings: ArenaSettings) -> RecordingStore:
    """Build the RecordingStore configured by settings (``RECORDINGS_DIR``, ``ALLOW_DEV_FIXTURES``)."""
    return RecordingStore(settings.recordings_path, allow_dev_fixtures=settings.allow_dev_fixtures)


def replay_pair(settings: ArenaSettings, models: RunModels | None) -> tuple[str, str]:
    """The (synthesis, judge) pair to replay: the run's selection, else the settings defaults."""
    return models.pair if models is not None else default_selection_ids(settings)


def load_recording_or_raise(
    store: RecordingStore, engine_id: str, question_id: str | None, *, pair: tuple[str, str]
) -> Recording:
    """Load a recording for ``pair``, mapping store failures to ``ReplayUnavailableError`` with the spec message."""
    if not question_id:
        raise ReplayUnavailableError("No recording for this question in replay mode (question_id missing)")
    try:
        return store.load(engine_id, question_id, pair=pair)
    except RecordingNotFound as exc:
        raise ReplayUnavailableError(
            f"No recording for question {question_id} with synthesis={pair[0]}, judge={pair[1]} in replay mode"
        ) from exc
    except (RecordingError, ValueError) as exc:
        raise ReplayUnavailableError(f"Recording unusable in replay mode: {exc}") from exc


async def replay_events(
    events: tuple[dict[str, Any], ...],
    emitter: StageEmitter,
    *,
    replay_speed: float,
) -> None:
    """Forward recorded events through ``emitter.replay_recorded`` honouring ``t_rel_ms`` × 1/replay_speed."""
    if not isinstance(emitter, BaseStageEmitter):
        raise TypeError("replay requires a BaseStageEmitter (supports replay_recorded)")
    if not isinstance(replay_speed, (int, float)) or replay_speed <= 0:
        raise ValueError("replay_speed must be > 0")
    prev_t = events[0]["t_rel_ms"] if events else 0
    slept = 0.0
    for event in events:
        if event.get("event") not in _REPLAYABLE_EVENTS:
            continue
        delta_s = max(0, int(event["t_rel_ms"]) - prev_t) / 1000.0 / float(replay_speed)
        prev_t = int(event["t_rel_ms"])
        wait = min(delta_s, _MAX_SINGLE_SLEEP_SECONDS, max(0.0, _MAX_TOTAL_SLEEP_SECONDS - slept))
        if wait > 0:
            await asyncio.sleep(wait)
            slept += wait
        await emitter.replay_recorded(event)


def split_engine_and_eval_events(
    events: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Separate engine-owned stage events from the ``evaluate`` stage events (owned by the evaluator)."""
    engine_events = tuple(e for e in events if e.get("stage") != _EVALUATE_STAGE)
    eval_events = tuple(e for e in events if e.get("stage") == _EVALUATE_STAGE)
    return engine_events, eval_events


class ReplayRAGEngine(BaseRAGEngine):
    """Plays back a recorded run for a given level; metadata (label, stages) mirrors the live engine class."""

    def __init__(
        self,
        *,
        engine_id: str,
        engine_label: str,
        stages: tuple[str, ...],
        tier_profile: str,
        settings: ArenaSettings,
        store: RecordingStore | None = None,
        models: RunModels | None = None,
    ) -> None:
        if not isinstance(engine_id, str) or not engine_id.strip():
            raise ValueError("engine_id must be a non-empty string")
        if models is not None and not isinstance(models, RunModels):
            raise TypeError("models must be a RunModels or None")
        self.engine_id = engine_id
        self.engine_label = engine_label
        self.stages = tuple(stages)
        self.tier_profile = tier_profile
        super().__init__(settings=settings)
        self.store = store if store is not None else recording_store_for(self.settings)
        self.pair = replay_pair(self.settings, models)

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Replay the engine-owned stage events of the recording and return the recorded EngineResult."""
        self.validate_query_inputs(query, solicitation_filter)
        recording = load_recording_or_raise(self.store, self.engine_id, question_id, pair=self.pair)
        engine_events, _ = split_engine_and_eval_events(recording.events)
        await replay_events(engine_events, emitter, replay_speed=self.settings.replay_speed)
        try:
            recorded = engine_result_from_dict(recording.result)
        except ValueError as exc:
            raise ReplayUnavailableError(f"Recorded result is malformed: {exc}") from exc
        return EngineResult(
            engine_id=recorded.engine_id,
            engine_label=recorded.engine_label,
            answer=recorded.answer,
            citations=recorded.citations,
            retrieved_chunks=recorded.retrieved_chunks,
            agent_steps=recorded.agent_steps,
            latency_ms=recorded.latency_ms,
            cost_usd=recorded.cost_usd,
            stage_log=self.stage_log_of(emitter),
            usage=recorded.usage,
            flags=recorded.flags,
        )


async def replay_evaluation(
    store: RecordingStore,
    *,
    engine_id: str,
    question_id: str | None,
    emitter: StageEmitter,
    replay_speed: float,
    pair: tuple[str, str],
) -> EvalOutcome:
    """Replay the recorded ``evaluate`` stage events and return the recorded judge outcome (never re-judged)."""
    recording = load_recording_or_raise(store, engine_id, question_id, pair=pair)
    if recording.evaluation is None:
        raise ReplayUnavailableError("Recording has no evaluation; the live run did not reach the judge")
    _, eval_events = split_engine_and_eval_events(recording.events)
    await replay_events(eval_events, emitter, replay_speed=replay_speed)
    try:
        return eval_outcome_from_dict(recording.evaluation)
    except ValueError as exc:
        raise ReplayUnavailableError(f"Recorded evaluation is malformed: {exc}") from exc


__all__ = [
    "ReplayRAGEngine",
    "ReplayUnavailableError",
    "load_recording_or_raise",
    "recording_store_for",
    "replay_evaluation",
    "replay_events",
    "replay_pair",
    "split_engine_and_eval_events",
]
