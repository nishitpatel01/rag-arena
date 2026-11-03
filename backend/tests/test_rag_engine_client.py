"""Step 1 tests for Level 6 Vertex AI RAG Engine client, config bounds, redaction, and safe provisioning."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.core.pricing import PRICE_TABLE
from app.recording.redaction import assert_redacted, redact_value, redaction_tokens
from fakes import live_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_provision_script():
    spec = importlib.util.spec_from_file_location(
        "provision_rag_engine", _REPO_ROOT / "scripts" / "provision_rag_engine.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Send:
    """Deterministic HTTP transport double recording every call and replaying queued (status, json) pairs."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        status, body = self.responses.pop(0)
        return httpx.Response(status, text=json.dumps(body), request=httpx.Request(method, url))


def test_rag_engine_config_defaults_and_boundary_validation() -> None:
    defaults = get_settings({"DEMO_MODE": "replay"})
    assert defaults.rag_engine_location == "us-central1"
    assert defaults.rag_engine_corpus_id == ""
    assert defaults.rag_engine_embedding_model == "text-embedding-005"
    assert defaults.rag_engine_top_k == 10
    assert defaults.rag_engine_chunk_size == 512
    assert defaults.rag_engine_chunk_overlap == 100
    assert defaults.rag_engine_timeout_seconds == 30
    assert PRICE_TABLE["text-embedding-005"].input_usd_per_1m == 0.10

    valid = live_settings(
        rag_engine_location="us-east4",
        rag_engine_corpus_id="corp_987654",
        rag_engine_top_k=5,
        rag_engine_chunk_size=1024,
        rag_engine_chunk_overlap=128,
        rag_engine_timeout_seconds=45,
    )
    assert valid.rag_engine_corpus_id == "corp_987654"

    with pytest.raises(ValueError):
        live_settings(rag_engine_location="global")
    with pytest.raises(ValueError):
        live_settings(rag_engine_corpus_id="projects/acme/locations/us-central1/ragCorpora/123")
    with pytest.raises(ValueError):
        live_settings(rag_engine_chunk_size=512, rag_engine_chunk_overlap=512)
    with pytest.raises(ValueError):
        live_settings(rag_engine_top_k=0)
    with pytest.raises(ValueError):
        live_settings(rag_engine_top_k=21)
    with pytest.raises(ValueError):
        live_settings(rag_engine_timeout_seconds=1)


def test_redaction_tokens_scrubs_rag_engine_corpus_id() -> None:
    s = live_settings(gcp_project_id="acme-proj-1", rag_engine_corpus_id="461168601842")
    tokens = redaction_tokens(s, home="/tmp/fake-home")
    raw = {
        "corpus": "projects/acme-proj-1/locations/us-central1/ragCorpora/461168601842",
        "op": "projects/76543/locations/us-central1/operations/9911",
    }
    cleaned = redact_value(raw, tokens)
    assert cleaned["corpus"] == "projects/${GCP_PROJECT_ID}/locations/us-central1/ragCorpora/${RAG_ENGINE_CORPUS_ID}"
    assert_redacted(cleaned, tokens)


def test_vertex_rag_client_blocks_unprovisioned_and_redacts_errors() -> None:
    from app.core.rag_engine_client import RagEngineHTTPError, RagEngineResources, VertexRagClient, aiplatform_host

    assert aiplatform_host("us-central1") == "https://us-central1-aiplatform.googleapis.com"
    with pytest.raises(ValueError):
        aiplatform_host("global")

    res = RagEngineResources("acme-proj", "us-central1", "corp_12345")
    assert res.corpus_label == "ragCorpora/corp_12345"

    send = _Send()
    client = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send,
    )
    # Regional singleton safety invariant: unprovisioned is blocked before any HTTP call is made.
    with pytest.raises(ValueError):
        client.request("PATCH", res.engine_config_path, body={"ragManagedDbConfig": {"unprovisioned": {}}})
    with pytest.raises(ValueError):
        client.request(
            "PATCH", res.engine_config_path, body={"ragManagedDbConfig": {"spanner": {"unprovisioned": {}}}}
        )
    assert len(send.calls) == 0

    # Retry 503 once -> 200; non-retryable 403 fails immediately and redacts project id + project number.
    send_retry = _Send((503, {"error": "unavailable"}), (200, {"ragCorpora": [{"name": "c1"}]}))
    client_retry = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send_retry,
    )
    doc, attempts = client_retry.request("GET", res.corpora_path)
    assert attempts == 2 and doc["ragCorpora"] == [{"name": "c1"}]

    send_403 = _Send((403, {"error": {"code": 403, "message": "Denied on projects/acme-proj (projects/76543)"}}))
    client_403 = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send_403,
    )
    try:
        client_403.request("GET", res.corpora_path)
        raise AssertionError("expected RagEngineHTTPError")
    except RagEngineHTTPError as exc:
        assert exc.status_code == 403
        assert "acme-proj" not in str(exc)
        assert "${GCP_PROJECT_ID}" in str(exc)
        assert "projects/${GCP_PROJECT_NUMBER}" in str(exc)
    assert len(send_403.calls) == 1


