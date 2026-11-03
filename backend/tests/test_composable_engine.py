"""Step 2 tests: modular ``ComposableRAGEngine``, generalized SQL builders, ``pipelines`` SSE & ``query_runs`` provenance."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from google.genai import types
import httpx
import pytest
from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.core.models_catalog import load_catalog
from app.core.pipeline_spec import PipelineSpec, QuerySlots, preset_spec
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.rag_engine_client import VertexRagClient
from app.core.ranking import RankingClient
from app.core.telemetry import RecordingStageEmitter
from app.engines.composable import ComposableRAGEngine, build_composable_engine
from app.engines.level1_naive import build_vector_search_sql
from app.engines.replay_engine import ReplayUnavailableError
from app.ingestion.bq_indexer import (
    BQ_TIERS,
    build_amendment_lookup_sql,
    build_hybrid_rrf_sql,
    build_lexical_search_sql,
    build_tables_ddl,
)
from app.ingestion.live_backends import _QUERY_RUN_SCHEMA
from app.main import create_app
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run
from test_level4_agentic import StubLlm, _call
from test_uploads_api import _ALICE, _BOB, _client, _pdf, _upload, _workspace


def _events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


class _FakeRankPost:
    def __init__(self, scores: dict[str, float]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._scores = scores

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        ids = [r["id"] for r in kwargs["json"]["records"]]
        ranked = sorted(ids, key=lambda rid: self._scores.get(rid, 0.0), reverse=True)[: kwargs["json"]["topN"]]
        body = {"records": [{"id": rid, "score": self._scores.get(rid, 0.0)} for rid in ranked]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _rank_client(scores: dict[str, float]) -> tuple[RankingClient, _FakeRankPost]:
    post = _FakeRankPost(scores)
    client = RankingClient(
        project_id="test-project",
        model="semantic-ranker-default@latest",
        attempt_timeout_seconds=5.0,
        budget_seconds=10.0,
        token_provider=lambda: "fake-token",
        sleep=lambda s: None,
        post=post,
    )
    return client, post


def _rag_client(contexts: list[dict[str, Any]]) -> VertexRagClient:
    def _send(method: str, url: str, **kw: Any) -> httpx.Response:
        assert method == "POST" and ":retrieveContexts" in url
        return httpx.Response(200, json={"contexts": {"contexts": contexts}}, request=httpx.Request(method, url))

    return VertexRagClient(
        project_id="test-project",
        location="us-central1",
        token_provider=lambda: "tok",
        attempt_timeout_seconds=5.0,
        budget_seconds=10.0,
        sleep=lambda s: None,
        send=_send,
    )


def test_sql_builders_generalize_across_bq_tiers_and_query_runs_schema_has_pipeline_columns() -> None:
    assert BQ_TIERS == ("naive", "hybrid", "multimodal")

    # Vector search across multimodal + scoped/unscoped
    mm_sql = build_vector_search_sql(
        "demo-proj",
        "rag_arena",
        "document_chunks_v3",
        top_k=12,
        include_solicitation_filter=False,
        tier_profile="multimodal",
        embedding_column="embedding_multimodal",
    )
    assert "tier_profile = 'multimodal'" in mm_sql
    assert "NOT STARTS_WITH(solicitation_id, 'upl_')" in mm_sql
    assert "'embedding_multimodal'" in mm_sql
    assert "LIMIT 12;" in mm_sql

    # Lexical-only SQL: zero @query_embedding, parameterized SEARCH + @lexical_terms
    lex_sql = build_lexical_search_sql(
        "demo-proj",
        "rag_arena",
        "document_chunks_v3",
        top_k=8,
        include_solicitation_filter=True,
        tier_profile="hybrid",
    )
    assert "@query_embedding" not in lex_sql
    assert "VECTOR_SEARCH" not in lex_sql
    assert "SEARCH(content, REGEXP_REPLACE(@lexical_query" in lex_sql
    assert "@lexical_terms" in lex_sql
    assert "solicitation_id = @solicitation_id" in lex_sql
    assert "tier_profile = 'hybrid'" in lex_sql
    assert "LIMIT 8;" in lex_sql
    with pytest.raises(ValueError, match="tier_profile"):
        build_lexical_search_sql("demo-proj", "rag_arena", "t", tier_profile="bad' OR 1=1")

    # Hybrid RRF accepts naive tier too
    naive_rrf = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", tier_profile="naive")
    assert naive_rrf.count("tier_profile = 'naive'") == 3

    # Amendment lookup accepts configurable BQ tier
    amd_sql = build_amendment_lookup_sql("demo-proj", "rag_arena", "document_chunks_v3", tier_profile="hybrid")
    assert "tier_profile = 'hybrid'" in amd_sql

    # query_runs DDL & loader schema carry pipeline_id + pipeline_spec
    ddl = build_tables_ddl("demo-proj", "rag_arena", "document_chunks_v3")
    assert "pipeline_id STRING" in ddl and "pipeline_spec JSON" in ddl
    schema_cols = {name: ftype for name, ftype, _ in _QUERY_RUN_SCHEMA}
    assert schema_cols["pipeline_id"] == "STRING"
    assert schema_cols["pipeline_spec"] == "JSON"


def test_composable_lexical_bm25_omits_embed_query_stage_and_genai_embed_call() -> None:
    lex_rows = [
        {
            "chunk_id": "solicitation_01_doc1_p1_hybrid",
            "solicitation_id": "solicitation_01",
            "doc_name": "04_Pricing_Schedule_CLINs.pdf",
            "doc_type": "PRICING_CLIN",
            "element_type": "table",
            "section_id": "CLIN-0004-AB",
            "amendment_number": 0,
            "effective_date": "2026-01-15",
            "amends_section": None,
            "page_number": 1,
            "bbox": {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.5},
            "content": "CLIN-0004-AB rate $210.00/hr.",
            "layout_source": "pymupdf",
            "bbox_source": "pdf_text_layer",
            "lexical_score": 4,
            "lexical_rank": 1,
        }
    ]
    spec = PipelineSpec(
        index_profile="hybrid",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="lexical_bm25",
            candidate_k=8,
            top_k=4,
            reranker="none",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    settings = live_settings(enabled_levels=("level2_hybrid",))
    emitter = RecordingStageEmitter(run_id="run_lex", engine=spec.pipeline_id)
    with fake_clients(FakeGenaiClient(), FakeBigQueryClient(rows=lex_rows)) as (genai, bq):
        engine = ComposableRAGEngine(spec, settings=settings)
        result = run(
            engine.run_query(
                "What is the CLIN-0004-AB rate under the SLA?",
                question_id="q_test",
                solicitation_filter=None,
                emitter=emitter,
            )
        )
    assert [r.stage for r in emitter.records] == ["query_prep", "retrieve", "synthesize"]
    assert [c[0] for c in genai.calls] == ["generate_content"]  # zero embed_content calls!
    assert len(bq.queries) == 1 and "VECTOR_SEARCH" not in bq.queries[0][0]
    assert result.flags.reranker_used is False
    assert result.usage.embedding_tokens == 0


def test_composable_cross_level_rag_engine_plus_ranking_api_and_authority_order() -> None:
    contexts = [
        {
            "sourceUri": "gs://acme-bucket/sample_docs/solicitation_01_cloud/04_Pricing_Schedule.pdf",
            "sourceDisplayName": "04_Pricing_Schedule.pdf",
            "text": "Base CLIN-0004-AB labor rate is $210.00/hr.",
            "score": 0.12,
            "chunk": {"pageSpan": {"firstPage": 2, "lastPage": 2}},
        },
        {
            "sourceUri": "gs://acme-bucket/sample_docs/solicitation_01_cloud/06_Amendment_0001.pdf",
            "sourceDisplayName": "06_Amendment_0001.pdf",
            "text": "Amendment 0001 supersedes CLIN-0004-AB labor rate to $225.00/hr.",
            "score": 0.25,
            "chunk": {"pageSpan": {"firstPage": 1, "lastPage": 1}},
        },
    ]
    spec = PipelineSpec(
        index_profile="rag_engine",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="managed_retrieval",
            candidate_k=10,
            top_k=1,
            reranker="vertex_ranking_api",
            context_order="authority_amendment_first",
            reasoning_mode="single_pass",
        ),
    )
    settings = live_settings(
        enabled_levels=("rag_engine",),
        rag_engine_corpus_id="54321",
        gcs_corpus_bucket="acme-bucket",
    )
    # Give the 2nd context (Amendment) the higher cross-encoder score so top_k=1 selects it!
    rank_client, rank_post = _rank_client(
        {
            "rag_f1_p2_c1": 0.31,
            "rag_f2_p1_c2": 0.94,
        }
    )
    emitter = RecordingStageEmitter(run_id="run_rag_rerank", engine=spec.pipeline_id)
    with fake_clients(FakeGenaiClient()) as (genai, _):
        engine = ComposableRAGEngine(
            spec,
            settings=settings,
            ranking_client=rank_client,
            rag_client=_rag_client(contexts),
        )
        result = run(
            engine.run_query(
                "What is the governing CLIN-0004-AB rate?",
                question_id="q_test",
                solicitation_filter=None,
                emitter=emitter,
            )
        )
    assert [r.stage for r in emitter.records] == ["query_prep", "retrieve", "rerank", "synthesize"]
    assert len(rank_post.calls) == 1
    assert len(result.retrieved_chunks) == 1
    assert result.retrieved_chunks[0].doc_name == "06_Amendment_0001.pdf"
    assert result.retrieved_chunks[0].score == 0.94
    assert result.flags.reranker_used is True
    expected_cost = round(cost_from_usage(result.usage, "gemini-4-argon") + rank_cost_usd(rank_billed_counts(2)), 8)
    assert result.cost_usd == expected_cost
    synth_prompt = str(genai.calls[0][1]["contents"])
    assert "[BASE ·" in synth_prompt or "[AMENDMENT" in synth_prompt  # authority_amendment_first formatting applied


def test_composable_slim_adk_agent_registers_only_selected_tools_and_pins_upload_scope() -> None:
    ws = "upl_0123456789ab"
    lex_rows = [
        {
            "chunk_id": f"{ws}_doc1_p1_hybrid",
            "solicitation_id": ws,
            "doc_name": "Uploaded_Pricing.pdf",
            "doc_type": "PRICING_CLIN",
            "element_type": "table",
            "section_id": "CLIN-0004-AB",
            "amendment_number": 0,
            "effective_date": "2026-01-15",
            "amends_section": None,
            "page_number": 1,
            "bbox": {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.5},
            "content": "CLIN-0004-AB rate is $200/hr for 10 hours.",
            "layout_source": "pymupdf",
            "bbox_source": "pdf_text_layer",
            "lexical_score": 4,
            "lexical_rank": 1,
        }
    ]
    spec = PipelineSpec(
        index_profile="hybrid",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="lexical_bm25",
            candidate_k=5,
            top_k=3,
            reranker="none",
            context_order="score_order",
            reasoning_mode="adk_agent",
            agent_tools=("search_procurement_corpus", "calculate_contract_pricing"),
        ),
    )
    settings = live_settings(enabled_levels=("level2_hybrid", "level4_agentic"))
    llm = StubLlm(
        script=[
            [
                _call("search_procurement_corpus", {"query": "CLIN-0004-AB rate", "solicitation_id": ""}, "fc1"),
                _call(
                    "calculate_contract_pricing",
                    {"expression": "rate * hrs", "variables": {"rate": 200, "hrs": 10}},
                    "fc2",
                ),
            ],
            [types.Part(text=f"Total is $2,000.\nCITED_CHUNK_IDS: {ws}_doc1_p1_hybrid")],
        ]
    )
    captured_tool_names: list[str] = []

    from google.adk.agents import LlmAgent as RealLlmAgent

    def _fake_build_agent(model_spec, cfg, tools, *, enabled_tools=()):  # noqa: ANN001
        captured_tool_names.extend(t.__name__ for t in tools)
        return RealLlmAgent(name="procurement_agent", model=llm, instruction="test", tools=tools)

    emitter = RecordingStageEmitter(run_id="run_slim_agent", engine=spec.pipeline_id)
    with (
        fake_clients(FakeGenaiClient(), FakeBigQueryClient(rows=lex_rows)) as (genai, bq),
        patch("app.engines.composable.build_agent", side_effect=_fake_build_agent),
    ):
        engine = ComposableRAGEngine(spec, settings=settings)
        result = run(
            engine.run_query(
                "Compute 10 hours of CLIN-0004-AB.",
                question_id=None,
                solicitation_filter=ws,
                emitter=emitter,
            )
        )
    assert captured_tool_names == ["search_procurement_corpus", "calculate_contract_pricing"]
    assert [r.stage for r in emitter.records] == ["query_prep", "agent_turn", "synthesize"]
    assert genai.calls == []  # lexical_bm25 inside agent tool made 0 embed_content calls!
    assert result.flags.adk_runner_used is True and result.flags.reranker_used is False
    assert "$2,000" in result.answer


def test_composable_replay_serves_presets_and_fails_loud_on_unrecorded_custom_spec() -> None:
    replay_cfg = ArenaSettings(
        demo_mode="replay",
        enabled_levels=("level1_naive", "level2_hybrid"),
        allow_dev_fixtures=True,
        replay_speed=100.0,
    )
    preset = preset_spec("level1_naive", replay_cfg)
    preset_engine = build_composable_engine(preset, replay_cfg)
    emitter = RecordingStageEmitter(run_id="run_rep_ok", engine="slot_1")
    res = run(preset_engine.run_query("x", question_id="dev_q01", solicitation_filter=None, emitter=emitter))
    assert res.engine_id == "level1_naive" and res.answer

    custom = PipelineSpec(
        index_profile="hybrid",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="lexical_bm25",
            candidate_k=10,
            top_k=5,
            reranker="none",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    custom_engine = build_composable_engine(custom, replay_cfg)
    with pytest.raises(ReplayUnavailableError, match="Live mode"):
        run(
            custom_engine.run_query(
                "x",
                question_id="dev_q01",
                solicitation_filter=None,
                emitter=RecordingStageEmitter(run_id="run_rep_err", engine="slot_2"),
            )
        )


def test_http_presets_validate_and_ablation_battle_stream_collision_free_with_query_runs_provenance() -> None:
    class _Writer:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def write_query_run(self, row: dict[str, Any]) -> None:
            self.rows.append(row)

    writer = _Writer()
    settings = live_settings(enabled_levels=("level1_naive", "level2_hybrid", "rag_engine"), rag_engine_corpus_id="54321")
    app = create_app(settings=settings, query_run_writer=writer)

    with fake_clients(), TestClient(app) as client:
        # 1. GET /api/v1/pipelines/presets
        presets_resp = client.get("/api/v1/pipelines/presets")
        assert presets_resp.status_code == 200
        presets_body = presets_resp.json()
        assert len(presets_body["presets"]) == 6
        assert presets_body["schema"]["index_profiles"] == [
            "naive",
            "hybrid",
            "multimodal",
            "vertex_ai_search",
            "rag_engine",
        ]

        # 2. POST /api/v1/pipelines/validate (valid + invalid)
        valid_resp = client.post(
            "/api/v1/pipelines/validate",
            json={
                "index_profile": "rag_engine",
                "slots": {
                    "query_transform": "acronym",
                    "retrieval_mode": "managed_retrieval",
                    "candidate_k": 15,
                    "top_k": 5,
                    "reranker": "vertex_ranking_api",
                    "context_order": "authority_amendment_first",
                    "reasoning_mode": "single_pass",
                },
            },
        )
        assert valid_resp.status_code == 200
        v_doc = valid_resp.json()
        assert v_doc["valid"] is True and v_doc["preset_level_id"] is None
        assert "rank" in {n["id"] for n in v_doc["descriptor"]["nodes"]}

        bad_resp = client.post(
            "/api/v1/pipelines/validate",
            json={
                "index_profile": "rag_engine",
                "slots": {
                    "query_transform": "acronym",
                    "retrieval_mode": "lexical_bm25",
                    "candidate_k": 10,
                    "top_k": 5,
                    "reranker": "none",
                    "context_order": "score_order",
                    "reasoning_mode": "single_pass",
                },
            },
        )
        assert bad_resp.status_code == 400 and bad_resp.json()["valid"] is False

        # 3. POST /api/v1/battle/stream with 2 recipes on the SAME preset (level1_naive) + DIFFERENT synthesis models!
        base_dict = preset_spec("level1_naive", settings).to_dict()
        r1 = {**base_dict, "slot_id": "slot_argon", "synthesis_model": "gemini-4-argon", "label": "L1 · Argon"}
        r2 = {**base_dict, "slot_id": "slot_flash", "synthesis_model": "gemini-3.8-flash", "label": "L1 · Flash"}
        stream_resp = client.post(
            "/api/v1/battle/stream",
            json={
                "query": "What is the base hourly rate for CLIN-0004-AB?",
                "question_id": "q_exact_clin_lookup",
                "pipelines": [r1, r2],
            },
        )
        assert stream_resp.status_code == 200
        evs = _events(stream_resp.text)
        assert evs[0]["event"] == "run_started" and evs[0]["engines"] == ["slot_argon", "slot_flash"]
        assert set(evs[-1]["engines_completed"]) == {"slot_argon", "slot_flash"}

        answers = {e["engine"]: e for e in evs if e["event"] == "answer_complete"}
        evals = {e["engine"]: e for e in evs if e["event"] == "eval_complete"}
        assert set(answers) == {"slot_argon", "slot_flash"}
        assert set(evals) == {"slot_argon", "slot_flash"}
        assert answers["slot_argon"]["usage"]["model"] == "gemini-4-argon"
        assert answers["slot_flash"]["usage"]["model"] == "gemini-3.8-flash"

        # Verify query_runs provenance rows carry canonical engine + pipeline_id + pipeline_spec JSON
        assert len(writer.rows) == 2
        by_synth = {row["synthesis_model"]: row for row in writer.rows}
        assert set(by_synth) == {"gemini-4-argon", "gemini-3.8-flash"}
        for synth_id, row in by_synth.items():
            assert row["engine"] == "level1_naive"
            assert row["pipeline_id"] == base_dict["pipeline_id"]
            assert row["pipeline_spec"]["pipeline_id"] == base_dict["pipeline_id"]
            assert row["pipeline_spec"]["synthesis_model"] == synth_id

        # 4. Guardrails: mutually exclusive engines+pipelines, >4 pipelines, duplicate slot_id, disabled backing level
        both = client.post("/api/v1/battle/stream", json={"query": "q", "engines": ["level1_naive"], "pipelines": [r1]})
        assert both.status_code == 400
        dup = client.post("/api/v1/battle/stream", json={"query": "q", "pipelines": [r1, r1]})
        assert dup.status_code == 400
        too_many = client.post(
            "/api/v1/battle/stream",
            json={"query": "q", "pipelines": [{**base_dict, "slot_id": f"s{i}"} for i in range(5)]},
        )
        assert too_many.status_code == 400
        disabled = client.post(
            "/api/v1/battle/stream",
            json={"query": "q", "pipelines": [preset_spec("level3_multimodal", settings).to_dict()]},
        )
        assert disabled.status_code == 400 and "enabled" in disabled.json()["error"].lower()


def test_pipelines_battle_stream_enforces_upload_workspace_ownership_and_readiness() -> None:
    client, _, store = _client()
    with client:
        ws = _workspace(client, _ALICE)
        _, fin = _upload(client, store, ws, _pdf(pages=1), name="Private.pdf", headers=_ALICE)
        assert fin.status_code == 200
        recipe = {**preset_spec("level1_naive").to_dict(), "slot_id": "slot_1"}

        # Bob gets 404 (IDOR protection)
        idor = client.post(
            "/api/v1/battle/stream",
            headers=_BOB,
            json={"query": "What is in Private.pdf?", "solicitation_filter": ws, "pipelines": [recipe]},
        )
        assert idor.status_code == 404

        # Alice before ingesting level1_naive gets explicit readiness rejection (400)
        unready = client.post(
            "/api/v1/battle/stream",
            headers=_ALICE,
            json={"query": "What is in Private.pdf?", "solicitation_filter": ws, "pipelines": [recipe]},
        )
        assert unready.status_code == 400
        assert "not ingested" in unready.json()["error"]
