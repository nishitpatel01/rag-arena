"""v1.6.4 TDD test suite: Gemini 4 Neon, Cloud SQL pgvector 0.8.5 (Level 5), BigQuery Property Graph GraphRAG (Level 9),
Workbench ADK-on-all-stores & dense-only ANN/PG ablations, and ephemeral Cloud SQL / BQ Graph scripts."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

import httpx
from google.genai import types
import pytest

from app.core.acl import ARCHITECTURE_ACL_CAPABILITIES, resolve_caller_access
from app.core.architectures import CANONICAL_QUERY_STAGES, get_architecture_descriptor
from app.core.config import ALL_LEVEL_IDS, DEFAULT_LIVE_LEVELS, ArenaSettings, get_settings
from app.core.models_catalog import CATALOG_ENTRIES
from app.core.pipeline_spec import (
    INDEX_PROFILES,
    SCANN_RETRIEVAL_MODES,
    PipelineSpec,
    QuerySlots,
    active_query_stages,
    descriptor_for_pipeline,
    preset_spec,
)
from app.core.pricing import PRICE_TABLE, cost_from_usage
from app.core.ranking import RankingClient
from app.core.tco import TCO_SYNTHESIS_MODELS, estimate_enterprise_tco, parse_tco_workload
from app.core.telemetry import RecordingStageEmitter
from app.engines.agent_ledger import AgentLedger
from app.engines.agent_tools import ToolDeps, run_check_amendments
from app.engines.alloydb_scann import AlloyDBScaNNEngine, build_alloydb_vector_search_sql
from app.engines.bigquery_graphrag import BigQueryGraphRAGEngine
from app.engines.cloudsql_pgvector import (
    CloudSQLPgVectorEngine,
    build_cloudsql_hybrid_search_sql,
    build_cloudsql_pgvector_ddl,
    build_cloudsql_vector_search_sql,
)
from app.engines.composable import ComposableRAGEngine
from app.engines.vertex_vector_v2 import VertexVectorSearchV2Engine
from app.ingestion.bq_graph import (
    GRAPH_TABLE_NAMES,
    build_graph_amendment_lookup_gql,
    build_graph_expand_gql,
    build_graph_table_ddl,
    build_property_graph_ddl,
)
from app.recording.redaction import assert_redacted, redact_text, redaction_tokens
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EPHEM_CLOUDSQL = PROJECT_ROOT / "scripts" / "ephemeral_cloudsql.sh"
PROVISION_BQ_GRAPH = PROJECT_ROOT / "scripts" / "provision_bq_graph.py"


def _ranking() -> RankingClient:
    def rank_post(url: str, **kw: Any) -> httpx.Response:
        ids = [r["id"] for r in kw["json"]["records"]]
        return httpx.Response(
            200,
            json={"records": [{"id": rid, "score": round(0.95 - 0.05 * i, 4)} for i, rid in enumerate(ids)]},
            request=httpx.Request("POST", url),
        )

    return RankingClient(
        project_id="test-project",
        model="semantic-ranker-default@latest",
        attempt_timeout_seconds=5.0,
        budget_seconds=10.0,
        token_provider=lambda: "tok",
        sleep=lambda _s: None,
        post=rank_post,
    )


class _FakePgCursor:
    def __init__(self, conn: "_FakePgConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] = []

    def __enter__(self) -> "_FakePgCursor":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def execute(self, sql: Any, params: Any = None) -> None:
        text = sql.as_string(None) if hasattr(sql, "as_string") else str(sql)
        self._conn.executed.append((text, params))
        low = text.lower().strip()
        if low.startswith("set local") or "set_config" in low:
            self._rows = []
            self.description = []
            return
        if "count(*)" in low:
            self._rows = [(len(self._conn.seed_rows),)]
            self.description = [("n",)]
            return
        if "from pg_indexes" in low:
            self._rows = [(f"idx_{self._conn.table}_hnsw", "CREATE INDEX USING hnsw ...")]
            self.description = [("indexname",), ("indexdef",)]
            return
        if "select" in low:
            cols = [
                "chunk_id", "doc_name", "solicitation_id", "doc_type", "page_number", "section_id",
                "content", "element_type", "amendment_number", "effective_date", "amends_section",
                "bbox_json", "vec_distance", "vec_rank", "lex_score", "lex_rank", "rrf_score",
            ]
            self.description = [SimpleNamespace(name=c) for c in cols]
            self._rows = [tuple(r.get(c) for c in cols) for r in self._conn.seed_rows]
            return
        self._rows = []
        self.description = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakePgConn:
    def __init__(self, seed_rows: list[dict[str, Any]], table: str = "document_chunks") -> None:
        self.seed_rows = seed_rows
        self.table = table
        self.executed: list[tuple[str, Any]] = []
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self) -> "_FakePgConn":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def cursor(self) -> _FakePgCursor:
        return _FakePgCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        return None


def _sample_row(cid: str, *, doc_name: str, doc_type: str = "PRICING_CLIN", section_id: str = "CLIN-0004-AB",
                amendment: int = 0, amends_section: str | None = None, content: str = "Rate is $245.00/hr.") -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "solicitation_id": "solicitation_01",
        "doc_name": doc_name,
        "doc_type": doc_type,
        "element_type": "table" if amendment == 0 else "narrative_text",
        "section_id": section_id,
        "amendment_number": amendment,
        "effective_date": "2026-03-15" if amendment else "2026-01-15",
        "amends_section": amends_section,
        "page_number": 1,
        "bbox": {"page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.8, "y1": 0.3},
        "bbox_json": {"page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.8, "y1": 0.3},
        "content": content,
        "layout_source": "documentai_layout",
        "bbox_source": "pdf_text_layer",
        "vector_rank": 1,
        "vector_distance": 0.12,
        "vec_rank": 1,
        "vec_distance": 0.12,
        "lexical_rank": 1,
        "lexical_score": 0.8,
        "lex_rank": 1,
        "lex_score": 0.8,
        "rrf_score": 0.032,
    }


# --- 1. Gemini 4 Neon, 10-level order, ACL capabilities & TCO ---


def test_gemini_4_neon_catalog_pricing_and_tco_10_architectures() -> None:
    assert CATALOG_ENTRIES[0][0] == "gemini-4-neon"
    p = PRICE_TABLE["gemini-4-neon"]
    assert (p.input_usd_per_1m, p.output_usd_per_1m) == (3.0, 15.0)
    assert "gemini-4-neon" in TCO_SYNTHESIS_MODELS

    wl = parse_tco_workload({"synthesis_model": "gemini-4-neon", "monthly_queries": 50_000, "corpus_pages": 10_000})
    est = estimate_enterprise_tco(wl)
    assert len(est.architectures) == 10
    by_id = {a.level_id: a for a in est.architectures}
    assert by_id["cloudsql_pgvector"].order == 5
    assert by_id["cloudsql_pgvector"].standing_monthly_usd > 95.0
    assert by_id["cloudsql_pgvector"].standing_monthly_usd < by_id["alloydb_scann"].standing_monthly_usd
    assert by_id["bigquery_graphrag"].order == 9
    assert by_id["bigquery_graphrag"].standing_monthly_usd == 0.0
    assert by_id["bigquery_graphrag"].idle_mode == "serverless_zero"

    assert ARCHITECTURE_ACL_CAPABILITIES["cloudsql_pgvector"].enforcement_mode == "postgres_rls_policy"
    assert ARCHITECTURE_ACL_CAPABILITIES["bigquery_graphrag"].enforcement_mode == "bigquery_parameterized_rls"
    assert "graph_expand" in CANONICAL_QUERY_STAGES
    assert "bigquery_graphrag" in DEFAULT_LIVE_LEVELS
    assert "cloudsql_pgvector" not in DEFAULT_LIVE_LEVELS
    assert "cloudsql_pgvector" in ALL_LEVEL_IDS


# --- 2. BigQuery Property Graph DDL/GQL + BigQueryGraphRAGEngine ---


def test_bq_graph_ddl_gql_and_rls_builders() -> None:
    ddls = build_graph_table_ddl("demo-proj", "rag_arena", "document_chunks_v3")
    assert len(ddls) == len(GRAPH_TABLE_NAMES) == 5
    joined = "\n".join(ddls)
    assert "NOT STARTS_WITH(solicitation_id, 'upl_')" in joined
    assert "UNNEST(SPLIT(IFNULL(amends_section, ''), ','))" in joined
    assert "allowed_groups" in joined

    pg_ddl = build_property_graph_ddl("demo-proj", "rag_arena", "procurement_graph")
    assert "CREATE OR REPLACE PROPERTY GRAPH `demo-proj.rag_arena.procurement_graph`" in pg_ddl
    for token in ("ContractDoc", "ContractSection", "DEFINES_SECTION", "AMENDS_SECTION", "SUPERSEDES_DOC"):
        assert token in pg_ddl

    gql_open = build_graph_expand_gql("demo-proj", "rag_arena", "document_chunks_v3", "procurement_graph", include_acl_filter=False)
    assert "GRAPH_TABLE(`demo-proj.rag_arena.procurement_graph`" in gql_open or "GRAPH_TABLE (\n    `demo-proj.rag_arena.procurement_graph`" in gql_open
    assert "SUPERSEDES_DOC]->{1,3}" in gql_open
    assert "@caller_groups" not in gql_open

    gql_rls = build_graph_expand_gql("demo-proj", "rag_arena", "document_chunks_v3", "procurement_graph", include_acl_filter=True)
    assert "@caller_groups" in gql_rls
    assert "UNNEST(allowed_groups)" in gql_rls

    amend_gql = build_graph_amendment_lookup_gql("demo-proj", "rag_arena", "document_chunks_v3", "procurement_graph", include_acl_filter=True)
    assert "GRAPH_TABLE" in amend_gql and "`demo-proj.rag_arena.procurement_graph`" in amend_gql
    assert "AMENDS_SECTION" in amend_gql and "@caller_groups" in amend_gql


def test_bigquery_graphrag_engine_happy_path_acl_and_upload_skip() -> None:
    base_row = _sample_row("c_base", doc_name="04_Pricing_Schedule_CLINs.pdf", section_id="CLIN-0004-AB", content="Base rate $210/hr.")
    amd_row = _sample_row(
        "c_amd",
        doc_name="06_Amendment_0001_Scope_Update.pdf",
        doc_type="AMENDMENT",
        section_id="AMD-0001",
        amendment=1,
        amends_section="CLIN-0004-AB",
        content="Amendment 0001 updates CLIN-0004-AB to $245.00/hr.",
    )
    graph_row = {
        **amd_row,
        "edge_type": "AMENDS_SECTION",
        "target_label": "CLIN-0004-AB",
        "hop_count": 1,
    }

    class _GraphBQ(FakeBigQueryClient):
        def query(self, sql: str, job_config: Any = None) -> Any:
            self.queries.append((sql, job_config))
            if "GRAPH_TABLE" in sql:
                return FakeBigQueryClient(rows=[graph_row]).query(sql, job_config)
            return FakeBigQueryClient(rows=[base_row]).query(sql, job_config)

    settings = live_settings(enabled_levels=("bigquery_graphrag",), gemini_fast_model="gemini-4-neon")
    bq = _GraphBQ()
    engine = BigQueryGraphRAGEngine(settings=settings, ranking_client=_ranking())
    emitter = RecordingStageEmitter(run_id="r_graph", engine="bigquery_graphrag")
    with fake_clients(
        genai=FakeGenaiClient(answer="The amended rate is $245.00/hr [06_Amendment_0001_Scope_Update.pdf, p. 1]."),
        bq=bq,
    ):
        result = run(
            engine.run_query(
                "What is the amended CLIN-0004-AB rate?",
                question_id="q_graph_1",
                solicitation_filter=None,
                emitter=emitter,
            )
        )
    assert "$245.00/hr" in result.answer
    assert {c.chunk_id for c in result.retrieved_chunks} == {"c_base", "c_amd"}
    completed = {e["stage"]: e["payload"] for e in emitter.events if e["event"] == "stage_completed"}
    assert "graph_expand" in completed
    assert completed["graph_expand"]["graph_chunks_added"] == 1
    assert completed["graph_expand"]["traversed_edges"][0]["edge_type"] == "AMENDS_SECTION"
    assert any("GRAPH_TABLE" in sql for sql, _ in bq.queries)

    # Upload workspace gracefully skips graph_expand without hitting GRAPH_TABLE
    bq_upl = _GraphBQ()
    engine_upl = BigQueryGraphRAGEngine(settings=settings, ranking_client=_ranking())
    em_upl = RecordingStageEmitter(run_id="r_upl", engine="bigquery_graphrag")
    with fake_clients(genai=FakeGenaiClient(answer="Upload answer."), bq=bq_upl):
        run(
            engine_upl.run_query(
                "What is in my upload?",
                question_id=None,
                solicitation_filter="upl_aabbccddeeff",
                emitter=em_upl,
            )
        )
    skipped = [e for e in em_upl.events if e["event"] == "stage_skipped" and e["stage"] == "graph_expand"]
    assert len(skipped) == 1
    assert not any("GRAPH_TABLE" in sql for sql, _ in bq_upl.queries)

    # ACL restriction zero-row refusal
    bq_empty = FakeBigQueryClient(rows=[])
    engine_rls = BigQueryGraphRAGEngine(settings=settings, ranking_client=_ranking())
    em_rls = RecordingStageEmitter(run_id="r_rls", engine="bigquery_graphrag")
    with fake_clients(genai=FakeGenaiClient(answer="Should not be called"), bq=bq_empty):
        res_rls = run(
            engine_rls.run_query(
                "What is the executive pricing rate?",
                question_id="q_acl_1",
                solicitation_filter=None,
                emitter=em_rls,
                caller_access=resolve_caller_access("engineering_analyst"),
            )
        )
    assert res_rls.retrieved_chunks == []
    assert "authorized" in res_rls.answer.lower() or "access" in res_rls.answer.lower()
    assert any(e["stage"] == "retrieve" and e["payload"].get("acl_access_denied") is True for e in em_rls.events if e["event"] == "stage_completed")


# --- 3. Cloud SQL pgvector 0.8.5 DDL, SQL & Engine ---


def test_cloudsql_pgvector_ddl_sql_and_engine_hybrid_dense_and_rls() -> None:
    ddl = build_cloudsql_pgvector_ddl(table="document_chunks", vector_dim=3072)
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in s for s in ddl)
    assert any("halfvec(3072)" in s for s in ddl)
    assert any("ENABLE ROW LEVEL SECURITY" in s for s in ddl)
    assert any("CREATE POLICY" in s for s in ddl)
    assert "USING hnsw" in ddl[-1] and "halfvec_cosine_ops" in ddl[-1]

    hybrid_sql, hybrid_params = build_cloudsql_hybrid_search_sql(
        table="document_chunks",
        query_vector=[0.1, 0.2],
        query_text="CLIN rate",
        caller_groups=("engineering", "corp_all"),
    )
    assert "websearch_to_tsquery" in hybrid_sql and "::halfvec" in hybrid_sql
    assert "allowed_groups && %(caller_groups)s::text[]" in hybrid_sql
    assert hybrid_params["caller_groups"] == ["engineering", "corp_all"]

    dense_sql, dense_params = build_cloudsql_vector_search_sql(
        table="document_chunks",
        query_vector=[0.1, 0.2],
        caller_groups=("engineering", "corp_all"),
    )
    assert "websearch_to_tsquery" not in dense_sql and "::halfvec" in dense_sql
    assert "allowed_groups && %(caller_groups)s::text[]" in dense_sql
    assert dense_params["caller_groups"] == ["engineering", "corp_all"]

    row = _sample_row("c_sql1", doc_name="06_Amendment_0001_Scope_Update.pdf", doc_type="AMENDMENT", amendment=1)
    conn = _FakePgConn([row])
    settings = live_settings(
        enabled_levels=("cloudsql_pgvector",),
        cloudsql_dsn="postgresql://postgres:secret@127.0.0.1:15433/postgres",
        gemini_fast_model="gemini-4-neon",
    )
    engine = CloudSQLPgVectorEngine(
        settings=settings,
        ranking_client=_ranking(),
        conn_factory=lambda _s: conn,
    )
    em = RecordingStageEmitter(run_id="r_sql", engine="cloudsql_pgvector")
    with fake_clients(genai=FakeGenaiClient(answer="Cloud SQL rate is $245.00/hr.")):
        res = run(engine.run_query("What is the rate?", question_id="q_sql_1", solicitation_filter=None, emitter=em))
    assert "$245.00/hr" in res.answer
    assert any("hnsw.ef_search" in sql for sql, _ in conn.executed)
    assert any("websearch_to_tsquery" in sql for sql, _ in conn.executed)
    assert res.cost_usd == pytest.approx(cost_from_usage(res.usage, "gemini-4-neon") + 0.001)

    # Dense-only ablation skips websearch_to_tsquery
    conn_dense = _FakePgConn([row])
    eng_dense = CloudSQLPgVectorEngine(
        settings=settings,
        ranking_client=_ranking(),
        conn_factory=lambda _s: conn_dense,
    )
    em_dense = RecordingStageEmitter(run_id="r_sql_d", engine="cloudsql_pgvector")
    chunks = run(eng_dense._retrieve_candidates([0.1, 0.2], "rate", None, em_dense, candidate_k=10, dense_only=True))
    assert len(chunks) == 1
    assert not any("websearch_to_tsquery" in sql for sql, _ in conn_dense.executed)

    # ACL zero-row refusal
    conn_empty = _FakePgConn([])
    eng_rls = CloudSQLPgVectorEngine(
        settings=settings,
        ranking_client=_ranking(),
        conn_factory=lambda _s: conn_empty,
    )
    em_rls = RecordingStageEmitter(run_id="r_sql_rls", engine="cloudsql_pgvector")
    with fake_clients(genai=FakeGenaiClient(answer="Must not run")):
        res_rls = run(
            eng_rls.run_query(
                "Executive pricing?",
                question_id="q_sql_rls",
                solicitation_filter=None,
                emitter=em_rls,
                caller_access=resolve_caller_access("engineering_analyst"),
            )
        )
    assert res_rls.retrieved_chunks == []
    assert "authorized" in res_rls.answer.lower() or "access" in res_rls.answer.lower()
    assert any("hnsw.iterative_scan" in sql for sql, _ in conn_empty.executed)
    assert any("set_config('app.caller_groups'" in sql for sql, _ in conn_empty.executed)

    # Missing CLOUDSQL_DSN fails loud
    no_dsn_engine = CloudSQLPgVectorEngine(
        settings=live_settings(enabled_levels=("cloudsql_pgvector",), cloudsql_dsn=""),
        ranking_client=_ranking(),
    )
    with fake_clients(genai=FakeGenaiClient()):
        try:
            run(no_dsn_engine.run_query("test", question_id=None, solicitation_filter=None, emitter=em_rls))
        except RuntimeError as exc:
            assert "CLOUDSQL_DSN" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError when CLOUDSQL_DSN is unset")


# --- 4. Workbench ADK-on-all-stores + dense-only AlloyDB/VVS2 + GQL check_amendments ---


def test_workbench_adk_all_stores_and_dense_only_ablations() -> None:
    assert SCANN_RETRIEVAL_MODES == ("hybrid_rrf", "vector_cosine")
    assert len(INDEX_PROFILES) == 9
    settings = ArenaSettings()
    for profile in INDEX_PROFILES:
        rmode = (
            "managed_retrieval"
            if profile in ("vertex_ai_search", "rag_engine")
            else "hybrid_rrf"
        )
        spec = PipelineSpec(
            index_profile=profile,
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode=rmode,
                candidate_k=10,
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="adk_agent",
                agent_tools=("search_procurement_corpus", "calculate_contract_pricing", "verify_citations"),
            ),
        )
        desc = descriptor_for_pipeline(spec, settings)
        assert "agent" in {n.id for n in desc.nodes}
        assert "agent_turn" in active_query_stages(spec)

    # AlloyDB dense-only SQL omits websearch_to_tsquery
    adb_dense_sql, adb_dense_params = build_alloydb_vector_search_sql(
        table="procurement_chunks_scann",
        query_vector=[0.1, 0.2],
        caller_groups=("engineering", "corp_all"),
    )
    assert "websearch_to_tsquery" not in adb_dense_sql and "allowed_groups && %(caller_groups)s::text[]" in adb_dense_sql
    assert adb_dense_params["caller_groups"] == ["engineering", "corp_all"]

    # VVS2 dense-only sends only dense search leg (no sparse_vec / rrf combine)
    captured_vvs2: dict[str, Any] = {}

    class _FakeVvs2:
        def request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, **_kw: Any) -> tuple[dict[str, Any], int]:
            captured_vvs2["method"] = method
            captured_vvs2["path"] = path
            captured_vvs2["json_body"] = json_body or {}
            return (
                {
                    "results": [
                        {
                            "results": [
                                {
                                    "dataObject": {
                                        "dataObjectId": "c_vvs2_1",
                                        "data": {
                                            "chunk_id": "c_vvs2_1",
                                            "solicitation_id": "solicitation_01",
                                            "doc_name": "04_Pricing_Schedule_CLINs.pdf",
                                            "doc_type": "PRICING_CLIN",
                                            "page_number": 1,
                                            "content": "Rate $210/hr",
                                        },
                                    },
                                    "distance": 0.91,
                                }
                            ]
                        }
                    ]
                },
                1,
            )

    vvs2_eng = VertexVectorSearchV2Engine(
        settings=live_settings(enabled_levels=("vertex_vector_v2",), vvs2_collection="rag-arena-ephem-col"),
        vvs2_client=_FakeVvs2(),
        ranking_client=_ranking(),
    )
    em_vvs2 = RecordingStageEmitter(run_id="r_vvs2", engine="vertex_vector_v2")
    vvs2_cands = run(vvs2_eng._retrieve_candidates([0.1, 0.2], "CLIN rate", None, em_vvs2, candidate_k=10, dense_only=True))
    assert len(vvs2_cands) == 1
    assert len(captured_vvs2["json_body"]["searches"]) == 1
    assert "combine" not in captured_vvs2["json_body"]

    # Composable search_multimodal dispatches to cloudsql_pgvector cleanly
    row = _sample_row("c_comp_sql", doc_name="06_Amendment_0001_Scope_Update.pdf", doc_type="AMENDMENT", amendment=1)
    conn = _FakePgConn([row])
    comp_spec = PipelineSpec(
        index_profile="cloudsql_pgvector",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="vector_cosine",
            candidate_k=10,
            top_k=5,
            reranker="none",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    comp = ComposableRAGEngine(
        spec=comp_spec,
        settings=live_settings(
            enabled_levels=("cloudsql_pgvector",),
            cloudsql_dsn="postgresql://u:p@127.0.0.1:15433/postgres",
        ),
        ranking_client=_ranking(),
        cloudsql_conn_factory=lambda _s: conn,
    )
    em_comp = RecordingStageEmitter(run_id="r_comp", engine="cloudsql_pgvector")
    fake_genai = FakeGenaiClient(answer="Composable dense Cloud SQL.")
    fake_bq = FakeBigQueryClient(rows=[])
    with fake_clients(genai=fake_genai, bq=fake_bq):
        mm = run(
            comp.search_multimodal(
                "CLIN rate",
                solicitation_filter=None,
                element_types=(),
                genai_client=fake_genai,
                bq_client=fake_bq,
                emitter=em_comp,
            )
        )
    assert len(mm.chunks) == 1 and mm.chunks[0].chunk_id == "c_comp_sql"
    assert not any("websearch_to_tsquery" in sql for sql, _ in conn.executed)

    # ADK check_amendments uses ISO GQL on corpus queries and SQL on upl_* queries
    bq_gql = FakeBigQueryClient(rows=[row])
    deps_corpus = ToolDeps(
        settings=live_settings(enabled_levels=("level4_agentic",)),
        retriever=comp,
        genai_client=fake_genai,
        bq_client=bq_gql,
        ledger=AgentLedger("r_amend_1"),
        tier_profile="multimodal",
    )
    out_corpus = run(run_check_amendments(deps_corpus, "solicitation_01", "CLIN-0004-AB"))
    assert out_corpus.response["query_engine"] == "bigquery_property_graph_gql"
    assert "GRAPH_TABLE" in bq_gql.queries[0][0]

    bq_upl = FakeBigQueryClient(rows=[row])
    deps_upl = ToolDeps(
        settings=live_settings(enabled_levels=("level4_agentic",)),
        retriever=comp,
        genai_client=fake_genai,
        bq_client=bq_upl,
        ledger=AgentLedger("r_amend_2"),
        tier_profile="multimodal",
        upload_scope="upl_aabbccddeeff",
    )
    out_upl = run(run_check_amendments(deps_upl, "upl_aabbccddeeff", "CLIN-0004-AB"))
    assert out_upl.response["query_engine"] == "bigquery_sql"
    assert "GRAPH_TABLE" not in bq_upl.queries[0][0]


# --- 5. Scripts, Redaction & Loader offline tests ---


def test_ephemeral_cloudsql_script_and_provision_bq_graph_dry_run_and_redaction() -> None:
    assert EPHEM_CLOUDSQL.is_file()
    assert PROVISION_BQ_GRAPH.is_file()

    # CLOUDSQL_DSN redaction
    s = ArenaSettings(
        demo_mode="live",
        gcp_project_id="my-secret-proj-123",
        cloudsql_dsn="postgresql://postgres:TopSecretPass99@127.0.0.1:15433/postgres",
    )
    tokens = redaction_tokens(s)
    redacted = redact_text(
        f"Connected to {s.cloudsql_dsn} in {s.gcp_project_id}",
        tokens,
    )
    assert "TopSecretPass99" not in redacted
    assert "${CLOUDSQL_DSN}" in redacted
    assert_redacted(redacted, tokens)

    # provision_bq_graph.py --dry-run
    proc_graph = subprocess.run(
        [sys.executable, str(PROVISION_BQ_GRAPH), "--dry-run"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PROJECT_ROOT / "backend"), "DEMO_MODE": "replay"},
        cwd=str(PROJECT_ROOT),
        timeout=30,
        check=False,
    )
    assert proc_graph.returncode == 0, proc_graph.stderr
    assert '"standing_cost_usd_per_month": 0.0' in proc_graph.stdout
    assert '"statements": 6' in proc_graph.stdout

    # ephemeral_cloudsql.sh --dry-run & safety guards
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("GCP_PROJECT_ID=demo-project-1\nREGION=us-central1\n", encoding="utf-8")
        proc_ok = subprocess.run(
            ["bash", str(EPHEM_CLOUDSQL), "--env-file", str(env_file), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=30,
            check=False,
        )
        assert proc_ok.returncode == 0, proc_ok.stderr
        assert "127.0.0.1:15433" in proc_ok.stdout
        assert "halfvec(3072)" in proc_ok.stdout

        proc_no_yes = subprocess.run(
            ["bash", str(EPHEM_CLOUDSQL), "--env-file", str(env_file), "--up"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=30,
            check=False,
        )
        assert proc_no_yes.returncode != 0 and "--yes" in proc_no_yes.stderr

        bad_env = Path(tmp) / "bad.env"
        bad_env.write_text("GCP_PROJECT_ID=demo-project-1\nEPHEM_CLOUDSQL_INSTANCE=prod-db\n", encoding="utf-8")
        proc_bad = subprocess.run(
            ["bash", str(EPHEM_CLOUDSQL), "--env-file", str(bad_env), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=30,
            check=False,
        )
        assert proc_bad.returncode != 0 and "rag-arena-ephem-" in proc_bad.stderr
