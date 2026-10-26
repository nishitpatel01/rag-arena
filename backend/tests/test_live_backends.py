"""Live ingestion backends (offline, fake SDK clients): embedder batching/retry/dims, BigQuery loader SQL, index honesty."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import ArenaSettings
from app.core.embeddings import DOCUMENT_TASK_TYPE, QUERY_TASK_TYPE, format_document_for_embedding, format_query_for_embedding
from app.ingestion import live_backends as lb
from app.ingestion.jobs import IngestBackendUnavailable, IngestBackends
from fakes import live_settings

_DIMS = 8


class _ApiError(Exception):
    def __init__(self, code: int, message: str = "boom") -> None:
        super().__init__(f"{code} {message}")
        self.code = code


class ResourceExhausted(Exception):
    """Name-alike of google.api_core.exceptions.ResourceExhausted (retry classification is by class name/code)."""


class _FakeModels:
    def __init__(self, failures: list[BaseException] | None = None, dims: int = _DIMS, per_call: int = 1) -> None:
        self.calls: list[dict[str, Any]] = []
        self._failures = list(failures or [])
        self._dims = dims
        self._per_call = per_call

    def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._failures:
            raise self._failures.pop(0)
        text = kwargs["contents"]
        dims = self._dims + (1 if "ODD" in text else 0)
        emb = SimpleNamespace(values=[0.5] * dims, statistics=SimpleNamespace(token_count=7))
        return SimpleNamespace(embeddings=[emb] * self._per_call)


def _embedder(models: _FakeModels, sleeps: list[float] | None = None) -> lb.VertexEmbedder:
    record = sleeps if sleeps is not None else []
    return lb.VertexEmbedder(client=SimpleNamespace(models=models), model="gemini-embedding-2", sleep=record.append)


def test_embedder_one_vertex_call_per_text_with_document_format_and_task_type() -> None:
    models = _FakeModels()
    vectors = _embedder(models).embed_batch(["alpha clause", "beta clause", "gamma clause"])
    assert len(vectors) == 3 and all(len(v) == _DIMS and isinstance(v[0], float) for v in vectors)
    assert len(models.calls) == 3
    sent = sorted(c["contents"] for c in models.calls)
    assert sent == sorted(format_document_for_embedding(t) for t in ("alpha clause", "beta clause", "gamma clause"))
    assert all(isinstance(c["contents"], str) for c in models.calls)  # never a list: Vertex would aggregate it into ONE vector
    assert all(c["config"].task_type == DOCUMENT_TASK_TYPE == "RETRIEVAL_DOCUMENT" for c in models.calls)
    assert all(c["model"] == "gemini-embedding-2" for c in models.calls)


def test_embedder_enforces_batch_limit_and_input_validation() -> None:
    emb = _embedder(_FakeModels())
    assert lb.MAX_TEXTS_PER_BATCH == 32
    with pytest.raises(ValueError, match="at most 32"):
        emb.embed_batch(["x"] * 33)
    with pytest.raises(ValueError, match="non-empty"):
        emb.embed_batch([])
    with pytest.raises(ValueError, match="non-empty string"):
        emb.embed_batch(["ok", "   "])
    with pytest.raises(ValueError, match="characters"):
        emb.embed_batch(["x" * (lb.MAX_TEXT_CHARS + 1)])


def test_embedder_rejects_aggregated_responses_and_inconsistent_dimensions() -> None:
    with pytest.raises(ValueError, match="exactly one embedding"):
        _embedder(_FakeModels(per_call=2)).embed_batch(["a"])
    with pytest.raises(ValueError, match="dimension"):
        _embedder(_FakeModels()).embed_batch(["normal", "ODD length"])
    with pytest.raises(ValueError, match="empty"):
        _embedder(_FakeModels(dims=0)).embed_batch(["a"])


def test_embedder_retry_is_bounded_exponential_and_only_for_transient_errors() -> None:
    sleeps: list[float] = []
    models = _FakeModels(failures=[_ApiError(429), ResourceExhausted("quota")])
    assert len(_embedder(models, sleeps).embed_batch(["a"])) == 1
    assert len(models.calls) == 3 and sleeps == [1.0, 2.0]

    sleeps2: list[float] = []
    always = _FakeModels(failures=[_ApiError(503)] * 5)
    with pytest.raises(_ApiError, match="503"):
        _embedder(always, sleeps2).embed_batch(["a"])
    assert len(always.calls) == lb.MAX_EMBED_ATTEMPTS == 3 and sleeps2 == [1.0, 2.0]

    bad_request = _FakeModels(failures=[_ApiError(400, "invalid argument")])
    with pytest.raises(_ApiError, match="400"):
        _embedder(bad_request).embed_batch(["a"])
    assert len(bad_request.calls) == 1
    assert lb.is_retryable_error(_ApiError(500)) and lb.is_retryable_error(ResourceExhausted())
    assert not lb.is_retryable_error(ValueError("x")) and not lb.is_retryable_error(_ApiError(403))


def test_query_and_document_embedding_formats_are_asymmetric_retrieval_prefixes() -> None:
    assert format_query_for_embedding("What is CLIN-0004?") == "task: question answering | query: What is CLIN-0004?"
    assert format_document_for_embedding("CLIN-0004 $210/hr") == "title: none | text: CLIN-0004 $210/hr"
    assert QUERY_TASK_TYPE == "RETRIEVAL_QUERY"


# ---------------------------------------------------------------- BigQuery loader


class _Job:
    def __init__(self, job_id: str, rows: list[Any] | None = None, output_rows: int | None = None) -> None:
        self.job_id = job_id
        self._rows = rows or []
        self.output_rows = output_rows
        self.errors = None
        self.result_timeouts: list[Any] = []

    def result(self, timeout: float | None = None) -> list[Any]:
        self.result_timeouts.append(timeout)
        return self._rows


class _FakeBQ:
    def __init__(self, *, inserted: int = 2, table_bytes: int = 500_000, index_rows: list[list[dict[str, Any]]] | None = None) -> None:
        self.queries: list[tuple[str, Any]] = []
        self.loads: list[tuple[list[dict[str, Any]], str, Any]] = []
        self.created: list[Any] = []
        self.deleted: list[str] = []
        self._inserted = inserted
        self._table_bytes = table_bytes
        self._index_rows = list(index_rows or [])

    def query(self, sql: str, job_config: Any = None) -> _Job:
        self.queries.append((sql, job_config))
        if "INFORMATION_SCHEMA" in sql:
            rows = self._index_rows.pop(0) if len(self._index_rows) > 1 else (self._index_rows[0] if self._index_rows else [])
            kind = "vector" if "VECTOR_INDEXES" in sql else "search"
            return _Job("q_idx", [SimpleNamespace(**r) for r in rows if r["kind"] == kind])
        if "GROUP BY tier_profile" in sql:
            return _Job("q_count", [SimpleNamespace(tier_profile="naive", n=52)])
        return _Job("script_123")

    def load_table_from_json(self, rows: list[dict[str, Any]], destination: str, job_config: Any = None) -> _Job:
        self.loads.append((list(rows), str(destination), job_config))
        return _Job("load_1", output_rows=len(rows))

    def create_table(self, table: Any, exists_ok: bool = False) -> Any:
        self.created.append(table)
        return table

    def delete_table(self, table: Any, not_found_ok: bool = False) -> None:
        self.deleted.append(str(table))

    def list_jobs(self, parent_job: Any = None, max_results: int | None = None) -> list[Any]:
        return [
            SimpleNamespace(statement_type="DELETE", dml_stats=SimpleNamespace(inserted_row_count=0, deleted_row_count=3)),
            SimpleNamespace(statement_type="INSERT", dml_stats=SimpleNamespace(inserted_row_count=self._inserted, deleted_row_count=0)),
        ]

    def get_table(self, table: Any) -> Any:
        return SimpleNamespace(num_bytes=self._table_bytes)


def _settings() -> ArenaSettings:
    return live_settings(bq_table="document_chunks_v3")


def _row(chunk_id: str, tier: str = "naive", run: str = "ing_abc123def456") -> dict[str, Any]:
    return {
        "chunk_id": chunk_id, "tier_profile": tier, "ingest_run_id": run, "solicitation_id": "solicitation_01",
        "doc_name": "a.pdf", "doc_type": "RFP", "element_type": "narrative_text", "section_id": None,
        "amendment_number": 0, "effective_date": None, "amends_section": None, "page_number": 1,
        "bbox": {"page": 1, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}, "content": "text", "layout_source": "pdf_stream_regex",
        "embedding": [0.1, 0.2], "embedding_multimodal": [], "bbox_source": None,
    }


def test_loader_uses_load_job_into_staging_then_tier_scoped_transaction_then_drops_only_staging() -> None:
    bq = _FakeBQ(inserted=2)
    loader = lb.BigQueryLiveLoader(_settings(), client=bq)
    assert loader.table_fqn == "test-project.rag_arena.document_chunks_v3"
    inserted = loader.load_rows([_row("c1"), _row("c2")])
    assert inserted == 2
    assert len(bq.loads) == 1
    rows, dest, cfg = bq.loads[0]
    assert dest == "test-project.rag_arena._staging_ing_abc123def456" and len(rows) == 2
    assert cfg.write_disposition == "WRITE_APPEND" and cfg.source_format == "NEWLINE_DELIMITED_JSON"
    assert [f.name for f in cfg.schema][:3] == ["chunk_id", "tier_profile", "ingest_run_id"]
    script = [sql for sql, _ in bq.queries if "BEGIN TRANSACTION" in sql]
    assert len(script) == 1
    sql = script[0]
    assert sql.index("BEGIN TRANSACTION") < sql.index("DELETE FROM `test-project.rag_arena.document_chunks_v3` WHERE tier_profile = @tier")
    assert "INSERT INTO `test-project.rag_arena.document_chunks_v3`" in sql and "FROM `test-project.rag_arena._staging_ing_abc123def456`" in sql
    assert sql.rstrip().endswith("COMMIT TRANSACTION;")
    script_cfg = [cfg for s, cfg in bq.queries if "BEGIN TRANSACTION" in s][0]
    assert {p.name: p.value for p in script_cfg.query_parameters} == {"tier": "naive"}
    assert bq.deleted == ["test-project.rag_arena._staging_ing_abc123def456"]
    assert all("DROP" not in s.upper() and "TRUNCATE" not in s.upper() for s, _ in bq.queries)


def test_loader_drops_staging_even_when_transaction_fails_and_rejects_bad_batches() -> None:
    class _FailingBQ(_FakeBQ):
        def query(self, sql: str, job_config: Any = None) -> _Job:
            if "BEGIN TRANSACTION" in sql:
                raise RuntimeError("Transaction aborted")
            return super().query(sql, job_config)

    bq = _FailingBQ()
    with pytest.raises(RuntimeError, match="Transaction aborted"):
        lb.BigQueryLiveLoader(_settings(), client=bq).load_rows([_row("c1")])
    assert bq.deleted == ["test-project.rag_arena._staging_ing_abc123def456"]
    loader = lb.BigQueryLiveLoader(_settings(), client=_FakeBQ())
    with pytest.raises(ValueError, match="single tier"):
        loader.load_rows([_row("c1"), _row("c2", tier="hybrid")])
    with pytest.raises(ValueError, match="single ingest_run_id"):
        loader.load_rows([_row("c1"), _row("c2", run="ing_other")])
    with pytest.raises(ValueError, match="non-empty"):
        loader.load_rows([])
    with pytest.raises(ValueError, match="Invalid"):
        loader.load_rows([_row("c1", run="ing; DROP TABLE x")])
    with pytest.raises(RuntimeError, match="inserted 1"):
        lb.BigQueryLiveLoader(_settings(), client=_FakeBQ(inserted=1)).load_rows([_row("c1"), _row("c2")])


def test_live_backends_module_never_uses_streaming_inserts() -> None:
    source = Path(lb.__file__).read_text(encoding="utf-8")
    assert "insert_rows" not in source and "insertAll" not in source
    assert "load_table_from_json" in source


def test_identifier_validation_rejects_bad_table_and_dataset_names() -> None:
    for bad in ("bad;drop", "a`b", "x y", "", "1" * 1100):
        with pytest.raises(ValueError):
            lb.BigQueryLiveLoader(live_settings(bq_table=bad), client=_FakeBQ())
    with pytest.raises(ValueError):
        lb.BigQueryLiveLoader(live_settings(bq_dataset="rag-arena; --"), client=_FakeBQ())
    with pytest.raises(ValueError):
        lb.staging_table_name("ing/../x")
    assert lb.staging_table_name("ing_abc123") == "_staging_ing_abc123"


def test_ensure_schema_is_create_if_not_exists_only_with_spec_columns() -> None:
    bq = _FakeBQ()
    lb.BigQueryLiveLoader(_settings(), client=bq).ensure_schema()
    sql = "\n".join(s for s, _ in bq.queries)
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3
    for needle in (
        "`test-project.rag_arena.document_chunks_v3`", "`test-project.rag_arena.ingest_runs`", "`test-project.rag_arena.query_runs`",
        "ingest_run_id STRING NOT NULL", "layout_source STRING", "embedding ARRAY<FLOAT64>", "embedding_multimodal ARRAY<FLOAT64>",
        "CLUSTER BY tier_profile, solicitation_id", "amendment_number INT64 DEFAULT 0", "page_number INT64 NOT NULL", "content STRING NOT NULL",
    ):
        assert needle in sql
    assert "DROP" not in sql.upper() and "CREATE OR REPLACE" not in sql.upper() and "VECTOR INDEX" not in sql


def test_describe_index_state_is_honest_about_small_tables() -> None:
    small = lb.describe_index_state("vector", "ACTIVE", 0.0, table_bytes=2_000_000)
    assert small.startswith("ACTIVE (coverage 0%") and "below 10 MB" in small and "brute-force" in small
    pending = lb.describe_index_state("vector", None, None, table_bytes=2_000_000)
    assert pending.startswith("PENDING") and "below 10 MB" in pending
    big = lb.describe_index_state("vector", "ACTIVE", 100.0, table_bytes=50_000_000)
    assert big == "ACTIVE (coverage 100%)"
    search_small = lb.describe_index_state("search", "ACTIVE", 0.0, table_bytes=2_000_000)
    assert "below 10 GB" in search_small and "ACTIVE" in search_small
    assert "ACTIVE (coverage 100%)" not in small


def test_build_indexes_issues_if_not_exists_ddl_and_polls_with_bound() -> None:
    rows = [
        [{"kind": "vector", "index_name": "document_chunks_v3_vec_idx", "index_status": "ACTIVE", "coverage_percentage": 0}],
        [{"kind": "vector", "index_name": "document_chunks_v3_vec_idx", "index_status": "ACTIVE", "coverage_percentage": 0},
         {"kind": "search", "index_name": "document_chunks_v3_search_idx", "index_status": "ACTIVE", "coverage_percentage": 0}],
    ]
    bq = _FakeBQ(index_rows=rows, table_bytes=1_000_000)
    clock = iter(float(i) for i in range(0, 1000, 5))
    sleeps: list[float] = []
    loader = lb.BigQueryLiveLoader(_settings(), client=bq, sleep=sleeps.append, monotonic=lambda: next(clock))
    states = loader.build_indexes()
    ddl = [s for s, _ in bq.queries if "CREATE" in s]
    assert len(ddl) == 2
    assert "CREATE VECTOR INDEX IF NOT EXISTS `document_chunks_v3_vec_idx`" in ddl[0]
    assert "ON `test-project.rag_arena.document_chunks_v3`(embedding)" in ddl[0] and "index_type = 'IVF'" in ddl[0]
    assert "distance_type = 'COSINE'" in ddl[0]
    assert "CREATE SEARCH INDEX IF NOT EXISTS `document_chunks_v3_search_idx`" in ddl[1] and "(content)" in ddl[1]
    assert set(states) == {"document_chunks_v3_vec_idx", "document_chunks_v3_search_idx"}
    assert "below 10 MB" in states["document_chunks_v3_vec_idx"]
    assert len(sleeps) <= lb.INDEX_POLL_MAX_SECONDS / lb.INDEX_POLL_INTERVAL_SECONDS
    info_sql = [s for s, _ in bq.queries if "INFORMATION_SCHEMA" in s]
    assert all("@table_name" in s for s in info_sql)


def test_build_indexes_times_out_honestly_when_index_never_appears() -> None:
    bq = _FakeBQ(index_rows=[[]], table_bytes=1_000_000)
    clock = iter(float(i) for i in range(0, 10_000, 5))
    sleeps: list[float] = []
    states = lb.BigQueryLiveLoader(_settings(), client=bq, sleep=sleeps.append, monotonic=lambda: next(clock)).build_indexes()
    assert states["document_chunks_v3_vec_idx"].startswith("PENDING")
    assert "not yet listed" in states["document_chunks_v3_search_idx"]
    assert 0 < len(sleeps) <= 12


def test_count_by_tier_and_recent_runs_and_ingest_run_write() -> None:
    bq = _FakeBQ()
    loader = lb.BigQueryLiveLoader(_settings(), client=bq)
    assert loader.count_by_tier() == {"naive": 52}
    loader.write_ingest_run({"run_id": "ing_1", "level": "level1_naive", "tier_profile": "naive", "status": "completed", "stage_log": [{"stage": "parse"}]})
    rows, dest, cfg = bq.loads[-1]
    assert dest == "test-project.rag_arena.ingest_runs" and cfg.write_disposition == "WRITE_APPEND"
    assert rows[0]["stage_log"] == [{"stage": "parse"}]
    with pytest.raises(ValueError, match="run_id"):
        loader.write_ingest_run({"level": "x"})
    loader.write_query_run({"run_id": "run_1", "engine": "level1_naive", "stage_log": [], "citations": []})
    assert bq.loads[-1][1] == "test-project.rag_arena.query_runs"
    with pytest.raises(ValueError, match="unknown"):
        loader.write_query_run({"run_id": "run_1", "engine": "level1_naive", "bogus": 1})


def test_live_backends_factory_refuses_replay_and_builds_live_pair() -> None:
    with pytest.raises(IngestBackendUnavailable, match="DEMO_MODE=replay"):
        lb.live_backends(ArenaSettings(demo_mode="replay"))
    with pytest.raises(IngestBackendUnavailable, match="BQ_TABLE"):
        lb.live_backends(live_settings(bq_table="document_chunks"))
    from unittest.mock import patch

    with patch.object(lb, "get_genai_client", lambda s: SimpleNamespace(models=_FakeModels())), patch.object(lb, "get_bigquery_client", lambda s: _FakeBQ()):
        backends = lb.live_backends(_settings())
    assert isinstance(backends, IngestBackends)
    assert backends.embedder.model == "gemini-embedding-2" and backends.loader.table_fqn.endswith(".document_chunks_v3")


def test_reads_on_a_missing_v3_table_raise_backend_unavailable_with_reason() -> None:
    from google.api_core.exceptions import NotFound

    class _NoTableBQ(_FakeBQ):
        def query(self, sql: str, job_config: Any = None) -> _Job:
            if "INFORMATION_SCHEMA" not in sql:
                raise NotFound("Not found: Table test-project:rag_arena.document_chunks_v3 was not found")
            return super().query(sql, job_config)

        def get_table(self, table: Any) -> Any:
            raise NotFound("Not found: Table document_chunks_v3")

    loader = lb.BigQueryLiveLoader(_settings(), client=_NoTableBQ())
    for call in (loader.count_by_tier, loader.index_states, loader.recent_ingest_runs):
        with pytest.raises(IngestBackendUnavailable, match="does not exist yet"):
            call()


def test_ivf_minimum_rows_rejection_is_reported_honestly_and_search_index_still_built() -> None:
    from google.api_core.exceptions import BadRequest

    class _SmallTableBQ(_FakeBQ):
        def query(self, sql: str, job_config: Any = None) -> _Job:
            if "CREATE VECTOR INDEX" in sql:
                self.queries.append((sql, job_config))
                raise BadRequest("Query error: Total rows 52 is smaller than min allowed 5000 for CREATE VECTOR INDEX query with the IVF index type.")
            return super().query(sql, job_config)

        def get_table(self, table: Any) -> Any:
            return SimpleNamespace(num_bytes=900_000, num_rows=52)

    rows = [[{"kind": "search", "index_name": "document_chunks_v3_search_idx", "index_status": "ACTIVE", "coverage_percentage": 0}]]
    bq = _SmallTableBQ(index_rows=rows)
    clock = iter(float(i) for i in range(0, 10_000, 5))
    states = lb.BigQueryLiveLoader(_settings(), client=bq, sleep=lambda s: None, monotonic=lambda: next(clock)).build_indexes()
    assert any("CREATE SEARCH INDEX" in s for s, _ in bq.queries)
    vec = states["document_chunks_v3_vec_idx"]
    assert vec.startswith("NOT CREATED") and "52 rows" in vec and "5000" in vec and "brute-force" in vec
    assert states["document_chunks_v3_search_idx"].startswith("ACTIVE (coverage 0%") and "below 10 GB" in states["document_chunks_v3_search_idx"]

    class _OtherBadRequestBQ(_SmallTableBQ):
        def query(self, sql: str, job_config: Any = None) -> _Job:
            if "CREATE VECTOR INDEX" in sql:
                raise BadRequest("Access Denied: something else")
            return super().query(sql, job_config)

    with pytest.raises(BadRequest, match="Access Denied"):
        lb.BigQueryLiveLoader(_settings(), client=_OtherBadRequestBQ(index_rows=rows), sleep=lambda s: None, monotonic=lambda: next(clock)).build_indexes()
    assert lb.describe_index_state("vector", None, None, table_bytes=900_000, table_rows=52).startswith("NOT CREATED (table has 52 rows")


def test_large_batches_are_loaded_into_staging_with_bounded_multiple_load_jobs() -> None:
    bq = _FakeBQ(inserted=2500)
    rows = [_row(f"c{i}") for i in range(2500)]
    assert lb.BigQueryLiveLoader(_settings(), client=bq).load_rows(rows) == 2500
    assert [len(r) for r, _, _ in bq.loads] == [1000, 1000, 500]
    assert {d for _, d, _ in bq.loads} == {"test-project.rag_arena._staging_ing_abc123def456"}
    assert sum(1 for s, _ in bq.queries if "BEGIN TRANSACTION" in s) == 1 and len(bq.deleted) == 1
    with pytest.raises(ValueError, match="at most"):
        lb.BigQueryLiveLoader(_settings(), client=_FakeBQ())._check_row_cap(lb.MAX_ROWS_PER_JOB + 1)


# ---------------------------------------------------------------- v1.1 upload scopes


def _upl_row(chunk_id: str, scope: str = "upl_0123456789ab", tier: str = "naive") -> dict[str, Any]:
    return {**_row(chunk_id, tier=tier), "solicitation_id": scope}


def test_corpus_replace_never_deletes_upload_rows() -> None:
    bq = _FakeBQ(inserted=1)
    lb.BigQueryLiveLoader(_settings(), client=bq).load_rows([_row("c1")])
    sql = next(s for s, _ in bq.queries if "BEGIN TRANSACTION" in s)
    assert "WHERE tier_profile = @tier AND NOT STARTS_WITH(solicitation_id, 'upl_');" in sql


def test_upload_replace_is_scoped_to_tier_and_workspace() -> None:
    bq = _FakeBQ(inserted=2)
    loader = lb.BigQueryLiveLoader(_settings(), client=bq)
    assert loader.load_rows([_upl_row("u1"), _upl_row("u2")], scope="upl_0123456789ab") == 2
    sql, cfg = next((s, c) for s, c in bq.queries if "BEGIN TRANSACTION" in s)
    assert "WHERE tier_profile = @tier AND solicitation_id = @scope;" in sql
    assert {p.name: p.value for p in cfg.query_parameters} == {"tier": "naive", "scope": "upl_0123456789ab"}


def test_scope_mismatches_fail_loud() -> None:
    loader = lb.BigQueryLiveLoader(_settings(), client=_FakeBQ())
    with pytest.raises(ValueError, match="scope"):
        loader.load_rows([_upl_row("u1"), _upl_row("u2", scope="upl_ffffffffffff")], scope="upl_0123456789ab")
    with pytest.raises(ValueError, match="scope"):
        loader.load_rows([_upl_row("u1")])  # upload rows without a scope would be a corpus load
    with pytest.raises(ValueError, match="scope"):
        loader.load_rows([_row("c1")], scope="solicitation_01")
    with pytest.raises(ValueError, match="scope"):
        loader.load_rows([_row("c1")], scope="upl_0123456789ab")  # corpus row inside an upload scope


def test_delete_and_count_scope_only_touch_one_workspace() -> None:
    bq = _FakeBQ()
    loader = lb.BigQueryLiveLoader(_settings(), client=bq)
    loader.delete_scope("upl_0123456789ab")
    sql, cfg = bq.queries[-1]
    assert sql.strip() == "DELETE FROM `test-project.rag_arena.document_chunks_v3` WHERE solicitation_id = @scope"
    assert {p.name: p.value for p in cfg.query_parameters} == {"scope": "upl_0123456789ab"}
    loader.count_scope("hybrid", "upl_0123456789ab")
    sql, cfg = bq.queries[-1]
    assert "WHERE tier_profile = @tier AND solicitation_id = @scope" in sql
    for bad in ("solicitation_01", "upl_x", "upl_0123456789ab' OR '1'='1"):
        with pytest.raises(ValueError):
            loader.delete_scope(bad)
