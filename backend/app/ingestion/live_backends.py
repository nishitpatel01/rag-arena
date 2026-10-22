"""Live ingestion backends (Phase 1): Vertex AI ``gemini-embedding-2`` embedder + BigQuery loader.

* ``VertexEmbedder`` — one ``embed_content`` call per text (Vertex returns ONE aggregated vector for a list
  of strings; see ``app.core.embeddings``), ≤ ``MAX_TEXTS_PER_BATCH`` texts per ``embed_batch``, bounded
  thread fan-out, bounded exponential retry on 429/5xx/ResourceExhausted/ServiceUnavailable only.
* ``BigQueryLiveLoader`` — load jobs only (never streaming inserts, which block DML for ~90 min):
  rows → per-job staging table → one transaction ``DELETE tier rows; INSERT staging`` → drop that staging table.
  Index states are read back from ``INFORMATION_SCHEMA`` and reported verbatim (never faked ACTIVE).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import re
import time
from typing import Any, Callable

from app.core.config import ArenaSettings
from app.core.embeddings import DOCUMENT_TASK_TYPE, embed_config, format_document_for_embedding, single_vector
from app.core.gcp_clients import get_bigquery_client, get_genai_client
from app.ingestion.bq_indexer import (
    _validate_identifier,
    build_search_index_ddl,
    build_tables_ddl,
    build_vector_index_ddl,
    search_index_name,
    vector_index_name,
)
from app.ingestion.jobs import IngestBackends, IngestBackendUnavailable

MAX_TEXTS_PER_BATCH = 32
MAX_TEXT_CHARS = 30_000  # gemini-embedding-2 input limit is 8,192 tokens; reject clearly oversized inputs up front
MAX_EMBED_ATTEMPTS = 3
EMBED_BACKOFF_BASE_SECONDS = 1.0
MAX_EMBED_WORKERS = 8
INDEX_POLL_MAX_SECONDS = 60.0
INDEX_POLL_INTERVAL_SECONDS = 5.0
LOAD_TIMEOUT_SECONDS = 300.0
LOAD_BATCH_ROWS = 1000  # ~60 MB of NDJSON per load job at 3072-d embeddings
MAX_ROWS_PER_JOB = 200_000
QUERY_TIMEOUT_SECONDS = 120.0
STAGING_TTL = timedelta(hours=2)
VECTOR_INDEX_MIN_BYTES = 10 * 1024 * 1024
SEARCH_INDEX_MIN_BYTES = 10 * 1024 * 1024 * 1024
IVF_MIN_ROWS = 5000  # BigQuery: "Total rows N is smaller than min allowed 5000 for CREATE VECTOR INDEX ... IVF"
_IVF_MIN_ROWS_RE = re.compile(r"Total rows (\d+) is smaller than min allowed (\d+) for CREATE VECTOR INDEX")
# BigQuery allows ONE vector index per table (measured 2026-10-22: 409 ALREADY_EXISTS on a second CREATE VECTOR INDEX).
_ONE_VECTOR_INDEX_RE = re.compile(r"Table '[^']*' already has a vector index on it, so can't create a new vector index\.")
_MAX_CHILD_JOBS = 20
_MAX_RECENT_RUNS = 50
_BQ_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_VALID_TIERS = ("naive", "hybrid", "multimodal")
_RETRYABLE_NAMES = frozenset({"ResourceExhausted", "ServiceUnavailable"})

_CHUNK_SCHEMA: tuple[tuple[str, str, str], ...] = (
    ("chunk_id", "STRING", "REQUIRED"), ("tier_profile", "STRING", "REQUIRED"), ("ingest_run_id", "STRING", "REQUIRED"),
    ("solicitation_id", "STRING", "REQUIRED"), ("doc_name", "STRING", "REQUIRED"), ("doc_type", "STRING", "REQUIRED"),
    ("element_type", "STRING", "REQUIRED"), ("section_id", "STRING", "NULLABLE"), ("amendment_number", "INT64", "NULLABLE"),
    ("effective_date", "DATE", "NULLABLE"), ("amends_section", "STRING", "NULLABLE"), ("page_number", "INT64", "REQUIRED"),
    ("bbox", "STRUCT", "NULLABLE"), ("content", "STRING", "REQUIRED"), ("layout_source", "STRING", "NULLABLE"),
    ("embedding", "FLOAT64", "REPEATED"), ("embedding_multimodal", "FLOAT64", "REPEATED"),
    ("bbox_source", "STRING", "NULLABLE"),
)
_INGEST_RUN_SCHEMA: tuple[tuple[str, str, str], ...] = (
    ("run_id", "STRING", "REQUIRED"), ("level", "STRING", "REQUIRED"), ("tier_profile", "STRING", "REQUIRED"),
    ("started_at", "TIMESTAMP", "NULLABLE"), ("finished_at", "TIMESTAMP", "NULLABLE"), ("status", "STRING", "NULLABLE"),
    ("docs", "INT64", "NULLABLE"), ("pages", "INT64", "NULLABLE"), ("chunks", "INT64", "NULLABLE"),
    ("embedded", "INT64", "NULLABLE"), ("loaded", "INT64", "NULLABLE"), ("embedding_model", "STRING", "NULLABLE"),
    ("layout_source", "STRING", "NULLABLE"), ("git_sha", "STRING", "NULLABLE"), ("stage_log", "JSON", "NULLABLE"),
)
_QUERY_RUN_SCHEMA: tuple[tuple[str, str, str], ...] = (
    ("run_id", "STRING", "REQUIRED"), ("engine", "STRING", "REQUIRED"), ("question_id", "STRING", "NULLABLE"),
    ("query", "STRING", "NULLABLE"), ("started_at", "TIMESTAMP", "NULLABLE"), ("latency_ms", "INT64", "NULLABLE"),
    ("cost_usd", "FLOAT64", "NULLABLE"), ("groundedness", "FLOAT64", "NULLABLE"), ("qa_relevance", "FLOAT64", "NULLABLE"),
    ("context_precision", "FLOAT64", "NULLABLE"), ("stage_log", "JSON", "NULLABLE"), ("answer", "STRING", "NULLABLE"),
    ("citations", "JSON", "NULLABLE"), ("demo_mode", "STRING", "NULLABLE"), ("git_sha", "STRING", "NULLABLE"),
    ("synthesis_model", "STRING", "NULLABLE"), ("judge_model", "STRING", "NULLABLE"),
)


def is_retryable_error(exc: BaseException) -> bool:
    """Retry only transient quota/server errors: HTTP 429/5xx, ResourceExhausted, ServiceUnavailable."""
    if type(exc).__name__ in _RETRYABLE_NAMES:
        return True
    code = getattr(exc, "code", None)
    return isinstance(code, int) and not isinstance(code, bool) and (code == 429 or 500 <= code <= 599)


class VertexEmbedder:
    """``Embedder`` for gemini-embedding-2 on Vertex AI (document side of the retrieval space)."""

    def __init__(
        self, *, client: Any, model: str, task_type: str = DOCUMENT_TASK_TYPE,
        max_workers: int = MAX_EMBED_WORKERS, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None or not hasattr(client, "models"):
            raise TypeError("client must be a google-genai Client (with .models)")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
            raise ValueError("max_workers must be an integer in [1, 32]")
        self.client = client
        self.model = model.strip()
        self.task_type = task_type
        self.input_format = format_document_for_embedding("{content}")
        self._max_workers = max_workers
        self._sleep = sleep

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed ≤ 32 texts (one Vertex call each, bounded concurrency); all vectors share one dimensionality."""
        _validate_texts(texts)
        config = embed_config(self.task_type)
        workers = min(self._max_workers, len(texts))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="embed") as pool:
            vectors = list(pool.map(lambda t: self._embed_one(t, config), texts))
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise ValueError(f"inconsistent embedding dimensionality in batch: {sorted(dims)}")
        return vectors

    def _embed_one(self, text: str, config: Any) -> list[float]:
        contents = format_document_for_embedding(text)
        for attempt in range(MAX_EMBED_ATTEMPTS):
            try:
                resp = self.client.models.embed_content(model=self.model, contents=contents, config=config)
                return single_vector(resp)
            except Exception as exc:  # classified below; non-transient or final attempt re-raises
                if attempt + 1 >= MAX_EMBED_ATTEMPTS or not is_retryable_error(exc):
                    raise
                self._sleep(EMBED_BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise RuntimeError("unreachable: embed retry loop exited without result")


def _validate_texts(texts: list[str]) -> None:
    if not isinstance(texts, list) or not texts:
        raise ValueError("texts must be a non-empty list of strings")
    if len(texts) > MAX_TEXTS_PER_BATCH:
        raise ValueError(f"embed_batch accepts at most {MAX_TEXTS_PER_BATCH} texts, got {len(texts)}")
    for idx, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"texts[{idx}] must be a non-empty string")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"texts[{idx}] exceeds {MAX_TEXT_CHARS} characters")


