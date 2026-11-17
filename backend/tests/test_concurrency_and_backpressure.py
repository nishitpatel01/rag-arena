"""v1.6.0 Step 4 TDD: bounded battle SSE queue + disconnect cancellation, multi-subscriber ingest stream, and client locks."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from app.api.routes import BATTLE_SSE_QUEUE_MAXSIZE, _stream_battle_events
from app.core import gcp_clients
from app.core.models import EngineFlags, EngineResult, TokenUsage
from app.core.telemetry import StageEmitter
from app.engines.base import BaseRAGEngine
from app.ingestion.jobs import IngestBackends, IngestJob, IngestJobRunner
from fakes import live_settings


class _HangingEngine(BaseRAGEngine):
    engine_id = "level1_naive"
    engine_label = "Hanging Fake"
    tier_profile = "naive"
    stages = ("retrieve",)

    def __init__(self, settings: Any) -> None:
        super().__init__(settings=settings)
        self.started_event = asyncio.Event()
        self.cancelled_event = asyncio.Event()

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        handle = await emitter.started("retrieve", label="Hang test", service="Fake", request={})
        self.started_event.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            self.cancelled_event.set()
            raise
        await emitter.completed(handle, payload={})
        return EngineResult(
            engine_id=self.engine_id, engine_label=self.engine_label, answer="never",
            citations=[], retrieved_chunks=[], agent_steps=[], latency_ms=1, cost_usd=0.0,
            stage_log=(), usage=TokenUsage(model="gemini-4-argon"), flags=EngineFlags(),
        )


def test_battle_sse_queue_is_bounded_and_cancels_workers_on_client_disconnect() -> None:
    assert BATTLE_SSE_QUEUE_MAXSIZE == 256
    settings = live_settings()
    hanging = _HangingEngine(settings)

    async def _exercise() -> bool:
        gen = _stream_battle_events(
            query="What is CLIN-0004?",
            question_id=None,
            selected_engines=["level1_naive"],
            solicitation_filter=None,
            registry={"level1_naive": hanging},
            settings=settings,
        )
        first = await anext(gen)
        assert "run_started" in first
        second = await anext(gen)
        assert "stage_started" in second
        await asyncio.wait_for(hanging.started_event.wait(), timeout=2.0)
        # Simulate browser closing EventSource early -> generator aclose() MUST cancel worker task!
        await gen.aclose()
        await asyncio.wait_for(hanging.cancelled_event.wait(), timeout=2.0)
        return hanging.cancelled_event.is_set()

    assert asyncio.run(_exercise()) is True


def test_ingest_stream_delivers_all_events_to_concurrent_subscribers_without_clear_race() -> None:
    settings = live_settings(ingest_job_timeout_seconds=30)
    runner = IngestJobRunner(settings, backend_factory=lambda s: IngestBackends(None, None))  # type: ignore[arg-type]
    job = IngestJob(job_id="ing_multi001", level="level1_naive", tier_profile="naive", status="running")
    runner._jobs[job.job_id] = job

    async def _exercise() -> tuple[list[str], list[str]]:
        async def _consume() -> list[str]:
            return [ev["event"] for ev in [e async for e in runner.stream(job.job_id)]]

        sub1 = asyncio.create_task(_consume())
        sub2 = asyncio.create_task(_consume())
        await asyncio.sleep(0.02)
        assert len(job.subscribers) == 2

        for name in ("job_started", "stage_started", "stage_completed", "job_complete"):
            await runner._put(job, {"event": name})
            await asyncio.sleep(0.005)
        job.status = "completed"
        job.settled = True
        job.wake_subscribers()

        r1, r2 = await asyncio.wait_for(asyncio.gather(sub1, sub2), timeout=2.0)
        assert len(job.subscribers) == 0
        return r1, r2

    out1, out2 = asyncio.run(_exercise())
    assert out1 == ["job_started", "stage_started", "stage_completed", "job_complete"]
    assert out2 == out1


def test_gcp_clients_locks_vvs2_cache_and_refreshes_token_outside_global_lock() -> None:
    gcp_clients.clear_client_cache()
    settings = live_settings(gcp_project_id="proj-lock-test", gcp_location="us-central1")

    lock_held_during_refresh: list[bool] = []

    class _FakeCreds:
        def __init__(self) -> None:
            self.valid = False
            self.token = ""

        def refresh(self, _req: Any) -> None:
            # _CLIENT_LOCK is an RLock: inspect whether the current thread holds it via repr ("count=0" when released)!
            lock_held_during_refresh.append("count=0" not in repr(gcp_clients._CLIENT_LOCK))
            self.valid = True
            self.token = "fresh-adc-token"

    fake_creds = _FakeCreds()
    with patch("google.auth.default", return_value=(fake_creds, "proj-lock-test")):
        tok = gcp_clients.get_access_token(settings)
    assert tok == "fresh-adc-token"
    assert lock_held_during_refresh == [False], "credentials.refresh() held global _CLIENT_LOCK!"

    fake_match_cls = MagicMock(side_effect=lambda **kw: MagicMock(kw=kw))
    fake_aiplatform = MagicMock(MatchServiceClient=fake_match_cls)
    with patch.dict("sys.modules", {"google.cloud.aiplatform_v1": fake_aiplatform}):
        c1 = gcp_clients.get_vector_search_v2_client(settings)
        c2 = gcp_clients.get_vector_search_v2_client(settings)
    assert c1 is c2 and fake_match_cls.call_count == 1
