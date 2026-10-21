"""BigQuery DDL and Hybrid Search (VECTOR_SEARCH + SEARCH RRF) SQL builders."""

from __future__ import annotations

import re

_SQL_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*")
_MAX_SEARCH_TOKENS = 64
_VALID_EMBEDDING_COLUMNS = ("embedding", "embedding_multimodal")


def sanitize_search_query(query: str) -> str:
    """Sanitize a user query into safe alphanumeric/clause tokens for BigQuery SEARCH()."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query) > 4000:
        raise ValueError("query exceeds 4000 character bound")

    tokens = [
        tok
        for tok in _SEARCH_TOKEN_PATTERN.findall(query)
        if len(tok) >= 2 or tok.isdigit()
    ][:_MAX_SEARCH_TOKENS]
    if not tokens:
        raise ValueError("query must contain at least one searchable alphanumeric token")
    return " ".join(tokens)


def _validate_identifier(value: str, name: str) -> str:
    """Validate that a BigQuery project, dataset, or table identifier is safe."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid SQL identifier for {name}: must be a non-empty string")
    cleaned = value.strip()
    if not _SQL_IDENTIFIER_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid SQL identifier for {name}: '{value}'. Only [a-zA-Z0-9_-] characters are allowed."
        )
    return cleaned


def _fqn(project_id: str, dataset_id: str, table_id: str) -> tuple[str, str, str]:
    proj = _validate_identifier(project_id, "project_id")
    dataset = _validate_identifier(dataset_id, "dataset_id")
    table = _validate_identifier(table_id, "table_id")
    return proj, dataset, table


def build_tables_ddl(project_id: str, dataset_id: str, table_id: str = "document_chunks") -> str:
    """CREATE TABLE IF NOT EXISTS for the chunks table, ``ingest_runs`` and ``query_runs`` (spec §5); no indexes."""
    proj, dataset, table = _fqn(project_id, dataset_id, table_id)
    fqn = f"{proj}.{dataset}.{table}"
    return f"""-- 1. Unified Document Chunks Table with Provenance, Clustering & Dual Embedding Spaces
CREATE TABLE IF NOT EXISTS `{fqn}` (
  chunk_id STRING NOT NULL,
  tier_profile STRING NOT NULL,
  ingest_run_id STRING NOT NULL,
  solicitation_id STRING NOT NULL,
  doc_name STRING NOT NULL,
  doc_type STRING NOT NULL,
  element_type STRING NOT NULL,
  section_id STRING,
  amendment_number INT64 DEFAULT 0,
  effective_date DATE,
  amends_section STRING,
  page_number INT64 NOT NULL,
  bbox STRUCT<page INT64, x0 FLOAT64, y0 FLOAT64, x1 FLOAT64, y1 FLOAT64>,
  content STRING NOT NULL,
  layout_source STRING,
  embedding ARRAY<FLOAT64>,
  embedding_multimodal ARRAY<FLOAT64>
)
CLUSTER BY tier_profile, solicitation_id;

-- 1b. Ingestion provenance: one row per visible ingestion job (spec §5).
CREATE TABLE IF NOT EXISTS `{proj}.{dataset}.ingest_runs` (
  run_id STRING NOT NULL,
  level STRING NOT NULL,
  tier_profile STRING NOT NULL,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  status STRING,
  docs INT64,
  pages INT64,
  chunks INT64,
  embedded INT64,
  loaded INT64,
  embedding_model STRING,
  layout_source STRING,
  git_sha STRING,
  stage_log JSON
);

-- 1c. Query provenance: one row per engine run (feeds Compare + Leaderboard).
CREATE TABLE IF NOT EXISTS `{proj}.{dataset}.query_runs` (
  run_id STRING NOT NULL,
  engine STRING NOT NULL,
  question_id STRING,
  query STRING,
  started_at TIMESTAMP,
  latency_ms INT64,
  cost_usd FLOAT64,
  groundedness FLOAT64,
  qa_relevance FLOAT64,
  context_precision FLOAT64,
  stage_log JSON,
  answer STRING,
  citations JSON,
  demo_mode STRING,
  git_sha STRING,
  synthesis_model STRING,
  judge_model STRING
);

-- 1d. Columns added after first deploy (model selection): added in place on the existing table.
ALTER TABLE `{proj}.{dataset}.query_runs`
  ADD COLUMN IF NOT EXISTS synthesis_model STRING,
  ADD COLUMN IF NOT EXISTS judge_model STRING;
"""


