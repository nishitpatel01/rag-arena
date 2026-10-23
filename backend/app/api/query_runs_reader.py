"""Read side of ``query_runs`` for the leaderboard (live/record only): ONE parameterized, bounded BigQuery read.

Per-run rows are fetched (``LIMIT`` ``QUERY_RUNS_MAX_ROWS``, newest first) and aggregated in Python by
``app.evaluation.leaderboard`` so ``scoring.battle_score`` stays the single source of the formula. Filters are
BigQuery query parameters, never interpolated; the table path is built from validated identifiers. Any BigQuery
error becomes ``QueryRunsReadError`` carrying the provider message verbatim, redacted (project id, numbers).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.core.config import ArenaSettings
from app.core.gcp_clients import get_bigquery_client
from app.core.models import EvalMetrics
from app.evaluation.leaderboard import LeaderboardFilters, RunSample, aggregate_leaderboard
from app.ingestion.bq_indexer import _validate_identifier
from app.recording.redaction import redact_text, redaction_tokens

QUERY_RUNS_MAX_ROWS = 5000
QUERY_RUNS_TIMEOUT_SECONDS = 30.0
QUERY_RUNS_MAX_BYTES_BILLED = 1 << 30  # 1 GiB hard cap: the query fails rather than bill more
_MAX_ERROR_CHARS = 2000
QUERY_RUNS_NOTE = (
    "query_runs rows are written only for runs that finished evaluation; failures counts rows with NULL metrics "
    "(engine errors before evaluation are not stored in query_runs)."
)


class QueryRunsReadError(RuntimeError):
    """BigQuery read of ``query_runs`` failed; the message is the redacted provider error."""


def query_runs_table_label(settings: ArenaSettings) -> str:
    """``<dataset>.query_runs`` — the provenance label shown to users (never the project id)."""
    return f"{_validate_identifier(settings.bq_dataset, 'dataset')}.query_runs"


def build_query_runs_sql(project_id: str, dataset_id: str) -> str:
    """Per-run read with ``@question_id`` / ``@engine`` parameters (NULL = no filter) and a fixed LIMIT."""
    project = _validate_identifier(project_id, "project_id")
    dataset = _validate_identifier(dataset_id, "dataset")
    return f"""SELECT engine, question_id, synthesis_model, judge_model, latency_ms, cost_usd,
  groundedness, qa_relevance, context_precision, started_at, demo_mode, git_sha,
  COUNT(*) OVER () AS rows_matching
FROM `{project}.{dataset}.query_runs`
WHERE (@question_id IS NULL OR question_id = @question_id)
  AND (@engine IS NULL OR engine = @engine)
ORDER BY started_at DESC
LIMIT {int(QUERY_RUNS_MAX_ROWS)}"""


def query_runs_parameters(filters: LeaderboardFilters) -> list[Any]:
    """The two STRING query parameters (None → SQL NULL)."""
    from google.cloud import bigquery

    if not isinstance(filters, LeaderboardFilters):
        raise TypeError("filters must be LeaderboardFilters")
    return [
        bigquery.ScalarQueryParameter("question_id", "STRING", filters.question_id),
        bigquery.ScalarQueryParameter("engine", "STRING", filters.engine),
    ]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value)


def _sample_from_row(row: Mapping[str, Any]) -> RunSample:
    triad = (row.get("groundedness"), row.get("qa_relevance"), row.get("context_precision"))
    metrics = None
    if all(v is not None for v in triad):
        metrics = EvalMetrics(groundedness=float(triad[0]), qa_relevance=float(triad[1]), context_precision=float(triad[2]))
    latency, cost = row.get("latency_ms"), row.get("cost_usd")
    return RunSample(
        engine=str(row.get("engine")), synthesis_model=row.get("synthesis_model") or None,
        judge_model=row.get("judge_model") or None, question_id=row.get("question_id"),
        run_at=_iso(row.get("started_at")), git_sha=row.get("git_sha"), demo_mode=row.get("demo_mode"),
        latency_ms=None if latency is None else int(latency), cost_usd=None if cost is None else float(cost),
        metrics=metrics,
    )


def redacted_error(exc: BaseException, settings: ArenaSettings) -> str:
    """``<Type>: <provider message>`` with identity values and ``projects/<digits>`` replaced, bounded."""
    message = str(getattr(exc, "message", "") or exc).strip() or type(exc).__name__
    return redact_text(f"{type(exc).__name__}: {message}", redaction_tokens(settings))[:_MAX_ERROR_CHARS]


def fetch_query_run_rows(
    settings: ArenaSettings, filters: LeaderboardFilters, *, client: Any | None = None
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Run the read; returns (rows, stats). Raises ``QueryRunsReadError`` (redacted) on any BigQuery failure."""
    from google.cloud import bigquery

    sql = build_query_runs_sql(settings.gcp_project_id, settings.bq_dataset)
    config = bigquery.QueryJobConfig(
        query_parameters=query_runs_parameters(filters), maximum_bytes_billed=QUERY_RUNS_MAX_BYTES_BILLED
    )
    try:
        bq = client if client is not None else get_bigquery_client(settings)
        job = bq.query(sql, job_config=config)
        rows = list(job.result(timeout=QUERY_RUNS_TIMEOUT_SECONDS, max_results=QUERY_RUNS_MAX_ROWS))
    except Exception as exc:  # boundary: every provider failure is surfaced verbatim (redacted)
        raise QueryRunsReadError(redacted_error(exc, settings)) from exc
    matching = int(rows[0].get("rows_matching") or 0) if rows else 0
    stats = {
        "rows_read": len(rows), "rows_matching": matching, "max_rows": QUERY_RUNS_MAX_ROWS,
        "bytes_processed": getattr(job, "total_bytes_processed", None),
    }
    return rows, stats


def leaderboard_from_query_runs(
    settings: ArenaSettings, filters: LeaderboardFilters, level_labels: Mapping[str, str], *, client: Any | None = None
) -> dict[str, Any]:
    """Live leaderboard: bounded read + the shared aggregator (same shape as the recordings source)."""
    if not isinstance(settings, ArenaSettings) or not settings.is_live:
        raise ValueError("query_runs is read only in live/record mode")
    rows, stats = fetch_query_run_rows(settings, filters, client=client)
    notes = [QUERY_RUNS_NOTE]
    if stats["rows_matching"] > stats["rows_read"]:
        notes.append(f"Only the newest {stats['rows_read']} of {stats['rows_matching']} matching runs were aggregated.")
    return aggregate_leaderboard(
        (_sample_from_row(r) for r in rows), source="query_runs", filters=filters, level_labels=level_labels,
        table=query_runs_table_label(settings), notes=notes, stats=stats,
    )


__all__ = [
    "QUERY_RUNS_MAX_ROWS",
    "QUERY_RUNS_TIMEOUT_SECONDS",
    "QueryRunsReadError",
    "build_query_runs_sql",
    "fetch_query_run_rows",
    "leaderboard_from_query_runs",
    "query_runs_parameters",
    "query_runs_table_label",
    "redacted_error",
]
