"""v1.1 step 2: ingestion jobs over an upload workspace (L1/L2) — downloaded from the bucket, scoped to upl_* rows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import ArenaSettings
from app.ingestion.jobs import IngestBackends, IngestJobRunner, UploadSource
from test_ingest_jobs_api import FakeEmbedder, FakeLoader

_CORPUS = Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
_PDF = _CORPUS / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf"
_WS = "upl_0123456789ab"


class ScopedLoader(FakeLoader):
    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[str | None] = []

    def load_rows(self, rows: list[dict[str, Any]], scope: str | None = None) -> int:  # type: ignore[override]
        self.scopes.append(scope)
        return super().load_rows(rows)

    def count_scope(self, tier: str, scope: str) -> int:
        return sum(1 for r in self.rows if r["tier_profile"] == tier and r["solicitation_id"] == scope)


def _settings() -> ArenaSettings:
    return ArenaSettings(demo_mode="live", gcp_project_id="test-project", ingest_job_timeout_seconds=120)


def _source(finished: list[Any], fetched: list[str], *, fail_fetch: bool = False) -> UploadSource:
    def fetch(object_name: str, dest: Path) -> int:
        if fail_fetch:
            raise RuntimeError("object vanished")
        fetched.append(object_name)
        dest.write_bytes(_PDF.read_bytes())
        return dest.stat().st_size

    return UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/f_0123456789ab-Pricing.pdf", "Pricing.pdf"),),
                        fetch=fetch, on_finish=finished.append)


async def _run(runner: IngestJobRunner, level: str, source: UploadSource) -> Any:
    job = runner.start(level, upload=source)
    events = [e async for e in runner.stream(job.job_id)]
    return job, events


def test_upload_job_downloads_parses_and_loads_only_its_scope() -> None:
    loader, finished, fetched = ScopedLoader(), [], []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, events = asyncio.run(_run(runner, "level1_naive", _source(finished, fetched)))
    assert job.status == "completed", job.error
    assert job.scope == _WS and fetched == [f"uploads/{_WS}/f_0123456789ab-Pricing.pdf"]
    discover = next(e for e in events if e["event"] == "stage_completed" and e["stage"] == "discover_docs")
    assert discover["payload"]["docs"] == 1 and discover["payload"]["doc_names"] == ["Pricing.pdf"]
    assert discover["payload"]["source"] == "upload workspace"
    assert loader.scopes == [_WS] and loader.rows and {r["solicitation_id"] for r in loader.rows} == {_WS}
    assert {r["doc_name"] for r in loader.rows} == {"Pricing.pdf"}
    verify = next(e for e in events if e["event"] == "stage_completed" and e["stage"] == "verify_counts")
    assert verify["payload"]["scope_rows"] == len(loader.rows) and verify["payload"]["scope"] == _WS
    assert [j.status for j in finished] == ["completed"]


def test_level2_upload_job_uses_hybrid_tier() -> None:
    loader, finished = ScopedLoader(), []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, _ = asyncio.run(_run(runner, "level2_hybrid", _source(finished, [])))
    assert job.status == "completed" and {r["tier_profile"] for r in loader.rows} == {"hybrid"}


def test_upload_job_failure_is_reported_and_callback_still_runs() -> None:
    loader, finished = ScopedLoader(), []
    runner = IngestJobRunner(_settings(), backend_factory=lambda s: IngestBackends(FakeEmbedder(), loader))
    job, events = asyncio.run(_run(runner, "level1_naive", _source(finished, [], fail_fetch=True)))
    assert job.status == "failed" and "object vanished" in (job.error or "")
    assert job.failed_stage == "discover_docs" and events[-1]["event"] == "job_failed"
    assert loader.rows == [] and [j.status for j in finished] == ["failed"]


def test_upload_levels_not_built_yet_are_refused_loudly() -> None:
    runner = IngestJobRunner(_settings())
    for level in ("level3_multimodal", "level4_agentic", "managed_vertex"):
        with pytest.raises(ValueError, match="not available for uploads yet"):
            runner.start(level, upload=_source([], []))


def test_upload_source_validates_inputs() -> None:
    with pytest.raises(ValueError):
        UploadSource(workspace_id="upl_bad", files=(("uploads/x/y.pdf", "y.pdf"),), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=(), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/a.pdf", "../a.pdf"),), fetch=lambda n, d: 0)
    with pytest.raises(ValueError):
        UploadSource(workspace_id=_WS, files=((f"uploads/{_WS}/a.pdf", "a.pdf"), (f"uploads/{_WS}/b.pdf", "a.pdf")),
                     fetch=lambda n, d: 0)