def vector_index_name(table_id: str, column: str = "embedding") -> str:
    """Index naming convention shared by DDL and INFORMATION_SCHEMA polling."""
    table = _validate_identifier(table_id, "table_id")
    if column not in _VALID_EMBEDDING_COLUMNS:
        raise ValueError(f"column must be one of {_VALID_EMBEDDING_COLUMNS}, got '{column}'")
    return f"{table}_vec_idx" if column == "embedding" else f"{table}_mm_vec_idx"


def search_index_name(table_id: str) -> str:
    return f"{_validate_identifier(table_id, 'table_id')}_search_idx"


def build_vector_index_ddl(project_id: str, dataset_id: str, table_id: str = "document_chunks", column: str = "embedding") -> str:
    """CREATE VECTOR INDEX IF NOT EXISTS (IVF, COSINE) on one embedding column."""
    proj, dataset, table = _fqn(project_id, dataset_id, table_id)
    return (
        f"CREATE VECTOR INDEX IF NOT EXISTS `{vector_index_name(table, column)}`\n"
        f"ON `{proj}.{dataset}.{table}`({column})\n"
        "OPTIONS(distance_type = 'COSINE', index_type = 'IVF');\n"
    )


def build_search_index_ddl(project_id: str, dataset_id: str, table_id: str = "document_chunks") -> str:
    """CREATE SEARCH INDEX IF NOT EXISTS on ``content``."""
    proj, dataset, table = _fqn(project_id, dataset_id, table_id)
    return f"CREATE SEARCH INDEX IF NOT EXISTS `{search_index_name(table)}`\nON `{proj}.{dataset}.{table}`(content);\n"


def build_index_ddl(
    project_id: str, dataset_id: str, table_id: str = "document_chunks", *, columns: tuple[str, ...] = ("embedding",)
) -> str:
    """Vector index DDL per embedding column + the content search index DDL."""
    if not columns or any(c not in _VALID_EMBEDDING_COLUMNS for c in columns):
        raise ValueError(f"columns must be a non-empty subset of {_VALID_EMBEDDING_COLUMNS}")
    parts = [build_vector_index_ddl(project_id, dataset_id, table_id, col) for col in columns]
    parts.append(build_search_index_ddl(project_id, dataset_id, table_id))
    return "\n".join(parts)


def build_bigquery_ddl(
    project_id: str,
    dataset_id: str,
    table_id: str = "document_chunks",
) -> str:
    """Build BigQuery DDL for the unified chunks table, COSINE vector indexes, and inverted search index."""
    tables = build_tables_ddl(project_id, dataset_id, table_id)
    indexes = build_index_ddl(project_id, dataset_id, table_id, columns=_VALID_EMBEDDING_COLUMNS)
    return f"{tables}\n-- 2. Vector Indexes (gemini-embedding-2 text + multimodal) and 3. Inverted Text Search Index\n{indexes}"