def test_delete_corpus_if_exists_handles_404_and_polls_lro() -> None:
    from app.core.rag_engine_client import RagEngineResources, VertexRagClient

    res = RagEngineResources("acme-proj", "us-central1", "corp_upl")
    send = _Send(
        (404, {"error": {"code": 404, "message": "not found"}}),
        (200, {"name": "projects/76543/locations/us-central1/operations/del_1", "done": False}),
        (200, {"name": "projects/76543/locations/us-central1/operations/del_1", "done": True, "response": {}}),
    )
    client = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send,
    )
    assert client.delete_corpus_if_exists(res.corpus_path) is False
    assert client.delete_corpus_if_exists(res.corpus_path) is True
    assert [c["method"] for c in send.calls] == ["DELETE", "DELETE", "GET"]


def test_provision_reuses_active_spanner_basic_without_patching_and_creates_corpus_ignoring_neighbors() -> None:
    from app.core.rag_engine_client import RagEngineResources, VertexRagClient

    prov = _load_provision_script()
    res = RagEngineResources("acme-proj", "us-central1")
    send = _Send(
        # 1. GET ragEngineConfig -> already active on spanner.basic
        (200, {"name": res.engine_config_path, "ragManagedDbConfig": {"basic": {}, "spanner": {"basic": {}}}}),
        # 2. GET ragCorpora -> unrelated travel_planner exists; ours is absent
        (
            200,
            {
                "ragCorpora": [
                    {
                        "name": "projects/76543/locations/us-central1/ragCorpora/11111",
                        "displayName": "travel_planner",
                        "corpusStatus": {"state": "ACTIVE"},
                    }
                ]
            },
        ),
        # 3. POST ragCorpora -> LRO
        (200, {"name": "projects/76543/locations/us-central1/operations/create_1", "done": False}),
        # 4. GET LRO -> done with created RagCorpus
        (
            200,
            {
                "name": "projects/76543/locations/us-central1/operations/create_1",
                "done": True,
                "response": {
                    "name": "projects/76543/locations/us-central1/ragCorpora/998877",
                    "displayName": "rag-arena-procurement",
                },
            },
        ),
        # 5. Re-run GET ragCorpora -> both exist -> idempotent 'exists'
        (
            200,
            {
                "ragCorpora": [
                    {
                        "name": "projects/76543/locations/us-central1/ragCorpora/11111",
                        "displayName": "travel_planner",
                        "corpusStatus": {"state": "ACTIVE"},
                    },
                    {
                        "name": "projects/76543/locations/us-central1/ragCorpora/998877",
                        "displayName": "rag-arena-procurement",
                        "corpusStatus": {"state": "ACTIVE"},
                    },
                ]
            },
        ),
    )
    client = VertexRagClient(
        project_id="acme-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send,
    )
    db_state = prov.ensure_engine_config(client, res)
    assert db_state == "reused:spanner_basic"
    assert [c["method"] for c in send.calls] == ["GET"]

    corpus_state, corpus_id = prov.ensure_corpus(client, res, embedding_model="text-embedding-005")
    assert (corpus_state, corpus_id) == ("created", "998877")
    post_body = send.calls[2]["json"]
    assert post_body["displayName"] == "rag-arena-procurement"
    assert post_body["vectorDbConfig"]["ragManagedDb"] == {"knn": {}}
    assert post_body["vectorDbConfig"]["ragEmbeddingModelConfig"]["vertexPredictionEndpoint"]["endpoint"].endswith(
        "/publishers/google/models/text-embedding-005"
    )

    again_state, again_id = prov.ensure_corpus(client, res, embedding_model="text-embedding-005")
    assert (again_state, again_id) == ("exists", "998877")


def test_provision_initializes_serverless_mode_only_when_unprovisioned() -> None:
    from app.core.rag_engine_client import RagEngineResources, VertexRagClient

    prov = _load_provision_script()
    res = RagEngineResources("fresh-proj", "us-central1")
    send = _Send(
        (200, {"name": res.engine_config_path, "ragManagedDbConfig": {"unprovisioned": {}}}),
        (200, {"name": "projects/76543/locations/us-central1/operations/cfg_1", "done": True, "response": {}}),
    )
    client = VertexRagClient(
        project_id="fresh-proj",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=15.0,
        sleep=lambda _: None,
        send=send,
    )
    db_state = prov.ensure_engine_config(client, res)
    assert db_state == "initialized:serverless"
    assert send.calls[1]["method"] == "PATCH"
    assert send.calls[1]["json"]["ragManagedDbConfig"] == {"serverless": {}}