def staging_table_name(job_id: str) -> str:
    """``_staging_<job_id>`` with a strict identifier check (job ids are ``ing_<hex>``)."""
    if not isinstance(job_id, str) or not _RUN_ID_RE.match(job_id):
        raise ValueError(f"Invalid ingest job id for staging table: {job_id!r}")
    return f"_staging_{job_id}"


def _validate_table_name(value: str, name: str) -> str:
    if not isinstance(value, str) or not _BQ_TABLE_RE.match(value):
        raise ValueError(f"Invalid BigQuery {name}: {value!r} (allowed: letters, digits, underscore; max 1024)")
    return value


def describe_index_state(
    kind: str, status: str | None, coverage: float | None, *, table_bytes: int, table_rows: int | None = None
) -> str:
    """Honest one-line state: BigQuery's own ``index_status`` + coverage, plus why a small table stays unindexed."""
    if kind not in ("vector", "search"):
        raise ValueError("kind must be 'vector' or 'search'")
    threshold = VECTOR_INDEX_MIN_BYTES if kind == "vector" else SEARCH_INDEX_MIN_BYTES
    limit_label = "10 MB" if kind == "vector" else "10 GB"
    fallback = "VECTOR_SEARCH uses exact brute-force" if kind == "vector" else "SEARCH() scans the table without the index"
    pct = None if coverage is None else float(coverage)
    small = int(table_bytes) < threshold
    if status is None and kind == "vector" and table_rows is not None and table_rows < IVF_MIN_ROWS:
        return f"NOT CREATED (table has {table_rows} rows; BigQuery IVF vector index needs >= {IVF_MIN_ROWS} — {fallback})"
    if status is None:
        reason = f"; table below {limit_label} — {fallback}" if small else ""
        return f"PENDING (not yet listed in INFORMATION_SCHEMA{reason})"
    head = f"{status} (coverage {pct:g}%" if pct is not None else f"{status} (coverage unknown"
    if small and (pct is None or pct < 100.0):
        return f"{head}; table below {limit_label} — index not populated, {fallback})"
    return f"{head})"