def build_hybrid_rrf_sql(
    project_id: str,
    dataset_id: str,
    table_id: str = "document_chunks",
    rrf_k: int = 60,
    top_k: int = 50,
    include_solicitation_filter: bool = False,
    embedding_column: str = "embedding",
) -> str:
    """Build parameterized BigQuery Hybrid Search SQL combining pre-filtered VECTOR_SEARCH and SEARCH() via RRF."""
    proj = _validate_identifier(project_id, "project_id")
    dataset = _validate_identifier(dataset_id, "dataset_id")
    table = _validate_identifier(table_id, "table_id")
    if not isinstance(rrf_k, int) or rrf_k < 1 or rrf_k > 1000:
        raise ValueError("rrf_k must be an integer between 1 and 1000")
    if not isinstance(top_k, int) or top_k < 1 or top_k > 500:
        raise ValueError("top_k must be an integer between 1 and 500")
    if embedding_column not in _VALID_EMBEDDING_COLUMNS:
        raise ValueError(
            f"embedding_column must be one of {_VALID_EMBEDDING_COLUMNS}, got '{embedding_column}'"
        )

    fqn = f"{proj}.{dataset}.{table}"
    sol_predicate = " AND solicitation_id = @solicitation_id" if include_solicitation_filter else ""
    base_subquery = f"(SELECT * FROM `{fqn}` WHERE tier_profile = @tier_profile{sol_predicate})"

    return f"""WITH vector_results AS (
  SELECT
    base.chunk_id,
    base.solicitation_id,
    base.doc_name,
    base.doc_type,
    base.element_type,
    base.section_id,
    base.amendment_number,
    base.effective_date,
    base.amends_section,
    base.page_number,
    base.bbox,
    base.content,
    distance,
    ROW_NUMBER() OVER (ORDER BY distance ASC) AS vector_rank
  FROM VECTOR_SEARCH(
    {base_subquery},
    '{embedding_column}',
    (SELECT @query_embedding AS {embedding_column}),
    top_k => {top_k},
    distance_type => 'COSINE'
  )
),
lexical_results AS (
  SELECT
    chunk_id,
    solicitation_id,
    doc_name,
    doc_type,
    element_type,
    section_id,
    amendment_number,
    effective_date,
    amends_section,
    page_number,
    bbox,
    content,
    ROW_NUMBER() OVER (
      ORDER BY (
        SELECT COUNTIF(LENGTH(tok) >= 3 AND STRPOS(LOWER(content), tok) > 0)
        FROM UNNEST(SPLIT(LOWER(REGEXP_REPLACE(@query_text, r'[^a-zA-Z0-9 ]', ' ')), ' ')) AS tok
      ) DESC,
      amendment_number DESC,
      page_number ASC
    ) AS lexical_rank
  FROM `{fqn}`
  WHERE tier_profile = @tier_profile{sol_predicate}
    AND (
      SEARCH(content, REGEXP_REPLACE(@query_text, r'[^a-zA-Z0-9 ]', ' '))
      OR EXISTS (
        SELECT 1
        FROM UNNEST(SPLIT(LOWER(REGEXP_REPLACE(@query_text, r'[^a-zA-Z0-9 ]', ' ')), ' ')) AS tok
        WHERE LENGTH(tok) >= 4
          AND tok NOT IN ('what', 'when', 'where', 'which', 'with', 'from', 'that', 'this', 'does', 'under', 'between', 'compare', 'calculate')
          AND STRPOS(LOWER(content), tok) > 0
      )
    )
  LIMIT {top_k}
)
SELECT
  COALESCE(v.chunk_id, l.chunk_id) AS chunk_id,
  COALESCE(v.solicitation_id, l.solicitation_id) AS solicitation_id,
  COALESCE(v.doc_name, l.doc_name) AS doc_name,
  COALESCE(v.doc_type, l.doc_type) AS doc_type,
  COALESCE(v.element_type, l.element_type) AS element_type,
  COALESCE(v.section_id, l.section_id) AS section_id,
  COALESCE(v.amendment_number, l.amendment_number) AS amendment_number,
  COALESCE(v.effective_date, l.effective_date) AS effective_date,
  COALESCE(v.amends_section, l.amends_section) AS amends_section,
  COALESCE(v.page_number, l.page_number) AS page_number,
  COALESCE(v.bbox, l.bbox) AS bbox,
  COALESCE(v.content, l.content) AS content,
  (
    COALESCE(1.0 / ({rrf_k} + v.vector_rank), 0.0) +
    COALESCE(1.0 / ({rrf_k} + l.lexical_rank), 0.0)
  ) AS rrf_score
FROM vector_results v
FULL OUTER JOIN lexical_results l
  ON v.chunk_id = l.chunk_id
ORDER BY rrf_score DESC
LIMIT {top_k};
"""
