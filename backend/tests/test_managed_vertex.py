"""Level 5 (managed_vertex) engine tests: offline, Discovery Engine REST mocked with httpx responses.

Covers the search request shape, segment parsing, summary mode vs model mode, citations from page numbers,
the 400 rules of the pseudo model, no fallback on API errors, architecture coverage and redaction.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.architectures import get_architecture_descriptor, ingest_stages_for
from app.core.models_catalog import ModelSelectionError, load_catalog
from app.core.pricing import vais_query_cost_usd
from app.core.telemetry import RecordingStageEmitter
from app.core.vertex_search import DiscoveryEngineClient, DiscoveryEngineHTTPError, redact_resource_text
from app.engines import build_engine, get_engine_registry
from app.engines.managed_vertex import ManagedVertexSearchEngine
from app.engines.vais_search import VaisResponseError, build_search_body, parse_search_response
from app.main import create_app
from fakes import FakeGenaiClient, fake_clients, live_settings, run

_BUCKET = "acme-proj-rag-arena-corpus"
_ALL = ("level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex")


def _settings(**over: Any):
    base = dict(enabled_levels=_ALL, vais_data_store_id="rag-arena-procurement", vais_engine_id="rag-arena-procurement-search",
                gcs_corpus_bucket=_BUCKET, gcp_project_id="acme-proj")
    base.update(over)
    return live_settings(**base)


def _result(doc_id: str, name: str, segs: list[tuple[str, Any]], sol: str = "solicitation_01_cloud_infrastructure") -> dict[str, Any]:
    return {"id": doc_id, "document": {
        "name": f"projects/123456789/locations/global/collections/default_collection/dataStores/rag-arena-procurement/branches/0/documents/{doc_id}",
        "id": doc_id,
        "derivedStructData": {
            "link": f"gs://{_BUCKET}/sample_docs/procurement_corpus/{sol}/{name}", "title": name.removesuffix(".pdf"),
            "extractive_segments": [{"content": c, "pageNumber": p, "relevanceScore": 0.9 - i / 10} for i, (c, p) in enumerate(segs)],
            "snippets": [{"snippet": "CLIN-0004-AB …", "snippet_status": "SUCCESS"}],
        }}}


def _search_doc(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = {
        "results": [
            _result("d1", "04_Pricing_Schedule_CLINs.pdf", [("CLIN-0004-AB Senior Cloud Architect $210.00/hr", "2")]),
            _result("d2", "06_Amendment_0001_Scope_Update.pdf", [("Amendment 0001 revises CLIN-0004-AB to $245.00/hr", 1),
                                                                ("Effective 2026-02-10", "1")]),
        ],
        "totalSize": 17, "attributionToken": "tok-1",
    }
    if summary is not None:
        doc["summary"] = summary
    return doc


class _Send:
    """Records every request; answers with queued (status, json) pairs."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        status, body = self.responses.pop(0)
        return httpx.Response(status, text=json.dumps(body), request=httpx.Request(method, url))


def _client(send: _Send) -> DiscoveryEngineClient:
    return DiscoveryEngineClient(project_id="acme-proj", location="global", token_provider=lambda: "tok",
                                 attempt_timeout_seconds=5, budget_seconds=10, sleep=lambda s: None, send=send)


def _engine(synthesis: str, send: _Send, settings=None) -> ManagedVertexSearchEngine:
    settings = settings or _settings()
    models = load_catalog(settings).select(synthesis, "claude-opus-5-5")
    return ManagedVertexSearchEngine(settings, synthesis_model=models.synthesis, client=_client(send))


_SUMMARY = {"summaryText": "CLIN-0004-AB was $210.00/hr and Amendment 0001 set it to $245.00/hr [2].",
            "summarySkippedReasons": [], "safetyAttributes": {"categories": [], "scores": []},
            "summaryWithMetadata": {"summary": "x", "citationMetadata": {"citations": [{"startIndex": "0", "endIndex": "9", "sources": [{"referenceIndex": "1"}]}]},
                                    "references": [{"title": "04", "document": "projects/123456789/locations/global/collections/default_collection/dataStores/rag-arena-procurement/branches/0/documents/d1"},
                                                   {"title": "06", "document": "projects/123456789/locations/global/collections/default_collection/dataStores/rag-arena-procurement/branches/0/documents/d2"}]}}


