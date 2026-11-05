"""Level 7 (``alloydb_scann``) SQL builder, parameterization, ScaNN hybrid engine, and DSN redaction tests (offline)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.core.models_catalog import spec_for_settings_model
from app.core.ranking import RankingClient
from app.core.telemetry import QueueStageEmitter
from fakes import FakeGenaiClient, fake_clients, live_settings


class _CaptureEmitter(QueueStageEmitter):
    def __init__(self, engine: str = "alloydb_scann") -> None:
        self.q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        super().__init__(self.q, run_id="run_alloy_test", engine=engine)

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


def test_alloydb_ddl_and_parameterized_hybrid_sql_reject_sql_injection() -> None:
    from app.engines.alloydb_scann import (
        build_alloydb_hybrid_search_sql,
        build_alloydb_scann_ddl,
        validate_pg_identifier,
    )

    assert validate_pg_identifier("document_chunks") == "document_chunks"
    for bad in ("", "1bad", "chunks; DROP TABLE users;--", "public.chunks", "chunks-v3"):
        try:
            validate_pg_identifier(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for unsafe identifier {bad!r}")

    ddl_stmts = build_alloydb_scann_ddl(table="document_chunks", vector_dim=3072, num_leaves=24)
    joined_ddl = "\n".join(ddl_stmts)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in joined_ddl
    assert "CREATE EXTENSION IF NOT EXISTS alloydb_scann" in joined_ddl
    assert "vector(3072)" in joined_ddl
    assert "USING scann (embedding cosine) WITH (num_leaves = 24)" in joined_ddl
    assert "USING GIN (to_tsvector('english', content))" in joined_ddl

    unfiltered_sql, params_u = build_alloydb_hybrid_search_sql(
        table="document_chunks",
        query_vector=[0.125, -0.5, 0.25],
        query_text="CLIN-0004-AB labor rate'; DROP TABLE document_chunks;--",
        candidate_k=25,
        solicitation_filter=None,
    )
    assert "DROP TABLE" not in unfiltered_sql
    assert "%(query_vector)s" in unfiltered_sql
    assert "%(query_text)s" in unfiltered_sql
    assert "%(candidate_k)s" in unfiltered_sql
    assert "%(solicitation_id)s" not in unfiltered_sql
    assert params_u["candidate_k"] == 25
    assert params_u["query_vector"] == "[0.125,-0.5,0.25]"

    filtered_sql, params_f = build_alloydb_hybrid_search_sql(
        table="document_chunks",
        query_vector=[0.125, -0.5],
        query_text="CLIN-0004-AB",
        candidate_k=20,
        solicitation_filter="W912DY-24-R-0001' OR '1'='1",
    )
    assert "OR '1'='1" not in filtered_sql
    assert "%(solicitation_id)s" in filtered_sql
    assert params_f["solicitation_id"] == "W912DY-24-R-0001' OR '1'='1"


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], executed: list[tuple[str, Any]]) -> None:
        self._rows = rows
        self._executed = executed
        self.description = [
            type("Col", (), {"name": n})()
            for n in (
                "chunk_id",
                "doc_name",
                "solicitation_id",
                "page_number",
                "section_id",
                "content",
                "element_type",
                "amendment_number",
                "vec_distance",
                "vec_rank",
                "lex_score",
                "lex_rank",
                "rrf_score",
            )
        ]

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self._executed.append((rendered, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.rows, self.executed)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def test_alloydb_scann_engine_executes_scann_rrf_reranks_synthesizes_and_redacts_dsn() -> None:
    from app.engines.alloydb_scann import AlloyDBScaNNEngine

    secret_dsn = "postgresql://arena_user:SuperSecretPass99@127.0.0.1:15432/rag_arena"
    settings = live_settings(
        gcp_project_id="secret-proj-99",
        enabled_levels=("alloydb_scann",),
        alloydb_dsn=secret_dsn,
        alloydb_table="document_chunks",
        alloydb_candidate_k=25,
        rerank_top_n=2,
    )
    rows = [
        (
            "W912DY_h_01",
            "Solicitation_01.pdf",
            "W912DY-24-R-0001",
            3,
            "B.4",
            "Base CLIN-0004-AB rate was $140.00/hr.",
            "narrative_text",
            0,
            0.14,
            1,
            0.42,
            2,
            0.0325,
        ),
        (
            "W912DY_h_02",
            "Amend_01.pdf",
            "W912DY-24-R-0001",
            6,
            "B.4",
            "Amendment 1 updates CLIN-0004-AB rate to $148.00/hr.",
            "table",
            1,
            0.16,
            2,
            0.88,
            1,
            0.0325,
        ),
    ]
    fake_conn = _FakeConn(rows)
    fake_genai = FakeGenaiClient(answer="CLIN-0004-AB rate is $148.00/hr [chunk_id=W912DY_h_02] [doc=Amend_01.pdf, p.6].")
    fake_ranker = _fake_ranking_client("secret-proj-99", [("W912DY_h_02", 0.96), ("W912DY_h_01", 0.58)])

    eng = AlloyDBScaNNEngine(
        settings=settings,
        synthesis_model=spec_for_settings_model(settings, "gemini-4-argon"),
        ranking_client=fake_ranker,
        conn_factory=lambda _s: fake_conn,
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

    assert result.engine_id == "alloydb_scann"
    assert [c.chunk_id for c in result.retrieved_chunks] == ["W912DY_h_02", "W912DY_h_01"]
    assert result.citations[0].doc_name == "Amend_01.pdf"
    assert fake_conn.committed >= 1
    executed_sql = "\n".join(sql_text for sql_text, _ in fake_conn.executed)
    assert "scann.num_leaves_to_search" in executed_sql

    frames = emitter.drain()
    completed = [f["stage"] for f in frames if f.get("event") == "stage_completed"]
    assert completed == ["query_prep", "embed_query", "retrieve", "rerank", "synthesize"]
    raw_dump = json.dumps(frames)
    assert "SuperSecretPass99" not in raw_dump
    assert "arena_user" not in raw_dump
    assert "secret-proj-99" not in raw_dump


def test_alloydb_scann_engine_fails_loud_on_missing_dsn_upload_scope_zero_hits_and_replay() -> None:
    from app.engines.alloydb_scann import AlloyDBScaNNEngine

    empty_conn = _FakeConn([])

    no_dsn = AlloyDBScaNNEngine(
        settings=live_settings(enabled_levels=("alloydb_scann",), alloydb_dsn=""),
        conn_factory=lambda _s: empty_conn,
    )
    try:
        asyncio.run(no_dsn.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
    except RuntimeError as exc:
        assert "ALLOYDB_DSN" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when ALLOYDB_DSN is empty")

    with_dsn = AlloyDBScaNNEngine(
        settings=live_settings(enabled_levels=("alloydb_scann",), alloydb_dsn="postgresql://u:p@127.0.0.1:15432/db"),
        conn_factory=lambda _s: empty_conn,
    )
    try:
        asyncio.run(with_dsn.run_query("rate?", question_id=None, solicitation_filter="upl_abcdef012345", emitter=_CaptureEmitter()))
    except ValueError as exc:
        assert "upl_" in str(exc) or "upload" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError on upl_* scope")

    with fake_clients(genai=FakeGenaiClient()):
        try:
            asyncio.run(with_dsn.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
        except RuntimeError as exc:
            assert "0 rows" in str(exc) or "ephemeral_alloydb" in str(exc)
        else:
            raise AssertionError("expected RuntimeError on 0 rows")

    replay_eng = AlloyDBScaNNEngine(
        settings=live_settings(
            demo_mode="replay",
            enabled_levels=("alloydb_scann",),
            alloydb_dsn="postgresql://u:p@127.0.0.1:15432/db",
        ),
        conn_factory=lambda _s: empty_conn,
    )
    try:
        asyncio.run(replay_eng.run_query("rate?", question_id=None, solicitation_filter=None, emitter=_CaptureEmitter()))
    except RuntimeError as exc:
        assert "replay" in str(exc).lower()
    else:
        raise AssertionError("expected RuntimeError in DEMO_MODE=replay")