class BigQueryLiveLoader:
    """``BigQueryLoader`` writing to ``BQ_TABLE`` (+ ``ingest_runs``/``query_runs``) with load jobs and DML only."""

    def __init__(
        self, settings: ArenaSettings, *, client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        self._project = _validate_identifier(settings.gcp_project_id, "project_id")
        self._dataset = _validate_table_name(settings.bq_dataset, "dataset")
        self._table = _validate_table_name(settings.bq_table, "table")
        self.table_fqn = f"{self._project}.{self._dataset}.{self._table}"
        self._client = client if client is not None else get_bigquery_client(settings)
        self._sleep = sleep
        self._monotonic = monotonic
        self._index_errors: dict[str, str] = {}

    def _fq(self, table: str) -> str:
        return f"{self._project}.{self._dataset}.{_validate_table_name(table, 'table')}"

    def _query(self, sql: str, params: list[Any] | None = None) -> Any:
        from google.cloud import bigquery

        job = self._client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=list(params or [])))
        rows = list(job.result(timeout=QUERY_TIMEOUT_SECONDS))
        return job, rows

    def _read(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a read; a missing table/dataset becomes IngestBackendUnavailable (visible reason, not a 500)."""
        from google.api_core.exceptions import NotFound

        try:
            return fn(*args)
        except NotFound as exc:
            raise IngestBackendUnavailable(
                f"BigQuery table {self.table_fqn} (or ingest_runs) does not exist yet — run the ingestion job first ({exc.message})"
            ) from exc

    def ensure_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS for chunks/ingest_runs/query_runs + ``ALTER TABLE … ADD COLUMN IF NOT EXISTS``
        for columns added later (query_runs.synthesis_model/judge_model) — never replaces or drops."""
        self._query(build_tables_ddl(self._project, self._dataset, self._table))

    def _load_json(self, rows: list[dict[str, Any]], table_fqn: str, schema: tuple[tuple[str, str, str], ...]) -> int:
        from google.cloud import bigquery

        config = bigquery.LoadJobConfig(
            schema=_schema_fields(schema),
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )
        job = self._client.load_table_from_json(rows, table_fqn, job_config=config)
        job.result(timeout=LOAD_TIMEOUT_SECONDS)
        if getattr(job, "errors", None):
            raise RuntimeError(f"BigQuery load job into {table_fqn} reported errors: {job.errors}")
        loaded = int(getattr(job, "output_rows", 0) or 0)
        if loaded != len(rows):
            raise RuntimeError(f"BigQuery load job into {table_fqn} loaded {loaded} rows for {len(rows)} submitted")
        return loaded

    def load_rows(self, rows: list[dict[str, Any]]) -> int:
        """Load job → ``_staging_<job_id>`` → transactional tier-scoped replace → drop that staging table."""
        tier, job_id = _validate_chunk_rows(rows)
        self._check_row_cap(len(rows))
        staging_fqn = self._fq(staging_table_name(job_id))
        self._create_staging(staging_fqn)
        try:
            for start in range(0, len(rows), LOAD_BATCH_ROWS):
                self._load_json(rows[start : start + LOAD_BATCH_ROWS], staging_fqn, _CHUNK_SCHEMA)
            inserted = self._replace_tier_from_staging(staging_fqn, tier)
        finally:
            self._client.delete_table(staging_fqn, not_found_ok=False)
        if inserted != len(rows):
            raise RuntimeError(f"transaction inserted {inserted} rows into {self.table_fqn} for {len(rows)} staged")
        return inserted

    @staticmethod
    def _check_row_cap(n_rows: int) -> None:
        if n_rows > MAX_ROWS_PER_JOB:
            raise ValueError(f"load_rows accepts at most {MAX_ROWS_PER_JOB} rows per ingestion job, got {n_rows}")

    def _create_staging(self, staging_fqn: str) -> None:
        from google.cloud import bigquery

        table = bigquery.Table(staging_fqn, schema=_schema_fields(_CHUNK_SCHEMA))
        table.expires = datetime.now(timezone.utc) + STAGING_TTL  # safety net if the explicit drop never runs
        self._client.create_table(table, exists_ok=False)

    def _replace_tier_from_staging(self, staging_fqn: str, tier: str) -> int:
        from google.cloud import bigquery

        cols = ", ".join(name for name, _, _ in _CHUNK_SCHEMA)
        script = (
            "BEGIN TRANSACTION;\n"
            f"DELETE FROM `{self.table_fqn}` WHERE tier_profile = @tier;\n"
            f"INSERT INTO `{self.table_fqn}` ({cols})\nSELECT {cols} FROM `{staging_fqn}`;\n"
            "COMMIT TRANSACTION;\n"
        )
        job, _ = self._query(script, [bigquery.ScalarQueryParameter("tier", "STRING", tier)])
        children = list(self._client.list_jobs(parent_job=job.job_id, max_results=_MAX_CHILD_JOBS))
        inserts = [c for c in children if getattr(c, "statement_type", None) == "INSERT"]
        if len(inserts) != 1 or getattr(inserts[0], "dml_stats", None) is None:
            raise RuntimeError(f"could not read INSERT dml_stats from transaction job {job.job_id} ({len(children)} child jobs)")
        return int(inserts[0].dml_stats.inserted_row_count)

    def build_indexes(self, embedding_columns: tuple[str, ...] = ("embedding",)) -> dict[str, str]:
        """Issue CREATE ... INDEX IF NOT EXISTS (one vector index per embedding column + the search index), then poll
        INFORMATION_SCHEMA (≤ 60 s) and report real states."""
        if not embedding_columns or any(c not in ("embedding", "embedding_multimodal") for c in embedding_columns):
            raise ValueError(f"embedding_columns must be a non-empty subset of ('embedding', 'embedding_multimodal'), got {embedding_columns}")
        for column in embedding_columns:
            self._create_vector_index(column)
        self._query(build_search_index_ddl(self._project, self._dataset, self._table))
        deadline = self._monotonic() + INDEX_POLL_MAX_SECONDS
        max_polls = int(INDEX_POLL_MAX_SECONDS / INDEX_POLL_INTERVAL_SECONDS) + 1
        rows, stats = self._read_index_rows(), self._table_stats()
        for _ in range(max_polls):
            if _indexes_settled(rows, stats) or self._monotonic() + INDEX_POLL_INTERVAL_SECONDS > deadline:
                break
            self._sleep(INDEX_POLL_INTERVAL_SECONDS)
            rows, stats = self._read_index_rows(), self._table_stats()
        return self._describe(rows, stats, embedding_columns)

    def _create_vector_index(self, column: str = "embedding") -> None:
        """CREATE VECTOR INDEX; BigQuery's IVF minimum-row rejection is the only tolerated error — its message is kept
        verbatim and reported in the index state (``NOT CREATED (BigQuery: …)``)."""
        from google.api_core.exceptions import BadRequest, Conflict

        try:
            self._query(build_vector_index_ddl(self._project, self._dataset, self._table, column))
        except BadRequest as exc:
            match = _IVF_MIN_ROWS_RE.search(str(exc))
            if not match:
                raise
            rows, minimum = match.group(1), match.group(2)
            self._index_errors[vector_index_name(self._table, column)] = (
                f"{rows} rows on {column}; BigQuery IVF vector index needs >= {minimum} — BigQuery: '{match.group(0)}'"
            )
        except Conflict as exc:
            match = _ONE_VECTOR_INDEX_RE.search(str(exc))
            if column == "embedding" or not match:
                raise
            verbatim = match.group(0).replace(self._project, "${GCP_PROJECT_ID}")
            self._index_errors[vector_index_name(self._table, column)] = (
                f"BigQuery allows one vector index per table and {vector_index_name(self._table)} (embedding) exists — "
                f"BigQuery: \"{verbatim}\""
            )

    def index_states(self) -> dict[str, str]:
        columns = ("embedding", "embedding_multimodal")
        return self._describe(self._read(self._read_index_rows), self._read(self._table_stats), columns)

    def _read_index_rows(self) -> dict[str, tuple[str, str | None, float | None]]:
        from google.cloud import bigquery

        param = [bigquery.ScalarQueryParameter("table_name", "STRING", self._table)]
        out: dict[str, tuple[str, str | None, float | None]] = {}
        for kind, view in (("vector", "VECTOR_INDEXES"), ("search", "SEARCH_INDEXES")):
            sql = (
                "SELECT index_name, index_status, coverage_percentage "
                f"FROM `{self._project}.{self._dataset}.INFORMATION_SCHEMA.{view}` WHERE table_name = @table_name"
            )
            _, rows = self._query(sql, param)
            for r in rows:
                cov = getattr(r, "coverage_percentage", None)
                out[str(r.index_name)] = (kind, getattr(r, "index_status", None), None if cov is None else float(cov))
        return out

    def _table_stats(self) -> tuple[int, int | None]:
        table = self._client.get_table(self.table_fqn)
        rows = getattr(table, "num_rows", None)
        return int(getattr(table, "num_bytes", 0) or 0), None if rows is None else int(rows)

    def _describe(
        self, rows: dict[str, tuple[str, str | None, float | None]], stats: tuple[int, int | None],
        embedding_columns: tuple[str, ...] = ("embedding",),
    ) -> dict[str, str]:
        expected = {vector_index_name(self._table, c): "vector" for c in embedding_columns}
        expected[search_index_name(self._table)] = "search"
        states: dict[str, str] = {}
        for name, kind in expected.items():
            _, status, cov = rows.get(name, (kind, None, None))
            if status is None and name in self._index_errors:
                states[name] = f"NOT CREATED ({self._index_errors[name]}) — VECTOR_SEARCH uses exact brute-force"
                continue
            if status is None and name == vector_index_name(self._table, "embedding_multimodal"):
                states[name] = "NOT CREATED (not listed in INFORMATION_SCHEMA.VECTOR_INDEXES) — VECTOR_SEARCH on embedding_multimodal uses exact brute-force"
                continue
            states[name] = describe_index_state(kind, status, cov, table_bytes=stats[0], table_rows=stats[1])
        return states

    def count_by_tier(self) -> dict[str, int]:
        sql = f"SELECT tier_profile, COUNT(*) AS n FROM `{self.table_fqn}` GROUP BY tier_profile ORDER BY tier_profile"
        _, rows = self._read(self._query, sql)
        return {str(r.tier_profile): int(r.n) for r in rows}

    def recent_ingest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent ``ingest_runs`` rows (provenance for the /ingest page)."""
        from google.cloud import bigquery

        if not isinstance(limit, int) or not 1 <= limit <= _MAX_RECENT_RUNS:
            raise ValueError(f"limit must be an integer in [1, {_MAX_RECENT_RUNS}]")
        cols = [n for n, _, _ in _INGEST_RUN_SCHEMA if n != "stage_log"]
        sql = f"SELECT {', '.join(cols)} FROM `{self._fq('ingest_runs')}` ORDER BY finished_at DESC LIMIT @limit"
        _, rows = self._read(self._query, sql, [bigquery.ScalarQueryParameter("limit", "INT64", limit)])
        return [{c: _jsonable(getattr(r, c, None)) for c in cols} for r in rows]

    def write_ingest_run(self, row: dict[str, Any]) -> None:
        self._load_json([_validated_row(row, _INGEST_RUN_SCHEMA)], self._fq("ingest_runs"), _INGEST_RUN_SCHEMA)

    def write_query_run(self, row: dict[str, Any]) -> None:
        self._load_json([_validated_row(row, _QUERY_RUN_SCHEMA)], self._fq("query_runs"), _QUERY_RUN_SCHEMA)


def _indexes_settled(rows: dict[str, tuple[str, str | None, float | None]], stats: tuple[int, int | None]) -> bool:
    """Stop polling once every creatable index is listed and either fully covered or known-unpopulated (small table)."""
    table_bytes, table_rows = stats
    required = {"search"} if table_rows is not None and table_rows < IVF_MIN_ROWS else {"vector", "search"}
    if not required <= {kind for kind, _, _ in rows.values()}:
        return False
    for kind, _status, cov in rows.values():
        threshold = VECTOR_INDEX_MIN_BYTES if kind == "vector" else SEARCH_INDEX_MIN_BYTES
        if table_bytes >= threshold and (cov is None or cov < 100.0):
            return False
    return True


def _validate_chunk_rows(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list of chunk row dicts")
    tiers = {r.get("tier_profile") for r in rows}
    if len(tiers) != 1:
        raise ValueError(f"load_rows requires a single tier per job, got {sorted(map(str, tiers))}")
    runs = {r.get("ingest_run_id") for r in rows}
    if len(runs) != 1:
        raise ValueError("load_rows requires a single ingest_run_id per job")
    tier, run_id = tiers.pop(), runs.pop()
    if tier not in _VALID_TIERS:
        raise ValueError(f"Invalid tier_profile {tier!r}; expected one of {_VALID_TIERS}")
    staging_table_name(str(run_id))
    expected = {name for name, _, _ in _CHUNK_SCHEMA}
    for idx, row in enumerate(rows):
        if set(row) != expected:
            raise ValueError(f"rows[{idx}] keys differ from the chunk schema: {sorted(set(row) ^ expected)}")
    return str(tier), str(run_id)


def _validated_row(row: dict[str, Any], schema: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError("row must be a dict")
    allowed = {name for name, _, _ in schema}
    unknown = sorted(set(row) - allowed)
    if unknown:
        raise ValueError(f"row has unknown columns {unknown}")
    for name, _, mode in schema:
        if mode == "REQUIRED" and not row.get(name):
            raise ValueError(f"row is missing required column '{name}'")
    return dict(row)


def _schema_fields(schema: tuple[tuple[str, str, str], ...]) -> list[Any]:
    from google.cloud import bigquery

    bbox = [bigquery.SchemaField(n, t) for n, t in (("page", "INT64"), ("x0", "FLOAT64"), ("y0", "FLOAT64"), ("x1", "FLOAT64"), ("y1", "FLOAT64"))]
    return [
        bigquery.SchemaField(name, ftype, mode=mode, fields=bbox if ftype == "STRUCT" else ())
        for name, ftype, mode in schema
    ]


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def live_backends(settings: ArenaSettings) -> IngestBackends:
    """Factory for ``IngestJobRunner``: real Vertex embedder + BigQuery loader (live/record modes only)."""
    if not isinstance(settings, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance")
    if not settings.is_live:
        raise IngestBackendUnavailable(
            f"Live ingestion needs DEMO_MODE=live|record; current DEMO_MODE={settings.demo_mode} makes no GCP calls"
        )
    if settings.bq_table == "document_chunks":
        raise IngestBackendUnavailable(
            "BQ_TABLE=document_chunks is the frozen v2 table; v3 ingestion writes only to a v3 table (e.g. document_chunks_v3)"
        )
    embedder = VertexEmbedder(client=get_genai_client(settings), model=settings.text_embedding_model)
    loader = BigQueryLiveLoader(settings, client=get_bigquery_client(settings))
    if not settings.docai_processor_id:
        return IngestBackends(embedder=embedder, loader=loader)
    return IngestBackends(embedder=embedder, loader=loader, **level3_backends(settings))


def level3_backends(settings: ArenaSettings) -> dict[str, Any]:
    """Document AI Layout Parser + Gemini vision transcriber + multimodal embedder for the multimodal tier."""
    from app.core.gcp_clients import get_access_token
    from app.ingestion.documentai_client import DocumentAIClient
    from app.ingestion.layout_documentai import LayoutParser
    from app.ingestion.multimodal import VertexMultimodalEmbedder
    from app.ingestion.vision_transcribe import GeminiVisionTranscriber

    client = DocumentAIClient(
        project_id=settings.gcp_project_id, location=settings.docai_location,
        token_provider=lambda: get_access_token(settings), timeout_seconds=float(settings.docai_timeout_seconds),
    )
    genai = get_genai_client(settings)
    return {
        "layout_parser": LayoutParser(client=client, processor_id=settings.docai_processor_id, processor_version=settings.docai_processor_version),
        "vision": GeminiVisionTranscriber(client=genai, model=settings.vision_model),
        "mm_embedder": VertexMultimodalEmbedder(client=genai, model=settings.multimodal_embedding_model),
    }




class LiveQueryRunWriter:
    """``QueryRunWriter`` that builds its ``BigQueryLiveLoader`` on first write (no client at app start-up)."""

    def __init__(self, settings: ArenaSettings) -> None:
        if not isinstance(settings, ArenaSettings) or not settings.is_live:
            raise ValueError("LiveQueryRunWriter requires live/record ArenaSettings")
        self._settings = settings
        self._loader: BigQueryLiveLoader | None = None

    @property
    def loaded(self) -> bool:
        return self._loader is not None

    def write_query_run(self, row: dict[str, Any]) -> None:
        """First write runs ``ensure_schema`` once (adds new query_runs columns in place), then appends the row."""
        if self._loader is None:
            loader = BigQueryLiveLoader(self._settings)
            loader.ensure_schema()
            self._loader = loader
        self._loader.write_query_run(row)


__all__ = [
    "BigQueryLiveLoader",
    "LiveQueryRunWriter",
    "MAX_TEXTS_PER_BATCH",
    "VertexEmbedder",
    "describe_index_state",
    "is_retryable_error",
    "live_backends",
    "staging_table_name",
]