def test_search_body_shape_with_and_without_summary_spec() -> None:
    body = build_search_body("CLIN rate?", page_size=10, max_segments=3, summary_result_count=None, summary_model_version="stable")
    assert body == {"query": "CLIN rate?", "pageSize": 10, "contentSearchSpec": {
        "snippetSpec": {"returnSnippet": True},
        "extractiveContentSpec": {"maxExtractiveSegmentCount": 3, "returnExtractiveSegmentScore": True}}}
    summ = build_search_body("q", page_size=10, max_segments=3, summary_result_count=5, summary_model_version="stable")
    assert summ["contentSearchSpec"]["summarySpec"] == {
        "summaryResultCount": 5, "includeCitations": True, "ignoreAdversarialQuery": True,
        "ignoreNonSummarySeekingQuery": False, "modelSpec": {"version": "stable"}}
    with pytest.raises(ValueError):
        build_search_body("q", page_size=50, max_segments=3, summary_result_count=None, summary_model_version="stable")


def test_segment_parsing_pages_scores_doc_names_and_strict_failures() -> None:
    out = parse_search_response(_search_doc(), summary_requested=False)
    assert [h.doc_name for h in out.hits] == ["04_Pricing_Schedule_CLINs.pdf", "06_Amendment_0001_Scope_Update.pdf"]
    assert out.hits[0].segments[0].page_number == 2 and out.hits[1].segments[1].page_number == 1
    assert out.hits[0].solicitation_id == "solicitation_01" and out.total_size == 17 and out.attribution_token == "tok-1"
    bad = _search_doc()
    del bad["results"][0]["document"]["derivedStructData"]["extractive_segments"][0]["pageNumber"]
    for doc in (bad, {"results": [], "totalSize": 0}):
        try:
            parse_search_response(doc, summary_requested=False)
        except VaisResponseError:
            pass
        else:
            raise AssertionError("missing pageNumber / zero results must fail loud")
    try:
        parse_search_response(_search_doc({"summaryText": "", "summarySkippedReasons": ["OUT_OF_DOMAIN_QUERY_IGNORED"]}), summary_requested=True)
    except VaisResponseError as exc:
        assert "OUT_OF_DOMAIN_QUERY_IGNORED" in str(exc)
    else:
        raise AssertionError("a skipped summary must fail loud with the reasons verbatim")


def test_summary_mode_one_call_summary_answer_and_citations_from_cited_result_pages() -> None:
    send = _Send((200, _search_doc(_SUMMARY)))
    engine = _engine("vertex-ai-search-summary", send)
    emitter = RecordingStageEmitter(run_id="r", engine="managed_vertex")
    result = run(engine.run_query("What is the CLIN-0004-AB rate?", question_id=None, solicitation_filter=None, emitter=emitter))
    assert len(send.calls) == 1 and send.calls[0]["url"].endswith(
        "/v1/projects/acme-proj/locations/global/collections/default_collection/engines/rag-arena-procurement-search/servingConfigs/default_search:search")
    sent = send.calls[0]["json"]
    assert "summarySpec" in sent["contentSearchSpec"] and "Contract Line Item Number" in sent["query"]
    assert send.calls[0]["headers"]["x-goog-user-project"] == "acme-proj"
    assert result.answer.startswith("CLIN-0004-AB was $210.00/hr")
    assert [c.doc_name for c in result.citations] == ["06_Amendment_0001_Scope_Update.pdf"] * 2  # [2] → result 2's segments
    assert all(c.bbox_source == "page" and c.layout_source == "vertex_ai_search" and c.bbox.page == c.page_number for c in result.citations)
    assert result.usage.model == "vertex-ai-search-summary" and result.cost_usd == vais_query_cost_usd(searches=1, summary_queries=1) == 0.008
    stages = [(e["event"], e["stage"]) for e in emitter.events]
    assert stages == [("stage_started", "query_prep"), ("stage_completed", "query_prep"), ("stage_started", "retrieve"),
                      ("stage_completed", "retrieve"), ("stage_started", "synthesize"), ("stage_completed", "synthesize")]
    synth = emitter.events[5]["payload"]
    assert synth["generated_in_stage"] == "retrieve" and synth["cited_result_ranks"] == [2] and synth["citation_source"] == "summary_citations"
    assert synth["model_version_used"] == "not returned by the API"
    assert len(result.retrieved_chunks) == 3  # judge context = segments of the top VAIS_SUMMARY_RESULT_COUNT results


