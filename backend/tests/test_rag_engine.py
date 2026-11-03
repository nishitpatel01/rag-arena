"""Level 6 (``rag_engine``) engine, model catalog, architecture, and HTTP upload-lifecycle tests (offline)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.architectures import get_architecture_descriptor, ingest_stages_for, list_architecture_descriptors
from app.core.config import ALL_LEVEL_IDS
from app.core.models_catalog import (
    PROVIDER_VERTEX_RAG_ENGINE,
    RAG_ENGINE_GROUNDED_MODEL_ID,
    ModelSelectionError,
    load_catalog,
)
from app.core.rag_engine_client import VertexRagClient
from app.engines import ENGINE_CLASSES, build_engine, get_engine_registry, list_levels
from app.engines.rag_engine import (
    LAYOUT_SOURCE,
    RagEngine,
    RagEngineResponseError,
    build_retrieve_body,
    parse_retrieve_response,
)
from app.main import create_app
from fakes import FakeGenaiClient, fake_clients, live_settings

_CORPUS_BUCKET = "acme-proj-rag-arena-corpus"
_UPLOAD_BUCKET = "acme-proj-rag-arena-uploads"
_CORPUS_ID = "3379951520341557248"
_UPL_CORPUS_ID = "7788990011223344556"
_WS = "upl_0123456789ab"
_FID = "f_abcdef012345"


class _RecordingEmitter:
    """Minimal StageEmitter double capturing started/completed/failed records."""

    def __init__(self) -> None:
        from app.core.telemetry import QueueStageEmitter

        self.q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.inner = QueueStageEmitter(run_id="run_rag_test", engine="rag_engine", queue=self.q)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _Router:
    def __init__(self, rules: list[tuple[str, str, Any]]) -> None:
        self.rules = rules
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        for expected_method, needle, answer in self.rules:
            if method == expected_method and needle in url:
                status, body = answer(kw) if callable(answer) else answer
                text = "" if body is None else json.dumps(body)
                return httpx.Response(status, text=text, request=httpx.Request(method, url))
        raise AssertionError(f"unexpected HTTP call: {method} {url}")


def _rag_client(router: _Router) -> VertexRagClient:
    return VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda s: None,
        send=router,
    )


def _sample_retrieve_doc(uri: str, *, page: int | None = 4, text: str = "CLIN-0004-AB rate is $245.00/hr.") -> dict[str, Any]:
    chunk: dict[str, Any] = {"chunkId": "c_01", "fileId": "rf_01", "text": text}
    if page is not None:
        chunk["pageSpan"] = {"firstPage": page, "lastPage": page}
    return {
        "contexts": {
            "contexts": [
                {
                    "sourceUri": uri,
                    "sourceDisplayName": uri.rsplit("/", 1)[-1],
                    "text": text,
                    "score": 0.1823,
                    "chunk": chunk,
                }
            ]
        }
    }


def test_level6_registration_descriptor_and_pseudo_model_gating() -> None:
    assert "rag_engine" in ALL_LEVEL_IDS
    assert ENGINE_CLASSES[-1] is RagEngine
    settings = live_settings(
        gcp_project_id="acme-proj",
        enabled_levels=ALL_LEVEL_IDS,
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    assert len(list_architecture_descriptors(settings)) == 6
    desc = get_architecture_descriptor("rag_engine", settings)
    stage_mapped = {sid for n in desc.nodes for sid in n.stage_ids}
    ingest_mapped = {sid for n in desc.ingest_nodes for sid in n.stage_ids}
    assert set(RagEngine.stages) == {"query_prep", "retrieve", "synthesize", "evaluate"}
    assert set(RagEngine.stages) <= stage_mapped
    assert set(ingest_stages_for("rag_engine")) <= ingest_mapped
    assert any(n.region == "us-central1" for n in desc.nodes)

    catalog = load_catalog(settings)
    grounded = catalog.get(RAG_ENGINE_GROUNDED_MODEL_ID)
    assert grounded.provider == PROVIDER_VERTEX_RAG_ENGINE
    assert grounded.roles == ("synthesis",)
    assert grounded.allowed_levels == ("rag_engine",)
    assert grounded.available is True

    models = catalog.select(RAG_ENGINE_GROUNDED_MODEL_ID, "claude-opus-5-5")
    eng = build_engine("rag_engine", settings, models)
    assert isinstance(eng, RagEngine) and eng.grounded_mode is True
    assert set(get_engine_registry(settings, models, level_ids=("rag_engine",))) == {"rag_engine"}
    with pytest.raises(ModelSelectionError):
        build_engine("level1_naive", settings, models)

    unconfigured = live_settings(gcp_project_id="acme-proj", upload_bucket="", rag_engine_corpus_id="")
    unavail = load_catalog(unconfigured).get(RAG_ENGINE_GROUNDED_MODEL_ID)
    assert unavail.available is False and "RAG_ENGINE_CORPUS_ID" in (unavail.unavailable_reason or "")


def test_parse_retrieve_response_maps_page_span_and_redacts_uris() -> None:
    uri = f"gs://{_CORPUS_BUCKET}/sample_docs/solicitation_01_cloud_migration/04_Pricing_Schedule_CLINs.pdf"
    body = build_retrieve_body(
        "What is CLIN-0004-AB?",
        corpus_path=f"projects/acme-proj/locations/us-central1/ragCorpora/{_CORPUS_ID}",
        top_k=10,
    )
    assert body["query"]["ragRetrievalConfig"]["topK"] == 10
    hits = parse_retrieve_response(_sample_retrieve_doc(uri, page=4))
    assert len(hits) == 1
    assert hits[0].solicitation_id == "solicitation_01"
    assert hits[0].doc_name == "04_Pricing_Schedule_CLINs.pdf"
    assert hits[0].page_number == 4 and hits[0].page_source == "chunk.pageSpan"

    # Missing pageSpan falls back transparently to page 1 with explicit page_source
    no_span = parse_retrieve_response(_sample_retrieve_doc(uri, page=None))
    assert no_span[0].page_number == 1 and no_span[0].page_source == "document_default_page_1"

    with pytest.raises(RagEngineResponseError):
        parse_retrieve_response({"contexts": {"contexts": []}})


def test_rag_engine_run_query_catalog_synthesis_and_redaction() -> None:
    uri = f"gs://{_CORPUS_BUCKET}/sample_docs/solicitation_01_cloud_migration/04_Pricing_Schedule_CLINs.pdf"
    router = _Router([
        ("POST", ":retrieveContexts", (200, _sample_retrieve_doc(uri, page=2))),
    ])
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        enabled_levels=("rag_engine",),
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    catalog = load_catalog(settings)
    spec = catalog.get("gemini-4-argon")
    engine = RagEngine(settings=settings, synthesis_model=spec, client=_rag_client(router))
    emitter = _RecordingEmitter()

    with fake_clients() as (genai, _):
        result = asyncio.run(
            engine.run_query(
                "What is the amended labor rate for CLIN-0004-AB?",
                question_id="q01",
                solicitation_filter=None,
                emitter=emitter.inner,
            )
        )

    assert result.engine_id == "rag_engine"
    assert "245.00" in result.answer
    assert result.flags.layout_source == LAYOUT_SOURCE
    assert len(result.citations) == 1
    assert result.citations[0].doc_name == "04_Pricing_Schedule_CLINs.pdf"
    assert result.citations[0].page_number == 2
    assert result.usage.model == "gemini-4-argon"
    assert result.cost_usd > 0.0
    assert [r.stage for r in result.stage_log] == ["query_prep", "retrieve", "synthesize"]
    assert len(genai.calls) == 1 and genai.calls[0][0] == "generate_content"

    serialized = json.dumps([r.to_dict() for r in result.stage_log])
    assert _CORPUS_BUCKET not in serialized
    assert "acme-proj" not in serialized
    assert _CORPUS_ID not in serialized


def test_rag_engine_run_query_native_vertex_rag_store_grounded_mode() -> None:
    uri = f"gs://{_CORPUS_BUCKET}/sample_docs/solicitation_01_cloud_migration/04_Pricing_Schedule_CLINs.pdf"
    router = _Router([
        ("POST", ":retrieveContexts", (200, _sample_retrieve_doc(uri, page=3))),
    ])
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        enabled_levels=("rag_engine",),
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    spec = load_catalog(settings).get(RAG_ENGINE_GROUNDED_MODEL_ID)
    engine = RagEngine(settings=settings, synthesis_model=spec, client=_rag_client(router))
    emitter = _RecordingEmitter()

    fake_genai = FakeGenaiClient(answer="Grounded via VertexRagStore: CLIN-0004-AB is $245.00/hr.")
    with patch("app.engines.rag_engine.get_genai_client", lambda s, location=None: fake_genai):
        result = asyncio.run(
            engine.run_query(
                "What is CLIN-0004-AB?",
                question_id="q01",
                solicitation_filter=None,
                emitter=emitter.inner,
            )
        )

    assert "VertexRagStore" in result.answer
    assert result.usage.prompt_tokens == 1842 and result.usage.output_tokens == 96
    assert result.cost_usd > 0.0
    assert len(fake_genai.calls) == 1
    call_cfg = fake_genai.calls[0][1]["config"]
    dumped = call_cfg.model_dump(exclude_none=True)
    assert dumped["tools"][0]["retrieval"]["vertex_rag_store"]["rag_resources"][0]["rag_corpus"].endswith(f"/ragCorpora/{_CORPUS_ID}")


def test_rag_engine_upload_scope_resolves_workspace_corpus_and_enforces_isolation() -> None:
    upl_uri = f"gs://{_UPLOAD_BUCKET}/uploads/{_WS}/{_FID}-Pricing.pdf"
    router = _Router([
        ("GET", "/ragCorpora", (200, {"ragCorpora": [
            {"name": "projects/76543/locations/us-central1/ragCorpora/999111222", "displayName": "neighbor_corpus"},
            {"name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}", "displayName": f"rag-arena-{_WS}"},
        ]})),
        ("POST", ":retrieveContexts", (200, _sample_retrieve_doc(upl_uri, page=1, text="Exhibit rate $14,500/mo."))),
    ])
    settings = live_settings(
        gcp_project_id="acme-proj",
        upload_bucket=_UPLOAD_BUCKET,
        enabled_levels=("rag_engine",),
        rag_engine_location="us-central1",
        rag_engine_corpus_id="",  # upload search works even if benchmark corpus is unset
    )
    engine = RagEngine(settings=settings, client=_rag_client(router))
    emitter = _RecordingEmitter()
    with fake_clients():
        result = asyncio.run(
            engine.run_query("What is the exhibit rate?", question_id=None, solicitation_filter=_WS, emitter=emitter.inner)
        )
    assert result.citations[0].solicitation_id == _WS
    assert result.citations[0].doc_name == "Pricing.pdf"
    retrieve_call = next(c for c in router.calls if c["url"].endswith(":retrieveContexts"))
    assert retrieve_call["json"]["vertexRagStore"]["ragResources"][0]["ragCorpus"].endswith(f"/ragCorpora/{_UPL_CORPUS_ID}")
    serialized = json.dumps([r.to_dict() for r in result.stage_log])
    assert _UPLOAD_BUCKET not in serialized and _UPL_CORPUS_ID not in serialized

    # Foreign workspace chunk returned -> fails loud!
    leaked_uri = f"gs://{_UPLOAD_BUCKET}/uploads/upl_ffffffffffff/{_FID}-Other.pdf"
    bad_router = _Router([
        ("GET", "/ragCorpora", (200, {"ragCorpora": [
            {"name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}", "displayName": f"rag-arena-{_WS}"},
        ]})),
        ("POST", ":retrieveContexts", (200, _sample_retrieve_doc(leaked_uri, page=1))),
    ])
    bad_engine = RagEngine(settings=settings, client=_rag_client(bad_router))
    with fake_clients():
        with pytest.raises(RagEngineResponseError):
            asyncio.run(
                bad_engine.run_query("Leak?", question_id=None, solicitation_filter=_WS, emitter=_RecordingEmitter().inner)
            )


def test_rag_engine_solicitation_filter_passes_matching_rag_file_ids() -> None:
    uri1 = f"gs://{_CORPUS_BUCKET}/sample_docs/solicitation_02_cyber/01_SOW.pdf"
    uri2 = f"gs://{_CORPUS_BUCKET}/sample_docs/solicitation_03_erp/01_SOW.pdf"
    router = _Router([
        ("GET", "/ragFiles", (200, {"ragFiles": [
            {"name": f"projects/76543/locations/us-central1/ragCorpora/{_CORPUS_ID}/ragFiles/rf_match", "gcsSource": {"uris": [uri1]}},
            {"name": f"projects/76543/locations/us-central1/ragCorpora/{_CORPUS_ID}/ragFiles/rf_other", "gcsSource": {"uris": [uri2]}},
        ]})),
        ("POST", ":retrieveContexts", (200, _sample_retrieve_doc(uri1, page=1, text="Cyber SLA is 15 minutes."))),
    ])
    settings = live_settings(
        gcp_project_id="acme-proj",
        gcs_corpus_bucket=_CORPUS_BUCKET,
        enabled_levels=("rag_engine",),
        rag_engine_location="us-central1",
        rag_engine_corpus_id=_CORPUS_ID,
    )
    engine = RagEngine(settings=settings, client=_rag_client(router))
    with fake_clients():
        result = asyncio.run(
            engine.run_query(
                "What is the Cyber SLA?",
                question_id=None,
                solicitation_filter="solicitation_02",
                emitter=_RecordingEmitter().inner,
            )
        )
    assert result.citations[0].solicitation_id == "solicitation_02"
    retrieve_call = next(c for c in router.calls if c["url"].endswith(":retrieveContexts"))
    assert retrieve_call["json"]["vertexRagStore"]["ragResources"][0]["ragFileIds"] == ["rf_match"]


def test_rag_engine_http_upload_ingest_estimate_and_fail_closed_delete() -> None:
    from app.core.rag_engine_client import RagEngineResources
    from app.ingestion.jobs import IngestBackends, IngestJobRunner
    from app.ingestion.rag_engine_job import RagEngineUploadBackends
    from test_ingest_jobs_api import FakeEmbedder
    from test_upload_ingest import ScopedLoader
    from test_uploads_api import FakeManager, FakeServices, FakeStore, _ALICE, _corpus_pdf, _sse, _upload, _workspace

    loader = ScopedLoader()
    loader.deleted_scopes = []  # type: ignore[attr-defined]
    loader.delete_scope = lambda scope: loader.deleted_scopes.append(scope) or 0  # type: ignore[attr-defined]
    store = FakeStore()
    store.bucket = _UPLOAD_BUCKET  # type: ignore[attr-defined]
    services = FakeServices(FakeManager(), store)
    deleted_corpora: list[tuple[str, str]] = []
    state = {"fail_delete": False}

    def delete_rag_corpus(ws_id: str, manifest: dict[str, Any]) -> int:
        if state["fail_delete"]:
            raise RuntimeError("Vertex AI RAG Engine 503 unavailable")
        rec_id = str(((manifest.get("levels") or {}).get("rag_engine") or {}).get("rag_corpus_id") or "")
        deleted_corpora.append((ws_id, rec_id))
        return 1

    services.delete_rag_corpus = delete_rag_corpus  # type: ignore[attr-defined]

    created_ws_corpus = {"id": _UPL_CORPUS_ID, "imported_uris": []}

    def send(method: str, url: str, **kw: Any) -> httpx.Response:
        req = httpx.Request(method, url)
        if method == "GET" and url.endswith("/ragCorpora"):
            return httpx.Response(200, text=json.dumps({"ragCorpora": []}), request=req)
        if method == "POST" and url.endswith("/ragCorpora"):
            op = {
                "name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}/operations/op1",
                "done": True,
                "response": {"name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}"},
            }
            return httpx.Response(200, text=json.dumps(op), request=req)
        if method == "POST" and url.endswith("/ragFiles:import"):
            uris = kw["json"]["importRagFilesConfig"]["gcsSource"]["uris"]
            created_ws_corpus["imported_uris"].extend(uris)
            op = {
                "name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}/operations/op2",
                "done": True,
                "response": {"importedRagFilesCount": str(len(uris)), "failedRagFilesCount": "0"},
            }
            return httpx.Response(200, text=json.dumps(op), request=req)
        if method == "GET" and "/ragFiles" in url:
            files = [
                {"name": f"projects/76543/locations/us-central1/ragCorpora/{_UPL_CORPUS_ID}/ragFiles/rf1",
                 "gcsSource": {"uris": [u]}, "fileStatus": {"state": "ACTIVE"}}
                for u in created_ws_corpus["imported_uris"]
            ]
            return httpx.Response(200, text=json.dumps({"ragFiles": files}), request=req)
        raise AssertionError(f"unexpected {method} {url}")

    rag_client = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda s: None,
        send=send,
    )
    upload_rag = RagEngineUploadBackends(
        store=store,
        rag=rag_client,
        resources=RagEngineResources("acme-proj", "us-central1"),
        write_ingest_run=lambda r: None,
    )
    settings = live_settings(
        gcp_project_id="acme-proj",
        upload_bucket=_UPLOAD_BUCKET,
        ingest_job_timeout_seconds=120,
        enabled_levels=("level1_naive", "rag_engine"),
        rag_engine_location="us-central1",
    )
    factory = lambda s: IngestBackends(FakeEmbedder(), loader)  # noqa: E731
    app = create_app(
        settings=settings,
        query_run_writer=object(),
        ingest_backend_factory=factory,
        ingest_upload_rag_engine_factory=lambda s: upload_rag,
    )
    app.state.ingest_runner = IngestJobRunner(
        settings, backend_factory=factory, upload_rag_engine_factory=lambda s: upload_rag
    )
    app.state.upload_services = services
    client = TestClient(app)
    with client:
        ws = _workspace(client)
        _upload(client, store, ws, _corpus_pdf(), name="Pricing.pdf")
        est = client.get(f"/api/v1/uploads/{ws}/estimate?level=rag_engine", headers=_ALICE)
        assert est.status_code == 200
        assert est.json()["estimate"]["requires_confirmation"] is False
        assert "RAG Engine" in est.json()["estimate"]["basis"]

        resp = client.post(f"/api/v1/uploads/{ws}/ingest", headers=_ALICE, json={"level": "rag_engine"})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job"]["job_id"]
        events = _sse(client.get(f"/api/v1/ingest/jobs/{job_id}/stream", headers=_ALICE).text)
        assert events[-1]["event"] == "job_complete"
        re_entry = client.get(f"/api/v1/uploads/{ws}", headers=_ALICE).json()["workspace"]["levels"]["rag_engine"]
        assert re_entry["status"] == "completed"
        assert re_entry["tier_profile"] == "rag_engine"
        assert re_entry["chunks"] == 1
        assert "rag_corpus_id" not in re_entry
        assert re_entry["rag_corpus_display_name"] == f"rag-arena-{ws}"
        assert store.manifests[ws][0]["levels"]["rag_engine"]["rag_corpus_id"] == _UPL_CORPUS_ID

        # Fail-closed delete: if RagCorpus deletion fails, GCS manifest + objects remain for retry
        state["fail_delete"] = True
        failed_del = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
        assert failed_del.status_code == 502 and "RAG Engine" in failed_del.json()["error"]
        assert ws in store.manifests

        # Retry succeeds: deletes workspace RagCorpus first, then GCS objects
        state["fail_delete"] = False
        ok_del = client.delete(f"/api/v1/uploads/{ws}", headers=_ALICE)
        assert ok_del.status_code == 200, ok_del.text
        assert deleted_corpora == [(ws, _UPL_CORPUS_ID)]
        assert ok_del.json()["vertex_rag_engine"] == {"deleted_corpora": 1}
        assert ws not in store.manifests

