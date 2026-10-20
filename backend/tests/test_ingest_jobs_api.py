"""Ingestion jobs API (spec §6.3): real discover/parse/chunk on the local corpus, mocked embed/load/index/verify."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.ingestion.jobs import INGEST_STAGES, IngestBackends, IngestJobRunner, unavailable_backends
from app.main import create_app

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = _PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"


class FakeEmbedder:
    model = "gemini-embedding-2"

    def __init__(self) -> None:
        self.batches: list[int] = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[0.01, 0.02, 0.03] for _ in texts]


class FakeLoader:
    table_fqn = "test-project.rag_arena.document_chunks"

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.ingest_runs: list[dict[str, Any]] = []
        self.schema_calls = 0

    def ensure_schema(self) -> None:
        self.schema_calls += 1

    def load_rows(self, rows: list[dict[str, Any]]) -> int:
        self.rows.extend(rows)
        return len(rows)

    def build_indexes(self) -> dict[str, str]:
        return {"document_chunks_vec_idx": "ACTIVE", "document_chunks_search_idx": "ACTIVE"}

    def count_by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row["tier_profile"]] = counts.get(row["tier_profile"], 0) + 1
        return counts

    def index_states(self) -> dict[str, str]:
        return self.build_indexes()

    def write_ingest_run(self, row: dict[str, Any]) -> None:
        self.ingest_runs.append(row)


def _parse_sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def _small_corpus() -> Path:
    """Copy two PDFs from solicitation_01 into a temp corpus to keep the job fast."""
    src = _CORPUS / "solicitation_01_cloud_infrastructure"
    root = Path(tempfile.mkdtemp(prefix="rag_arena_corpus_")) / "corpus"
    dest = root / "solicitation_01_cloud_infrastructure"
    dest.mkdir(parents=True)
    for name in ("01_RFP_Base_Solicitation.pdf", "04_Pricing_Schedule_CLINs.pdf"):
        (dest / name).write_bytes((src / name).read_bytes())
    return root


def test_ingest_job_streams_all_seven_stages_with_progress_and_counts() -> None:
    embedder, loader = FakeEmbedder(), FakeLoader()
    settings = ArenaSettings(demo_mode="live", gcp_project_id="test-project", ingest_job_timeout_seconds=120)
    app = create_app(settings=settings, ingest_backend_factory=lambda s: IngestBackends(embedder, loader))
    app.state.ingest_runner = IngestJobRunner(
        settings, backend_factory=lambda s: IngestBackends(embedder, loader), corpus_dir=_small_corpus()
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/ingest/jobs", json={"level": "level1_naive"})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert created.json()["tier_profile"] == "naive"
        events = _parse_sse(client.get(f"/api/v1/ingest/jobs/{job_id}/stream").text)
        snapshot = client.get(f"/api/v1/ingest/jobs/{job_id}").json()
        status = client.get("/api/v1/ingest/status").json()

    kinds = [e["event"] for e in events]
    assert kinds[0] == "job_started" and kinds[-1] == "job_complete"
    assert events[0]["job_id"] == job_id and events[0]["level"] == "level1_naive" and events[0]["tier_profile"] == "naive"
    assert all(e["engine"] == "level1_naive" and e["job_id"] == job_id for e in events)
    started = [e["stage"] for e in events if e["event"] == "stage_started"]
    completed = [e["stage"] for e in events if e["event"] == "stage_completed"]
    assert started == list(INGEST_STAGES) and completed == list(INGEST_STAGES)
    assert "stage_failed" not in kinds
    doc_progress = [e for e in events if e["event"] == "doc_progress"]
    assert {e["doc_name"] for e in doc_progress} == {"01_RFP_Base_Solicitation.pdf", "04_Pricing_Schedule_CLINs.pdf"}
    assert all({"doc_name", "pages", "chunks"} <= set(e) for e in doc_progress)
    embed_progress = [e for e in events if e["event"] == "embed_progress"]
    assert embed_progress and embed_progress[-1]["done"] == embed_progress[-1]["total"] == len(loader.rows)
    assert all(isinstance(e["batch_ms"], int) for e in embed_progress)
    payloads = {e["stage"]: e["payload"] for e in events if e["event"] == "stage_completed"}
    assert payloads["discover_docs"]["docs"] == 2
    assert payloads["parse"]["pages"] >= 2 and payloads["parse"]["parser"] in ("pymupdf", "pdf_stream_regex")
    assert payloads["chunk"]["chunks"] == len(loader.rows) > 0 and payloads["chunk"]["tier_profile"] == "naive"
    assert payloads["embed"]["embedded"] == len(loader.rows) and payloads["embed"]["dimensions"] == 3
    assert payloads["load_bigquery"]["loaded"] == len(loader.rows)
    assert payloads["build_indexes"]["index_states"]["document_chunks_vec_idx"] == "ACTIVE"
    assert payloads["verify_counts"]["counts_by_tier"] == {"naive": len(loader.rows)}
    assert events[-1]["counts_by_tier"] == {"naive": len(loader.rows)}
    assert all(r["ingest_run_id"] == job_id and r["layout_source"] for r in loader.rows)
    assert loader.ingest_runs[0]["run_id"] == job_id and loader.ingest_runs[0]["status"] == "completed"
    assert loader.ingest_runs[0]["chunks"] == len(loader.rows) and len(loader.ingest_runs[0]["stage_log"]) == 7
    assert snapshot["status"] == "completed" and snapshot["counts_by_tier"] == {"naive": len(loader.rows)}
    assert status["available"] is True and status["counts_by_tier"] == {"naive": len(loader.rows)}
    assert status["index_states"]["document_chunks_search_idx"] == "ACTIVE"
    assert status["last_runs"][0] == {
        "run_id": job_id, "level": "level1_naive", "tier_profile": "naive",
        "finished_at": snapshot["finished_at"], "status": "completed", "chunks": len(loader.rows),
    }


def test_ingest_job_without_live_backends_fails_loud_at_embed_stage() -> None:
    settings = ArenaSettings(demo_mode="replay", ingest_job_timeout_seconds=120)
    app = create_app(settings=settings, ingest_backend_factory=unavailable_backends)
    app.state.ingest_runner = IngestJobRunner(settings, backend_factory=unavailable_backends, corpus_dir=_small_corpus())
    with TestClient(app) as client:
        job_id = client.post("/api/v1/ingest/jobs", json={"level": "level1_naive"}).json()["job_id"]
        events = _parse_sse(client.get(f"/api/v1/ingest/jobs/{job_id}/stream").text)
        status = client.get("/api/v1/ingest/status").json()
        assert client.post("/api/v1/ingest/jobs", json={"level": "vertex_vector_v2"}).status_code == 400
        assert client.get("/api/v1/ingest/jobs/nope/stream").status_code == 404
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "job_failed"
    failed = [e for e in events if e["event"] == "stage_failed"]
    assert len(failed) == 1 and failed[0]["stage"] == "embed" and failed[0]["error_type"] == "IngestBackendUnavailable"
    assert events[-1]["failed_stage"] == "embed" and "Phase 1" in events[-1]["error"]
    assert [e["stage"] for e in events if e["event"] == "stage_completed"] == ["discover_docs", "parse", "chunk"]
    assert status["available"] is False and status["counts_by_tier"] == {} and "reason" in status
    assert status["last_runs"][0]["status"] == "failed"