def test_model_mode_synthesizes_over_segments_with_catalog_model_and_cites_every_context_segment() -> None:
    send = _Send((200, _search_doc()))
    with fake_clients(genai=FakeGenaiClient(answer="Rate is $245.00/hr (06_Amendment_0001_Scope_Update.pdf p.1).")):
        engine = _engine("gemini-4-argon", send)
        emitter = RecordingStageEmitter(run_id="r", engine="managed_vertex")
        result = run(engine.run_query("CLIN-0004-AB rate?", question_id=None, solicitation_filter=None, emitter=emitter))
    assert "summarySpec" not in send.calls[0]["json"]["contentSearchSpec"]
    assert result.usage.model == "gemini-4-argon" and result.usage.prompt_tokens > 0
    assert [c.page_number for c in result.citations] == [2, 1, 1]
    retrieve = next(e for e in emitter.events if e["event"] == "stage_completed" and e["stage"] == "retrieve")["payload"]
    assert retrieve["results"][0]["extractive_segments"][0] == {"content": "CLIN-0004-AB Senior Cloud Architect $210.00/hr", "page_number": 2, "relevance_score": 0.9}
    assert retrieve["total_size"] == 17 and retrieve["attribution_token"] == "tok-1" and retrieve["api_ms"] >= 1
    synth = next(e for e in emitter.events if e["event"] == "stage_completed" and e["stage"] == "synthesize")["payload"]
    assert synth["context_source"] == "Vertex AI Search extractive segments" and synth["context_segments"] == 3
    assert result.cost_usd > vais_query_cost_usd(searches=1, summary_queries=0)


def test_api_error_fails_the_retrieve_stage_without_fallback_and_redacts_resource_names() -> None:
    err = {"error": {"code": 403, "message": "Permission denied on projects/123456789/locations/global for acme-proj"}}
    send = _Send((403, err))
    engine = _engine("vertex-ai-search-summary", send)
    emitter = RecordingStageEmitter(run_id="r", engine="managed_vertex")
    try:
        run(engine.run_query("q?", question_id=None, solicitation_filter=None, emitter=emitter))
    except DiscoveryEngineHTTPError as exc:
        assert exc.status_code == 403 and "123456789" not in str(exc) and "acme-proj" not in str(exc)
        assert "${GCP_PROJECT_NUMBER}" in str(exc) and "${GCP_PROJECT_ID}" in str(exc)
    else:
        raise AssertionError("a Vertex AI Search error must fail loud")
    assert [(e["event"], e["stage"]) for e in emitter.events][-1] == ("stage_failed", "retrieve")
    assert len(send.calls) == 1  # 403 is not retried; nothing else is called
    assert redact_resource_text("projects/42/x acme-proj", "acme-proj") == "projects/${GCP_PROJECT_NUMBER}/x ${GCP_PROJECT_ID}"


def test_retry_only_on_transient_status_and_bounded() -> None:
    send = _Send((503, {}), (503, {}), (503, {}))
    with pytest.raises(DiscoveryEngineHTTPError):
        _client(send).request("POST", "x:search", body={})
    assert len(send.calls) == 3


def test_missing_ids_and_solicitation_filter_fail_loud() -> None:
    engine = _engine("gemini-4-argon", _Send(), settings=_settings(vais_engine_id=""))
    for kwargs, exc_type in (({"solicitation_filter": None}, RuntimeError), ({"solicitation_filter": "solicitation_01"}, ValueError)):
        emitter = RecordingStageEmitter(run_id="r", engine="managed_vertex")
        with pytest.raises(exc_type):
            run(engine.run_query("q?", question_id=None, emitter=emitter, **kwargs))


