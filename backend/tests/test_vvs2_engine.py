"""Level 8 (``vertex_vector_v2``) client, sparse tokenizer, ScaNN RRF engine, and guard tests (offline)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from app.core.models_catalog import spec_for_settings_model
from app.core.ranking import RankingClient
from app.core.telemetry import QueueStageEmitter
from fakes import FakeGenaiClient, fake_clients, live_settings

_RFC1035_RE = re.compile(r"^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class _CaptureEmitter(QueueStageEmitter):
    def __init__(self, engine: str = "vertex_vector_v2") -> None:
        self.q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        super().__init__(self.q, run_id="run_vvs2_test", engine=engine)

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out


def _fake_ranking_client(project_id: str, scores: list[tuple[str, float]]) -> RankingClient:
    def _post(url: str, **kwargs: Any) -> httpx.Response:
        body = {"records": [{"id": rid, "score": sc} for rid, sc in scores]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return RankingClient(
        project_id=project_id,
        model="semantic-ranker-default-004",
        attempt_timeout_seconds=5.0,
        budget_seconds=10.0,
        token_provider=lambda: "fake-rank-token",
        sleep=lambda _s: None,
        post=_post,
    )


def test_vvs2_data_object_id_and_sparse_token_embedding() -> None:
    from app.core.vvs2_client import build_sparse_token_embedding, data_object_id

    oid_a = data_object_id("W912DY-24-R-0001_hybrid_0012")
    oid_b = data_object_id("W912DY-24-R-0001_hybrid_0013")
    assert _RFC1035_RE.match(oid_a), f"not RFC1035: {oid_a}"
    assert "_" not in oid_a
    assert oid_a == data_object_id("W912DY-24-R-0001_hybrid_0012")
    assert oid_a != oid_b

    for bad in ("", "   "):
        try:
            data_object_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")

    sparse = build_sparse_token_embedding("What is CLIN-0004-AB hourly labor rate? CLIN-0004-AB")
    indices = sparse["indices"]
    values = sparse["values"]
    assert len(indices) == len(values) > 0
    assert len(indices) <= 128
    assert indices == sorted(set(indices))
    assert all(isinstance(i, int) and 0 <= i <= 0x7FFFFFFF for i in indices)
    assert any(v > 1.0 for v in values)

    empty = build_sparse_token_embedding("   ??? !!!   ")
    assert empty == {"indices": [], "values": []}


def test_vvs2_resources_schemas_and_rest_client_redaction() -> None:
    from app.core.vvs2_client import (
        Vvs2Client,
        Vvs2HttpError,
        Vvs2Resources,
        batch_search_body,
        collection_body,
        data_object_request,
        dense_scann_index_body,
    )

    settings = live_settings(
        gcp_project_id="secret-proj-99",
        vvs2_location="us-central1",
        vvs2_collection="rag-arena-ephem-vvs2",
        vvs2_index_id="dense-scann",
    )
    res = Vvs2Resources.from_settings(settings)
    assert res.parent == "projects/secret-proj-99/locations/us-central1"
    assert res.collection_name == "projects/secret-proj-99/locations/us-central1/collections/rag-arena-ephem-vvs2"
    assert res.index_name == (
        "projects/secret-proj-99/locations/us-central1/collections/rag-arena-ephem-vvs2/indexes/dense-scann"
    )
    assert res.redacted_collection_name == (
        "projects/${GCP_PROJECT_ID}/locations/us-central1/collections/rag-arena-ephem-vvs2"
    )

    for bad_kwargs in (
        {"vvs2_location": "global", "vvs2_collection": "rag-arena-ephem-vvs2"},
        {"vvs2_location": "us-central1", "vvs2_collection": ""},
        {"vvs2_location": "us-central1", "vvs2_collection": "Bad_Upper"},
    ):
        try:
            Vvs2Resources.from_settings(live_settings(**bad_kwargs))
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad_kwargs}")

    col = collection_body(display_name="RAG Arena Ephemeral", vector_dim=3072)
    assert col["vectorSchema"]["dense_embedding"]["denseVector"]["dimensions"] == 3072
    assert col["vectorSchema"]["sparse_embedding"] == {"sparseVector": {}}
    assert {"chunk_id", "doc_name", "solicitation_id", "content", "page_number"} <= set(
        col["dataSchema"]["properties"]
    )

    idx = dense_scann_index_body(display_name="Dense ScaNN")
    assert idx["indexField"] == "dense_embedding"
    assert idx["distanceMetric"] == "COSINE_DISTANCE"
    assert idx["denseScann"]["featureNormType"] == "UNIT_L2_NORM"
    assert "solicitation_id" in idx["filterFields"]
    assert idx["dedicatedInfrastructure"] == {
        "mode": "PERFORMANCE_OPTIMIZED",
        "autoscalingSpec": {"minReplicaCount": 1, "maxReplicaCount": 2},
    }

    obj_req = data_object_request(
        collection_name=res.collection_name,
        chunk_row={
            "chunk_id": "W912DY-24-R-0001_hybrid_0001",
            "doc_name": "Solicitation_01.pdf",
            "solicitation_id": "W912DY-24-R-0001",
            "section_id": "Section B.4",
            "content": "CLIN-0004-AB hourly labor rate is 142.50 USD.",
            "element_type": "narrative_text",
            "page_number": 4,
            "amendment_number": 1,
        },
        dense_embedding=[0.1, 0.2, 0.3],
    )
    assert _RFC1035_RE.match(obj_req["dataObjectId"])
    assert obj_req["dataObject"]["vectors"]["dense_embedding"]["dense"]["values"] == [0.1, 0.2, 0.3]
    assert len(obj_req["dataObject"]["vectors"]["sparse_embedding"]["sparse"]["indices"]) > 0

    search_req = batch_search_body(
        query_vector=[0.25, 0.75],
        query_text="CLIN-0004-AB labor rate",
        top_k=25,
        solicitation_filter="W912DY-24-R-0001",
        index_name=res.index_name,
    )
    assert len(search_req["searches"]) == 2
    assert search_req["searches"][0]["vectorSearch"]["filter"] == {"solicitation_id": {"$eq": "W912DY-24-R-0001"}}
    assert search_req["searches"][0]["vectorSearch"]["outputFields"] == {"dataFields": ["*"]}
    assert search_req["searches"][1]["vectorSearch"]["outputFields"] == {"dataFields": ["*"]}
    assert search_req["searches"][1]["vectorSearch"]["searchHint"] == {"knnHint": {}}
    assert search_req["combine"]["ranker"]["rrf"]["weights"] == [1.0, 1.0]
    assert search_req["combine"]["outputFields"] == {"dataFields": ["*"]}

    calls: list[tuple[str, str]] = []

    def fake_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
        calls.append((method, url))
        if len(calls) == 1:
            return 503, b'{"error":{"message":"transient unavailable projects/secret-proj-99"}}'
        if "fail403" in url:
            num_ref = "projects/" + ("7" * 6)
            return 403, f'{{"error":{{"message":"denied on secret-proj-99 and {num_ref}"}}}}'.encode("utf-8")
        return 200, json.dumps({"results": [{"results": []}]}).encode("utf-8")

    client = Vvs2Client(
        settings,
        token_provider=lambda: "tok-test",
        transport=fake_transport,
        sleep_fn=lambda _s: None,
    )
    resp, attempts = client.request("POST", f"{res.collection_name}/dataObjects:batchSearch", json_body=search_req)
    assert resp == {"results": [{"results": []}]}
    assert attempts == 2
    assert len(calls) == 2

    try:
        client.request("GET", f"{res.collection_name}/fail403")
    except Vvs2HttpError as exc:
        msg = str(exc)
        assert "secret-proj-99" not in msg
        assert ("projects/" + ("7" * 6)) not in msg
        assert "${GCP_PROJECT_ID}" in msg
        assert "${GCP_PROJECT_NUMBER}" in msg
    else:
        raise AssertionError("expected Vvs2HttpError on 403")


def test_vertex_vector_v2_engine_runs_hybrid_scann_rrf_rerank_and_synthesizes() -> None:
    from app.engines.vertex_vector_v2 import VertexVectorSearchV2Engine

    settings = live_settings(
        gcp_project_id="secret-proj-99",
        enabled_levels=("vertex_vector_v2",),
        vvs2_location="us-central1",
        vvs2_collection="rag-arena-ephem-vvs2",
        vvs2_index_id="dense-scann",
        vvs2_top_k=25,
        rerank_top_n=2,
    )
    fake_genai = FakeGenaiClient(answer="CLIN-0004-AB rate is $148.00/hr [chunk_id=W912DY_h_02] [doc=Amend_01.pdf, p.6].")
    fake_ranker = _fake_ranking_client("secret-proj-99", [("W912DY_h_02", 0.94), ("W912DY_h_01", 0.61)])

    class _FakeVvs2Client:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

        def request(self, method: str, path: str, *, params: dict[str, str] | None = None, json_body: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
            self.requests.append((method, path, json_body))
            return (
                {
                    "results": [
                        {
                            "results": [
                                {
                                    "distance": 0.0312,
                                    "searchResultMetadata": {
                                        "rrfRankerResult": {"rank": 1, "score": 0.0312},
                                        "searchDistances": [
                                            {"rank": 1, "distance": 0.1421},
                                            {"searchIndex": 1, "rank": 2, "distance": 8.75},
                                        ],
                                    },
                                    "dataObject": {
                                        "dataObjectId": "chk-aaaaaaaaaaaaaaaaaaaaaaaa",
                                        "data": {
                                            "chunk_id": "W912DY_h_01",
                                            "doc_name": "Solicitation_01.pdf",
                                            "solicitation_id": "W912DY-24-R-0001",
                                            "section_id": "B.4",
                                            "content": "Base CLIN-0004-AB rate was $140.00/hr.",
                                            "element_type": "narrative_text",
                                            "page_number": 3,
                                            "amendment_number": 0,
                                        },
                                    },
                                },
                                {
                                    "distance": 0.0298,
                                    "dataObject": {
                                        "dataObjectId": "chk-bbbbbbbbbbbbbbbbbbbbbbbb",
                                        "data": {
                                            "chunk_id": "W912DY_h_02",
                                            "doc_name": "Amend_01.pdf",
                                            "solicitation_id": "W912DY-24-R-0001",
                                            "section_id": "B.4",
                                            "content": "Amendment 1 updates CLIN-0004-AB rate to $148.00/hr.",
                                            "element_type": "table",
                                            "page_number": 6,
                                            "amendment_number": 1,
                                        },
                                    },
                                },
                            ]
                        }
                    ]
                },
                1,
            )

    fake_vvs2 = _FakeVvs2Client()
    eng = VertexVectorSearchV2Engine(
        settings=settings,
        synthesis_model=spec_for_settings_model(settings, "gemini-4-argon"),
        ranking_client=fake_ranker,
        vvs2_client=fake_vvs2,
    )
    emitter = _CaptureEmitter()

    with fake_clients(genai=fake_genai):
        result = asyncio.run(
            eng.run_query(
                "What is the CLIN-0004-AB labor rate?",
                question_id="q01",
                solicitation_filter="W912DY-24-R-0001",
                emitter=emitter,
            )
        )

    assert result.engine_id == "vertex_vector_v2"
    assert [c.chunk_id for c in result.retrieved_chunks] == ["W912DY_h_02", "W912DY_h_01"]
    assert result.citations[0].doc_name == "Amend_01.pdf"
    assert result.cost_usd > 0

    frames = emitter.drain()
    completed = [f["stage"] for f in frames if f.get("event") == "stage_completed"]
    assert completed == ["query_prep", "embed_query", "retrieve", "rerank", "synthesize"]
    retrieve_frame = next(f for f in frames if f.get("event") == "stage_completed" and f.get("stage") == "retrieve")
    cands_out = retrieve_frame["payload"]["candidates"]
    assert cands_out[0]["vector_rank"] == 1
    assert cands_out[0]["lexical_rank"] == 2
    assert cands_out[0]["vector_distance"] == 0.1421
    assert cands_out[0]["rrf_score"] == 0.0312
    raw_dump = json.dumps(frames)
    assert "secret-proj-99" not in raw_dump


def test_vertex_vector_v2_engine_fails_loud_on_upload_scope_empty_collection_zero_hits_and_replay() -> None:
    from app.engines.vertex_vector_v2 import VertexVectorSearchV2Engine

    settings = live_settings(
        enabled_levels=("vertex_vector_v2",),
        vvs2_location="us-central1",
        vvs2_collection="rag-arena-ephem-vvs2",
    )

    class _EmptyVvs2:
        def request(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], int]:
            return {"results": [{"results": []}]}, 1

    eng = VertexVectorSearchV2Engine(settings=settings, vvs2_client=_EmptyVvs2())

    try:
        asyncio.run(eng.run_query("rate?", question_id=None, solicitation_filter="upl_abcdef012345", emitter=_CaptureEmitter()))
    except ValueError as exc:
        assert "upl_" in str(exc) or "upload" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError on upl_* scope")

    unconf = VertexVectorSearchV2Engine(
        settings=live_settings(enabled_levels=("vertex_vector_v2",), vvs2_collection=""),
        vvs2_client=_EmptyVvs2(),
    )
    try:
        asyncio.run(unconf.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
    except (RuntimeError, ValueError) as exc:
        assert "VVS2_COLLECTION" in str(exc)
    else:
        raise AssertionError("expected error when VVS2_COLLECTION is unset")

    with fake_clients(genai=FakeGenaiClient()):
        try:
            asyncio.run(eng.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
        except RuntimeError as exc:
            assert "0 candidates" in str(exc) or "ephemeral_vvs2" in str(exc)
        else:
            raise AssertionError("expected RuntimeError on 0 candidates")

    replay_eng = VertexVectorSearchV2Engine(
        settings=live_settings(demo_mode="replay", enabled_levels=("vertex_vector_v2",), vvs2_collection="rag-arena-ephem-vvs2"),
        vvs2_client=_EmptyVvs2(),
    )
    try:
        asyncio.run(replay_eng.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
    except RuntimeError as exc:
        assert "replay" in str(exc).lower()
    else:
        raise AssertionError("expected RuntimeError in DEMO_MODE=replay")
