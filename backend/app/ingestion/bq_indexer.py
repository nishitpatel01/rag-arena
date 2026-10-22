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
  embedding_multimodal ARRAY<FLOAT64>,
  bbox_source STRING
)
CLUSTER BY tier_profile, solicitation_id;

-- 1a. Level 3 columns on tables created before Phase 3: added in place (existing rows keep NULL).
ALTER TABLE `{fqn}`
  ADD COLUMN IF NOT EXISTS embedding_multimodal ARRAY<FLOAT64>,
  ADD COLUMN IF NOT EXISTS bbox_source STRING;

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


# --------------------------------------------------------------------------- hybrid (VECTOR_SEARCH + SEARCH) RRF

HYBRID_TIERS: tuple[str, ...] = ("hybrid", "multimodal")
MAX_LEXICAL_TERMS = 32
# Function words dropped from the lexical leg (data-driven; listed in the retrieve payload via ``lexical_terms``).
LEXICAL_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "with", "from", "that", "this", "what", "when", "where", "which",
        "who", "whom", "whose", "how", "does", "did", "under", "between", "compare", "calculate", "into", "onto",
        "per", "any", "all", "its", "our", "their", "there", "than", "then", "also", "about", "after", "before",
        "should", "would", "could", "will", "shall", "can", "may", "must", "has", "have", "had", "been", "being",
        "is", "of", "to", "in", "on", "at", "by", "an", "or", "as", "be", "it", "if", "do", "a",
    }
)
_IDENTIFIER_DIGIT_RE = re.compile(r"[0-9]")


def build_lexical_terms(query: str) -> tuple[str, ...]:
    """Lower-cased, de-duplicated lexical terms for the SEARCH() leg (hyphen/dot-joined ids kept whole).

    ``CLIN-0004-AB`` stays one term (exact identifier match via STRPOS) while SEARCH() itself splits it with the
    same analyzer used at index time. Terms shorter than 3 characters are dropped unless they contain a digit.
    """
    tokens = sanitize_search_query(query).lower().split()
    terms: list[str] = []
    for tok in tokens:
        keep = len(tok) >= 3 or bool(_IDENTIFIER_DIGIT_RE.search(tok))
        if keep and tok not in LEXICAL_STOPWORDS and tok not in terms:
            terms.append(tok)
        if len(terms) >= MAX_LEXICAL_TERMS:
            break
    if not terms:
        raise ValueError("query has no lexical terms after stopword removal")
    return tuple(terms)


def build_hybrid_rrf_sql(
    project_id: str,
    dataset_id: str,
    table_id: str = "document_chunks",
    rrf_k: int = 60,
    top_k: int = 50,
    include_solicitation_filter: bool = False,
    embedding_column: str = "embedding",
    *,
    tier_profile: str = "hybrid",
    vector_k: int = 50,
    lexical_k: int = 50,
) -> str:
    """One parameterized statement: VECTOR_SEARCH (top ``vector_k``) FULL OUTER JOIN a SEARCH() lexical CTE (top
    ``lexical_k``), fused by RRF ``1/(rrf_k + vector_rank) + 1/(rrf_k + lexical_rank)``, top ``top_k`` by rrf_score.

    User input only enters through parameters: ``@query_embedding`` (ARRAY<FLOAT64>), ``@lexical_query`` (STRING,
    re-sanitized to ``[a-zA-Z0-9 ]`` in SQL before SEARCH()), ``@lexical_terms`` (ARRAY<STRING>) and optionally
    ``@solicitation_id``. Identifiers are validated; ``tier_profile`` is checked against ``HYBRID_TIERS``.
    """
    proj, dataset, table = _fqn(project_id, dataset_id, table_id)
    for name, value, hi in (("rrf_k", rrf_k, 1000), ("top_k", top_k, 500), ("vector_k", vector_k, 500), ("lexical_k", lexical_k, 500)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > hi:
            raise ValueError(f"{name} must be an integer between 1 and {hi}")
    if embedding_column not in _VALID_EMBEDDING_COLUMNS:
        raise ValueError(f"embedding_column must be one of {_VALID_EMBEDDING_COLUMNS}, got '{embedding_column}'")
    if tier_profile not in HYBRID_TIERS:
        raise ValueError(f"tier_profile must be one of {HYBRID_TIERS}, got '{tier_profile}'")
    fqn = f"{proj}.{dataset}.{table}"
    where = f"tier_profile = '{tier_profile}'" + (" AND solicitation_id = @solicitation_id" if include_solicitation_filter else "")
    return _HYBRID_SQL_TEMPLATE.format(
        fqn=fqn, where=where, tier=tier_profile, col=embedding_column, rrf_k=rrf_k, top_k=top_k, vector_k=vector_k, lexical_k=lexical_k
    )


_HYBRID_SQL_TEMPLATE = """WITH vector_results AS (
  SELECT
    base.chunk_id,
    distance AS vector_distance,
    ROW_NUMBER() OVER (ORDER BY distance ASC, base.chunk_id) AS vector_rank
  FROM VECTOR_SEARCH(
    (SELECT chunk_id, {col} FROM `{fqn}` WHERE {where}),
    '{col}',
    (SELECT @query_embedding AS {col}),
    top_k => {vector_k},
    distance_type => 'COSINE'
  )
),
lexical_scored AS (
  SELECT
    chunk_id,
    SEARCH(content, REGEXP_REPLACE(@lexical_query, r'[^a-zA-Z0-9 ]', ' ')) AS all_terms_match,
    (
      SELECT SUM(IF(STRPOS(LOWER(content), term) > 0, IF(REGEXP_CONTAINS(term, r'[0-9]'), 3, 1), 0))
      FROM UNNEST(@lexical_terms) AS term
    ) AS lexical_score
  FROM `{fqn}`
  WHERE {where}
),
lexical_results AS (
  SELECT chunk_id, lexical_score, lexical_rank
  FROM (
    SELECT
      chunk_id,
      lexical_score,
      ROW_NUMBER() OVER (ORDER BY all_terms_match DESC, lexical_score DESC, chunk_id) AS lexical_rank
    FROM lexical_scored
    WHERE all_terms_match OR lexical_score > 0
  )
  WHERE lexical_rank <= {lexical_k}
),
fused AS (
  SELECT
    COALESCE(v.chunk_id, l.chunk_id) AS chunk_id,
    v.vector_rank,
    v.vector_distance,
    l.lexical_rank,
    l.lexical_score,
    COALESCE(1.0 / ({rrf_k} + v.vector_rank), 0.0) + COALESCE(1.0 / ({rrf_k} + l.lexical_rank), 0.0) AS rrf_score
  FROM vector_results v
  FULL OUTER JOIN lexical_results l
    ON v.chunk_id = l.chunk_id
)
SELECT
  f.chunk_id,
  c.solicitation_id,
  c.doc_name,
  c.doc_type,
  c.element_type,
  c.section_id,
  c.amendment_number,
  c.effective_date,
  c.amends_section,
  c.page_number,
  c.bbox,
  c.content,
  c.layout_source,
  c.bbox_source,
  f.vector_rank,
  f.vector_distance,
  f.lexical_rank,
  f.lexical_score,
  f.rrf_score
FROM fused f
JOIN `{fqn}` c
  ON c.chunk_id = f.chunk_id
WHERE c.tier_profile = '{tier}'
ORDER BY f.rrf_score DESC, f.chunk_id
LIMIT {top_k};
"""