def test_pseudo_model_is_synthesis_only_and_rejected_on_other_levels_with_http_400() -> None:
    settings = _settings()
    catalog = load_catalog(settings)
    spec = catalog.get("vertex-ai-search-summary")
    assert spec.roles == ("synthesis",) and spec.allowed_levels == ("managed_vertex",) and spec.provider == "vertex_ai_search"
    with pytest.raises(ModelSelectionError):
        catalog.select("gemini-4-argon", "vertex-ai-search-summary")
    models = catalog.select("vertex-ai-search-summary", "claude-opus-5-5")
    for level in _ALL[:4]:
        with pytest.raises(ModelSelectionError):
            build_engine(level, settings, models)
    assert build_engine("managed_vertex", settings, models).summary_mode is True
    assert set(get_engine_registry(settings, models, level_ids=("managed_vertex",))) == {"managed_vertex"}
    client = TestClient(create_app(settings=settings, query_run_writer=None))
    resp = client.post("/api/v1/battle/stream", json={"query": "q", "engines": ["level1_naive"],
                                                      "synthesis_model": "vertex-ai-search-summary", "judge_model": "claude-opus-5-5"})
    assert resp.status_code == 400 and "only allowed on ['managed_vertex']" in resp.json()["error"]
    replay = live_settings(demo_mode="replay", enabled_levels=_ALL)
    with pytest.raises(ModelSelectionError):
        build_engine("level2_hybrid", replay, load_catalog(replay).select("vertex-ai-search-summary", "claude-opus-5-5"))


def test_claude_synthesis_on_level1_is_not_blocked_by_an_enabled_level4() -> None:
    settings = _settings()
    models = load_catalog(settings).select("claude-opus-5-5", "gemini-4-argon")
    assert set(get_engine_registry(settings, models, level_ids=("level1_naive",))) == {"level1_naive"}


def test_pseudo_model_unavailable_in_live_mode_without_vais_ids() -> None:
    spec = load_catalog(_settings(vais_engine_id="")).get("vertex-ai-search-summary")
    assert spec.available is False and "VAIS_ENGINE_ID" in spec.unavailable_reason
    assert load_catalog(live_settings(demo_mode="replay")).get("vertex-ai-search-summary").available is True


def test_architecture_maps_every_stage_and_managed_ingest_stages() -> None:
    desc = get_architecture_descriptor("managed_vertex", _settings())
    mapped = {s for n in desc.nodes for s in n.stage_ids}
    assert set(ManagedVertexSearchEngine.stages) <= mapped
    ingest_mapped = {s for n in desc.ingest_nodes for s in n.stage_ids}
    assert set(ingest_stages_for("managed_vertex")) <= ingest_mapped
    by_id = {n.id: n for n in desc.ingest_nodes}
    assert by_id["managed"].stage_ids == ("parse", "chunk", "embed", "load_bigquery", "build_indexes")
    assert by_id["gcs"].stage_ids == ("upload_gcs",) and by_id["import"].stage_ids == ("import_documents",)
    assert all(n.region != "{VAIS_LOCATION}" for n in (*desc.nodes, *desc.ingest_nodes))


def test_retrieve_payload_redacts_bucket_and_carries_no_resource_names() -> None:
    send = _Send((200, _search_doc(_SUMMARY)))
    engine = _engine("vertex-ai-search-summary", send)
    emitter = RecordingStageEmitter(run_id="r", engine="managed_vertex")
    run(engine.run_query("q?", question_id=None, solicitation_filter=None, emitter=emitter))
    blob = json.dumps(emitter.events)
    assert _BUCKET not in blob and "acme-proj" not in blob and "projects/" not in blob
    assert "gs://${GCS_CORPUS_BUCKET}/sample_docs/procurement_corpus/" in blob


def test_no_local_search_fallback_code_paths_in_engine_module() -> None:
    import inspect

    import app.engines.managed_vertex as mv

    src = inspect.getsource(mv)
    for forbidden in ("bigquery", "VECTOR_SEARCH", "except Exception", "token overlap"):
        assert forbidden not in src, forbidden
    with patch("app.engines.managed_vertex.build_search_client") as built:
        built.side_effect = AssertionError("client injected; factory must not be used")
        _engine("gemini-4-argon", _Send())
