"""v1.1 step 6: security & isolation test suite across upload validation, IDOR, Origin, SQL scope, L4 tools, and redaction."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import patch

from google.adk.agents import LlmAgent
from google.genai import types
import pymupdf
import pytest
from starlette.testclient import TestClient

from app.core.models import BoundingBox, Citation, EngineFlags, EngineResult, EvalMetrics, EvalOutcome, TokenUsage
from app.core.models_catalog import load_catalog
from app.core.telemetry import RecordingStageEmitter
from app.engines.agent_ledger import AgentLedger
from app.engines.agent_runtime import AGENT_INSTRUCTION, AGENT_NAME
from app.engines.agent_tools import ToolArgumentError, ToolDeps, validate_amendment_args, validate_search_args
from app.engines.level1_naive import build_vector_search_sql
from app.engines.level3_multimodal import Level3MultimodalEngine
from app.engines.level4_agentic import Level4AgenticEngine
from app.ingestion.bq_indexer import build_hybrid_rrf_sql
from app.ingestion.live_backends import BigQueryLiveLoader
from app.ingestion.uploads import PdfRejected, sanitize_filename, validate_pdf_file
from app.main import create_app
from app.recording.recorder import build_recording
from app.recording.redaction import assert_redacted, redaction_tokens
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run
from test_level4_agentic import StubLlm, _call, _ranking, _RoutedBQ
from test_uploads_api import _ALICE, _BOB, _ingest_client, _pdf, _upload, _workspace

_MALLORY = {"X-Goog-Authenticated-User-Email": "accounts.google.com:mallory@example.com"}
_WS = "upl_0123456789ab"
_OTHER_WS = "upl_ffffffffffff"


def _launch_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Procurement pricing schedule")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<< /Type /Action /S /Launch /F (cmd.exe) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
    return doc.tobytes()


def test_pdf_launch_action_and_traversal_filenames_are_rejected() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rag_arena_sec_"))
    launch_path = tmp / "launch.pdf"
    launch_path.write_bytes(_launch_pdf())
    with pytest.raises(PdfRejected, match="launch actions"):
        validate_pdf_file(launch_path)

    for raw, expected in (
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\Windows\\System32\\evil.pdf", "evil.pdf"),
        ("..%2f..%2fsecret report.PDF", "_2f._2fsecret_report.pdf"),
    ):
        clean = sanitize_filename(raw)
        assert clean == expected and "/" not in clean and "\\" not in clean and ".." not in clean
    with pytest.raises(ValueError, match="control characters"):
        sanitize_filename("nul\x00byte.pdf")

    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client, _ALICE)
        _, fin = _upload(client, store, ws, _launch_pdf(), name="Launch.pdf", headers=_ALICE)
        assert fin.status_code == 422 and "launch actions" in fin.json()["error"]
        assert not any(k.endswith(".pdf") for k in store.objects)


def test_origin_binding_blocks_cross_origin_and_blocks_localhost_on_cloud_run() -> None:
    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client, _ALICE)
        cross = client.post(
            f"/api/v1/uploads/{ws}/files",
            headers={**_ALICE, "Origin": "https://attacker.example"},
            json={"filename": "a.pdf", "size_bytes": 10},
        )
        assert cross.status_code == 400 and "origin" in cross.json()["error"].lower()

        local_dev = client.post(
            f"/api/v1/uploads/{ws}/files",
            headers={**_ALICE, "Origin": "http://localhost:5173"},
            json={"filename": "dev.pdf", "size_bytes": 10},
        )
        assert local_dev.status_code == 201 and store.sessions[-1][3] == "http://localhost:5173"

        with patch.dict(os.environ, {"K_SERVICE": "rag-arena"}):
            on_cloudrun = client.post(
                f"/api/v1/uploads/{ws}/files",
                headers={**_ALICE, "Host": "arena.example.com", "Origin": "http://localhost:5173"},
                json={"filename": "prod_local.pdf", "size_bytes": 10},
            )
            assert on_cloudrun.status_code == 400 and "origin" in on_cloudrun.json()["error"].lower()
            same_host = client.post(
                f"/api/v1/uploads/{ws}/files",
                headers={**_ALICE, "Host": "arena.example.com", "Origin": "https://arena.example.com"},
                json={"filename": "prod_ok.pdf", "size_bytes": 10},
            )
            assert same_host.status_code == 201 and store.sessions[-1][3] == "https://arena.example.com"


def test_cross_owner_idor_matrix_returns_404_across_every_surface() -> None:
    client, store, _ = _ingest_client()
    with client:
        ws = _workspace(client, _ALICE)
        fid, fin = _upload(client, store, ws, _pdf(pages=1), name="Private.pdf", headers=_ALICE)
        assert fin.status_code == 200
        ing = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "level1_naive"})
        assert ing.status_code == 202
        jid = ing.json()["job"]["job_id"]

        assert client.get("/api/v1/uploads", headers=_MALLORY).json()["workspaces"] == []
        for method, path, body in (
            ("GET", f"/api/v1/uploads/{ws}", None),
            ("DELETE", f"/api/v1/uploads/{ws}", None),
            ("POST", f"/api/v1/uploads/{ws}/files", {"filename": "x.pdf", "size_bytes": 1}),
            ("POST", f"/api/v1/uploads/{ws}/files/{fid}/finalize", None),
            ("GET", f"/api/v1/uploads/{ws}/estimate?level=level1_naive", None),
            ("POST", f"/api/v1/uploads/{ws}/ingest", {"level": "level1_naive"}),
            ("GET", f"/api/v1/ingest/jobs/{jid}", None),
            ("GET", f"/api/v1/ingest/jobs/{jid}/stream", None),
            ("GET", f"/api/v1/documents/page?solicitation_id={ws}&doc_name=Private.pdf&page=1", None),
            ("GET", f"/api/v1/documents/highlights?solicitation_id={ws}&doc_name=Private.pdf&page=1&text=page", None),
            ("POST", "/api/v1/battle/stream", {"query": "price?", "engines": ["level1_naive"], "solicitation_filter": ws}),
        ):
            resp = client.request(method, path, headers=_MALLORY, json=body)
            assert resp.status_code == 404, (method, path, resp.status_code, resp.text)

        # Anonymous caller (missing IAP header) also cannot touch Alice's IAP-owned workspace
        assert client.get(f"/api/v1/uploads/{ws}").status_code == 404
        assert client.delete(f"/api/v1/uploads/{ws}").status_code == 404
        assert client.get(f"/api/v1/uploads/{ws}", headers=_ALICE).status_code == 200


def test_replay_mode_returns_404_on_every_upload_and_upload_document_route() -> None:
    app = create_app(settings=live_settings(demo_mode="replay", gcp_project_id="secret-p", upload_bucket="secret-u"))
    client = TestClient(app)
    health = client.get("/api/v1/health").json()
    assert health["project_id"] == "" and health["gcs_bucket"] == "" and health["upload_bucket"] == ""

    for method, path, body in (
        ("GET", "/api/v1/uploads/bucket", None),
        ("POST", "/api/v1/uploads/bucket", {}),
        ("GET", "/api/v1/uploads", None),
        ("POST", "/api/v1/uploads", {}),
        ("GET", f"/api/v1/uploads/{_WS}", None),
        ("DELETE", f"/api/v1/uploads/{_WS}", None),
        ("POST", f"/api/v1/uploads/{_WS}/files", {"filename": "a.pdf", "size_bytes": 1}),
        ("POST", f"/api/v1/uploads/{_WS}/files/f_0123456789ab/finalize", None),
        ("GET", f"/api/v1/uploads/{_WS}/estimate?level=level1_naive", None),
        ("POST", f"/api/v1/uploads/{_WS}/ingest", {"level": "level1_naive"}),
        ("GET", f"/api/v1/documents/page?solicitation_id={_WS}&doc_name=a.pdf&page=1", None),
        ("GET", f"/api/v1/documents/highlights?solicitation_id={_WS}&doc_name=a.pdf&page=1&text=hi", None),
        ("POST", "/api/v1/battle/stream", {"query": "q?", "solicitation_filter": _WS}),
    ):
        resp = client.request(method, path, headers=_ALICE, json=body)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)


def test_unscoped_corpus_queries_and_tier_counts_exclude_upload_workspaces() -> None:
    vec_corpus = build_vector_search_sql("demo-proj", "rag_arena", "document_chunks_v3", include_solicitation_filter=False)
    assert "NOT STARTS_WITH(solicitation_id, 'upl_')" in vec_corpus
    vec_scoped = build_vector_search_sql("demo-proj", "rag_arena", "document_chunks_v3", include_solicitation_filter=True)
    assert "solicitation_id = @solicitation_id" in vec_scoped and "NOT STARTS_WITH" not in vec_scoped

    rrf_corpus = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", include_solicitation_filter=False)
    assert rrf_corpus.count("NOT STARTS_WITH(solicitation_id, 'upl_')") == 2
    rrf_scoped = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", include_solicitation_filter=True)
    assert rrf_scoped.count("solicitation_id = @solicitation_id") == 2 and "NOT STARTS_WITH" not in rrf_scoped

    captured: list[str] = []

    class _CountingBQ:
        def query(self, sql: str, job_config: Any = None) -> Any:
            captured.append(sql)
            from types import SimpleNamespace

            return SimpleNamespace(result=lambda timeout=None: [SimpleNamespace(tier_profile="naive", n=5067)])

    loader = BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=_CountingBQ())
    assert loader.count_by_tier() == {"naive": 5067}
    assert captured and "NOT STARTS_WITH(solicitation_id, 'upl_')" in captured[0]


def test_level4_agent_tools_pin_upload_scope_and_reject_cross_scope_prompt_injection() -> None:
    # Corpus mode: upl_* is rejected in tool arguments
    with pytest.raises(ToolArgumentError):
        validate_search_args("rate", _WS, None)
    with pytest.raises(ToolArgumentError):
        validate_amendment_args(_WS, "CLIN-0001")

    # Upload mode: empty or matching solicitation_id pins to upload_scope; cross-scope fails loud
    assert validate_search_args("rate", "", None, upload_scope=_WS) == ("rate", _WS, ())
    assert validate_search_args("rate", _WS, ["table"], upload_scope=_WS) == ("rate", _WS, ("table",))
    for hostile in ("solicitation_01", _OTHER_WS):
        with pytest.raises(ToolArgumentError, match="scoped to upload workspace"):
            validate_search_args("rate", hostile, None, upload_scope=_WS)
        with pytest.raises(ToolArgumentError, match="scoped to upload workspace"):
            validate_amendment_args(hostile, "CLIN-0001", upload_scope=_WS)
    assert validate_amendment_args("", "CLIN-0001", upload_scope=_WS) == (_WS, "CLIN-0001")
    assert validate_amendment_args(_WS, "CLIN-0001", upload_scope=_WS) == (_WS, "CLIN-0001")


def test_level4_engine_runs_on_upload_workspace_and_blocks_injected_cross_scope_tool_call() -> None:
    upl_chunk_id = f"{_WS}_p1_t1_multimodal"
    upl_row = {
        "chunk_id": upl_chunk_id, "solicitation_id": _WS, "doc_name": "Uploaded_Pricing.pdf",
        "doc_type": "UPLOADED_DOCUMENT", "element_type": "table", "section_id": "CLIN-7701",
        "amendment_number": 0, "effective_date": None, "amends_section": None, "page_number": 1,
        "bbox": {"page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.3},
        "content": "| CLIN | Monthly |\n| CLIN-7701 | $14,500.00 |",
        "layout_source": "documentai_layout", "bbox_source": "pdf_text_layer",
        "vector_rank": 1, "vector_distance": 0.08, "lexical_rank": 1, "lexical_score": 3, "rrf_score": 0.032787,
    }
    draft = "CLIN-7701 is $14,500.00 per month (12-month total $174,000.00)."
    script = [
        # Turn 1 simulates prompt-injected attempt to query solicitation_01 -> blocked with tool error
        [_call("search_procurement_corpus", {"query": "CLIN-7701", "solicitation_id": "solicitation_01"}, "fc_inj")],
        # Turn 2 searches with empty solicitation_id -> pinned to _WS
        [_call("search_procurement_corpus", {"query": "CLIN-7701", "solicitation_id": ""}, "fc_ok"),
         _call("calculate_contract_pricing", {"expression": "monthly * months", "variables": {"monthly": 14500, "months": 12}}, "fc_calc")],
        [_call("verify_citations", {"draft_answer": draft, "cited_chunk_ids": [upl_chunk_id]}, "fc_ver")],
        [types.Part(text=f"{draft}\nCITED_CHUNK_IDS: {upl_chunk_id}")],
    ]
    llm = StubLlm(script=script)
    settings = live_settings(enabled_levels=("level4_agentic",))
    spec = load_catalog(settings).get("gemini-4-argon")
    emitter = RecordingStageEmitter(run_id="run_l4_upl", engine="level4_agentic")
    genai, bq = FakeGenaiClient(), FakeBigQueryClient(rows=[upl_row])

    def build_stub(s: Any, cfg: Any, tools: list[Any]) -> LlmAgent:
        return LlmAgent(name=AGENT_NAME, model=llm, instruction=AGENT_INSTRUCTION, tools=tools)

    with fake_clients(genai, bq), patch("app.engines.level4_agentic.get_genai_client", lambda s, location=None: genai), \
            patch("app.engines.level4_agentic.get_bigquery_client", lambda s: bq), \
            patch("app.engines.level4_agentic.build_agent", build_stub):
        retriever = Level3MultimodalEngine(settings=settings, ranking_client=_ranking())
        engine = Level4AgenticEngine(settings=settings, synthesis_model=spec, retriever=retriever)
        # Corpus solicitation_filter is still refused
        with pytest.raises(ValueError, match="solicitation"):
            run(engine.run_query("q?", question_id=None, solicitation_filter="solicitation_01", emitter=emitter))
        result = run(engine.run_query("What is CLIN-7701?", question_id=None, solicitation_filter=_WS, emitter=emitter))

    assert result.answer == draft
    assert [c.solicitation_id for c in result.citations] == [_WS]
    # Only ONE BigQuery query ran (the injected solicitation_01 call was rejected before touching BigQuery)
    assert len(bq.queries) == 1
    sql_ran, job_cfg = bq.queries[0]
    assert "solicitation_id = @solicitation_id" in sql_ran
    assert any(p.name == "solicitation_id" and p.value == _WS for p in job_cfg.query_parameters)
    tool_results = [e for e in emitter.events if e.get("event") == "agent_turn" and e.get("kind") == "tool_result"]
    assert tool_results[0]["status"] == "error" and "scoped to upload workspace" in tool_results[0]["tool_result_summary"]


def test_upload_recording_payload_passes_assert_redacted() -> None:
    proj_num = "7" * 12
    settings = live_settings(
        demo_mode="record",
        gcp_project_id="acme-priv-proj",
        gcs_corpus_bucket="acme-priv-proj-corpus",
        upload_bucket="",
    )
    result = EngineResult(
        engine_id="managed_vertex",
        engine_label="Level 5",
        answer="Grounded from gs://acme-priv-proj-rag-arena-uploads/uploads/upl_0123456789ab/f_0123456789ab-Spec.pdf",
        citations=[
            Citation(
                chunk_id="c1",
                solicitation_id=_WS,
                doc_name="Spec.pdf",
                page_number=1,
                section_id="",
                element_type="narrative_text",
                bbox=BoundingBox(page=1, x0=0.0, y0=0.0, x1=1.0, y1=1.0),
                excerpt="x",
            )
        ],
        retrieved_chunks=[],
        agent_steps=[],
        latency_ms=42,
        cost_usd=0.005,
        stage_log=(),
        usage=TokenUsage(model="gemini-4-argon"),
        flags=EngineFlags(),
    )
    events = (
        {
            "event": "stage_completed",
            "stage": "retrieve",
            "t_rel_ms": 20,
            "payload": {
                "uri": "gs://acme-priv-proj-rag-arena-uploads/uploads/upl_0123456789ab/f_0123456789ab-Spec.pdf",
                "serving": f"projects/{proj_num}/locations/global/collections/default_collection/engines/rag-arena-uploads-search",
            },
        },
    )
    outcome = EvalOutcome(
        metrics=EvalMetrics(groundedness=1.0, qa_relevance=1.0, context_precision=1.0),
        judge_model="claude-opus-5-5",
        reference_answer_used=False,
        duration_ms=30,
    )
    rec = build_recording(
        settings=settings, engine_id="managed_vertex", question_id="q_upl", query="CLIN?",
        run_id="run_u1", events=events, result=result, evaluation=outcome,
    )
    payload = rec.to_json_dict()
    assert_redacted(payload, redaction_tokens(settings))
    raw = json.dumps(payload)
    assert "acme-priv-proj" not in raw and proj_num not in raw and "${UPLOAD_BUCKET}" in raw and "${GCP_PROJECT_NUMBER}" in raw


def test_upload_store_download_to_streams_over_single_client_without_buffering() -> None:
    """Large downloads must stream chunks via a single reused httpx.Client (never per-chunk httpx.request cycles)."""
    from contextlib import contextmanager
    import httpx
    from app.ingestion.uploads import DOWNLOAD_CHUNK_BYTES, UploadStore

    total = DOWNLOAD_CHUNK_BYTES + 128
    payload = b"%PDF-1.7\n" + b"x" * (total - 9)
    stream_ranges: list[str] = []
    clients_created = 0

    class _FakeStreamResp:
        def __init__(self, body: bytes) -> None:
            self.status_code = 206
            self._body = body

        def iter_bytes(self, chunk_size: int = 262144):
            for idx in range(0, len(self._body), chunk_size):
                yield self._body[idx : idx + chunk_size]

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            nonlocal clients_created
            clients_created += 1

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def request(self, method: str, url: str, **_: Any) -> httpx.Response:
            assert method == "GET"
            return httpx.Response(200, json={"size": str(total), "generation": "1"}, request=httpx.Request(method, url))

        @contextmanager
        def stream(self, method: str, url: str, **kw: Any):
            assert method == "GET" and kw.get("params") == {"alt": "media"}
            rng = kw["headers"]["Range"]
            stream_ranges.append(rng)
            start_s, end_s = rng.removeprefix("bytes=").split("-", 1)
            yield _FakeStreamResp(payload[int(start_s) : int(end_s) + 1])

    dest = Path(tempfile.mkdtemp(prefix="rag_arena_stream_")) / "out.pdf"
    store = UploadStore(bucket="acme-uploads", token_provider=lambda: "tok", timeout_seconds=10.0)
    with patch("app.ingestion.uploads.httpx.request", side_effect=AssertionError("httpx.request leaks 8MiB cycles")), \
         patch("app.ingestion.uploads.httpx.Client", _FakeClient):
        written = store.download_to(f"uploads/{_WS}/f_0123456789ab-Doc.pdf", dest)
    assert written == total and dest.read_bytes() == payload
    assert len(stream_ranges) == 2 and clients_created <= 2

