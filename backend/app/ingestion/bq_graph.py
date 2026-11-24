"""Pure BigQuery Property Graph DDL & ISO GQL query builders (v1.6.4).

Builds five lightweight SQL-derived node/edge tables (<50 KiB total, $0/mo storage) from
``document_chunks_v3`` (``tier_profile='multimodal'``) and defines a BigQuery Property Graph
(``CREATE OR REPLACE PROPERTY GRAPH``) queried via ISO GQL ``GRAPH_TABLE(...)``:

* Nodes: ``ContractDoc``, ``ContractSection``
* Edges: ``DEFINES_SECTION``, ``AMENDS_SECTION``, ``SUPERSEDES_DOC`` (1..3 hop supersession chains)

All user/seed inputs enter strictly via BigQuery query parameters (``@seed_doc_keys``,
``@seed_section_keys``, ``@solicitation_id``, ``@section_id``, ``@caller_groups``).
"""

from __future__ import annotations

import re

from app.ingestion.bq_indexer import ACL_PREDICATE_SQL, _fqn, _validate_identifier

DEFAULT_GRAPH_NAME = "procurement_graph"
GRAPH_DOCS_TABLE = "graph_contract_docs"
GRAPH_SECTIONS_TABLE = "graph_contract_sections"
GRAPH_DEFINES_TABLE = "graph_defines_section_edges"
GRAPH_AMENDS_TABLE = "graph_amends_section_edges"
GRAPH_SUPERSEDES_TABLE = "graph_supersedes_doc_edges"
GRAPH_TABLE_NAMES: tuple[str, ...] = (
    GRAPH_DOCS_TABLE,
    GRAPH_SECTIONS_TABLE,
    GRAPH_DEFINES_TABLE,
    GRAPH_AMENDS_TABLE,
    GRAPH_SUPERSEDES_TABLE,
)
_GRAPH_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def validate_graph_name(graph_name: str) -> str:
    """Validate a BigQuery Property Graph identifier."""
    if not isinstance(graph_name, str) or not _GRAPH_NAME_RE.fullmatch(graph_name.strip()):
        raise ValueError(
            f"Invalid BigQuery Property Graph name: {graph_name!r}; expected [a-zA-Z_][a-zA-Z0-9_]{{0,62}}"
        )
    return graph_name.strip()


def build_graph_table_ddl(
    project_id: str,
    dataset_id: str,
    chunks_table_id: str = "document_chunks_v3",
) -> tuple[str, ...]:
    """Return 5 idempotent SQL statements materializing the procurement graph node and edge tables."""
    proj, dataset, chunks_tbl = _fqn(project_id, dataset_id, chunks_table_id)
    src = f"`{proj}.{dataset}.{chunks_tbl}`"
    docs_tbl = f"`{proj}.{dataset}.{GRAPH_DOCS_TABLE}`"
    secs_tbl = f"`{proj}.{dataset}.{GRAPH_SECTIONS_TABLE}`"
    def_tbl = f"`{proj}.{dataset}.{GRAPH_DEFINES_TABLE}`"
    amd_tbl = f"`{proj}.{dataset}.{GRAPH_AMENDS_TABLE}`"
    sup_tbl = f"`{proj}.{dataset}.{GRAPH_SUPERSEDES_TABLE}`"

    docs_sql = f"""CREATE OR REPLACE TABLE {docs_tbl} AS
SELECT
  CONCAT(solicitation_id, '::', doc_name) AS doc_key,
  solicitation_id,
  doc_name,
  ANY_VALUE(doc_type) AS doc_type,
  MAX(COALESCE(amendment_number, 0)) AS amendment_number,
  CAST(MAX(effective_date) AS STRING) AS effective_date,
  ANY_VALUE(allowed_groups) AS allowed_groups
FROM {src}
WHERE tier_profile = 'multimodal'
  AND NOT STARTS_WITH(solicitation_id, 'upl_')
GROUP BY solicitation_id, doc_name;"""

    secs_sql = f"""CREATE OR REPLACE TABLE {secs_tbl} AS
WITH base_sections AS (
  SELECT DISTINCT
    solicitation_id,
    UPPER(TRIM(section_id)) AS section_id
  FROM {src}
  WHERE tier_profile = 'multimodal'
    AND NOT STARTS_WITH(solicitation_id, 'upl_')
    AND section_id IS NOT NULL
    AND TRIM(section_id) != ''
),
amended_sections AS (
  SELECT DISTINCT
    solicitation_id,
    UPPER(TRIM(target)) AS section_id
  FROM {src},
  UNNEST(SPLIT(IFNULL(amends_section, ''), ',')) AS target
  WHERE tier_profile = 'multimodal'
    AND NOT STARTS_WITH(solicitation_id, 'upl_')
    AND TRIM(target) != ''
),
all_sections AS (
  SELECT solicitation_id, section_id FROM base_sections
  UNION DISTINCT
  SELECT solicitation_id, section_id FROM amended_sections
)
SELECT
  CONCAT(solicitation_id, '::', section_id) AS section_key,
  solicitation_id,
  section_id
FROM all_sections;"""

    defines_sql = f"""CREATE OR REPLACE TABLE {def_tbl} AS
WITH explicit_defs AS (
  SELECT DISTINCT
    CONCAT(solicitation_id, '::', doc_name) AS doc_key,
    CONCAT(solicitation_id, '::', UPPER(TRIM(section_id))) AS section_key,
    solicitation_id
  FROM {src}
  WHERE tier_profile = 'multimodal'
    AND NOT STARTS_WITH(solicitation_id, 'upl_')
    AND COALESCE(amendment_number, 0) = 0
    AND section_id IS NOT NULL
    AND TRIM(section_id) != ''
),
inferred_defs AS (
  SELECT DISTINCT
    d.doc_key,
    s.section_key,
    s.solicitation_id
  FROM {secs_tbl} s
  JOIN {docs_tbl} d
    ON d.solicitation_id = s.solicitation_id
   AND d.amendment_number = 0
  LEFT JOIN explicit_defs e
    ON e.section_key = s.section_key
  WHERE e.section_key IS NULL
    AND (
      (STARTS_WITH(s.section_id, 'CLIN-') AND d.doc_type = 'PRICING_CLIN')
      OR (STARTS_WITH(s.section_id, 'SEC-') AND d.doc_type = 'BASE_RFP')
      OR (STARTS_WITH(s.section_id, 'SLA-') AND d.doc_type = 'SLA_MATRIX')
      OR (STARTS_WITH(s.section_id, 'DATA-SLA-') AND d.doc_type = 'BASE_RFP')
    )
),
combined AS (
  SELECT doc_key, section_key, solicitation_id FROM explicit_defs
  UNION DISTINCT
  SELECT doc_key, section_key, solicitation_id FROM inferred_defs
)
SELECT
  CONCAT(doc_key, '->DEF->', section_key) AS edge_id,
  doc_key,
  section_key,
  solicitation_id
FROM combined;"""

    amends_sql = f"""CREATE OR REPLACE TABLE {amd_tbl} AS
WITH raw_amends AS (
  SELECT
    solicitation_id,
    doc_name,
    UPPER(TRIM(target)) AS norm_target,
    COALESCE(amendment_number, 0) AS amendment_number,
    effective_date,
    chunk_id
  FROM {src},
  UNNEST(SPLIT(IFNULL(amends_section, ''), ',')) AS target
  WHERE tier_profile = 'multimodal'
    AND NOT STARTS_WITH(solicitation_id, 'upl_')
    AND COALESCE(amendment_number, 0) > 0
    AND TRIM(target) != ''
)
SELECT
  CONCAT(solicitation_id, '::', doc_name, '->AMD->', solicitation_id, '::', norm_target) AS edge_id,
  CONCAT(solicitation_id, '::', doc_name) AS doc_key,
  CONCAT(solicitation_id, '::', norm_target) AS section_key,
  solicitation_id,
  MAX(amendment_number) AS amendment_number,
  CAST(MAX(effective_date) AS STRING) AS effective_date,
  ANY_VALUE(chunk_id) AS source_chunk_id
FROM raw_amends
GROUP BY solicitation_id, doc_name, norm_target;"""

    supersedes_sql = f"""CREATE OR REPLACE TABLE {sup_tbl} AS
WITH section_supersedes AS (
  SELECT DISTINCT
    a.doc_key AS amendment_doc_key,
    d.doc_key AS base_doc_key,
    s.section_id,
    a.solicitation_id,
    a.amendment_number,
    a.effective_date
  FROM {amd_tbl} a
  JOIN {def_tbl} d
    ON d.section_key = a.section_key
   AND d.solicitation_id = a.solicitation_id
  JOIN {secs_tbl} s
    ON s.section_key = a.section_key
  WHERE a.doc_key != d.doc_key
),
sequential_amendments AS (
  SELECT DISTINCT
    newer.doc_key AS amendment_doc_key,
    older.doc_key AS base_doc_key,
    CONCAT('AMENDMENT_', CAST(older.amendment_number AS STRING)) AS section_id,
    newer.solicitation_id,
    newer.amendment_number,
    newer.effective_date
  FROM {docs_tbl} newer
  JOIN {docs_tbl} older
    ON newer.solicitation_id = older.solicitation_id
   AND newer.amendment_number = older.amendment_number + 1
   AND older.amendment_number > 0
),
combined AS (
  SELECT * FROM section_supersedes
  UNION DISTINCT
  SELECT * FROM sequential_amendments
)
SELECT
  CONCAT(amendment_doc_key, '->SUP(', section_id, ')->', base_doc_key) AS edge_id,
  amendment_doc_key,
  base_doc_key,
  section_id,
  solicitation_id,
  amendment_number,
  effective_date
FROM combined;"""

    return (docs_sql, secs_sql, defines_sql, amends_sql, supersedes_sql)


def build_property_graph_ddl(
    project_id: str,
    dataset_id: str,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> str:
    """Return ``CREATE OR REPLACE PROPERTY GRAPH`` DDL mapping ContractDoc/ContractSection nodes & edges."""
    proj = _validate_identifier(project_id, "project_id")
    dataset = _validate_identifier(dataset_id, "dataset_id")
    gname = validate_graph_name(graph_name)
    return f"""CREATE OR REPLACE PROPERTY GRAPH `{proj}.{dataset}.{gname}`
NODE TABLES (
  `{proj}.{dataset}.{GRAPH_DOCS_TABLE}` AS ContractDoc
    KEY (doc_key)
    LABEL ContractDoc
    PROPERTIES (doc_key, solicitation_id, doc_name, doc_type, amendment_number, effective_date),
  `{proj}.{dataset}.{GRAPH_SECTIONS_TABLE}` AS ContractSection
    KEY (section_key)
    LABEL ContractSection
    PROPERTIES (section_key, solicitation_id, section_id)
)
EDGE TABLES (
  `{proj}.{dataset}.{GRAPH_DEFINES_TABLE}` AS DEFINES_SECTION
    KEY (edge_id)
    SOURCE KEY (doc_key) REFERENCES ContractDoc (doc_key)
    DESTINATION KEY (section_key) REFERENCES ContractSection (section_key)
    LABEL DEFINES_SECTION
    PROPERTIES (edge_id, solicitation_id),
  `{proj}.{dataset}.{GRAPH_AMENDS_TABLE}` AS AMENDS_SECTION
    KEY (edge_id)
    SOURCE KEY (doc_key) REFERENCES ContractDoc (doc_key)
    DESTINATION KEY (section_key) REFERENCES ContractSection (section_key)
    LABEL AMENDS_SECTION
    PROPERTIES (edge_id, solicitation_id, amendment_number, effective_date, source_chunk_id),
  `{proj}.{dataset}.{GRAPH_SUPERSEDES_TABLE}` AS SUPERSEDES_DOC
    KEY (edge_id)
    SOURCE KEY (amendment_doc_key) REFERENCES ContractDoc (doc_key)
    DESTINATION KEY (base_doc_key) REFERENCES ContractDoc (doc_key)
    LABEL SUPERSEDES_DOC
    PROPERTIES (edge_id, solicitation_id, section_id, amendment_number, effective_date)
);"""


def build_graph_expand_gql(
    project_id: str,
    dataset_id: str,
    chunks_table_id: str = "document_chunks_v3",
    graph_name: str = DEFAULT_GRAPH_NAME,
    *,
    include_acl_filter: bool = False,
    limit: int = 12,
) -> str:
    """Build parameterized BigQuery ISO GQL ``GRAPH_TABLE`` query traversing 1..3 hop supersession & amendment edges.

    Parameters:
      * ``@seed_doc_keys`` (ARRAY<STRING>): ``solicitation_id::doc_name`` keys from hybrid retrieval.
      * ``@seed_section_keys`` (ARRAY<STRING>): ``solicitation_id::UPPER(section_id)`` keys from hybrid retrieval.
      * ``@caller_groups`` (ARRAY<STRING>, when ``include_acl_filter=True``): authorized ACL groups.
    """
    proj, dataset, chunks_tbl = _fqn(project_id, dataset_id, chunks_table_id)
    gname = validate_graph_name(graph_name)
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 50):
        raise ValueError("limit must be an integer between 1 and 50")
    acl_clause = f"\n  {ACL_PREDICATE_SQL.lstrip()}" if include_acl_filter else ""
    graph_fqn = f"`{proj}.{dataset}.{gname}`"
    chunks_fqn = f"`{proj}.{dataset}.{chunks_tbl}`"
    return f"""WITH doc_hops AS (
  SELECT
    amd_doc_key,
    amd_doc_name,
    amd_solicitation_id,
    base_doc_key,
    base_doc_name,
    hop_count,
    amd_number,
    amd_effective_date,
    'SUPERSEDES_DOC' AS edge_type,
    base_doc_name AS target_label
  FROM GRAPH_TABLE (
    {graph_fqn}
    MATCH p = (amd:ContractDoc)-[e:SUPERSEDES_DOC]->{{1,3}}(base:ContractDoc)
    COLUMNS (
      amd.doc_key AS amd_doc_key,
      amd.doc_name AS amd_doc_name,
      amd.solicitation_id AS amd_solicitation_id,
      base.doc_key AS base_doc_key,
      base.doc_name AS base_doc_name,
      PATH_LENGTH(p) AS hop_count,
      amd.amendment_number AS amd_number,
      amd.effective_date AS amd_effective_date
    )
  )
  WHERE base_doc_key IN UNNEST(@seed_doc_keys)
),
section_hops AS (
  SELECT
    amd_doc_key,
    amd_doc_name,
    amd_solicitation_id,
    sec_key AS base_doc_key,
    sec_id AS base_doc_name,
    1 AS hop_count,
    amd_number,
    amd_effective_date,
    'AMENDS_SECTION' AS edge_type,
    sec_id AS target_label
  FROM GRAPH_TABLE (
    {graph_fqn}
    MATCH (amd:ContractDoc)-[a:AMENDS_SECTION]->(sec:ContractSection)
    COLUMNS (
      amd.doc_key AS amd_doc_key,
      amd.doc_name AS amd_doc_name,
      amd.solicitation_id AS amd_solicitation_id,
      sec.section_key AS sec_key,
      sec.section_id AS sec_id,
      a.amendment_number AS amd_number,
      a.effective_date AS amd_effective_date
    )
  )
  WHERE sec_key IN UNNEST(@seed_section_keys)
),
discovered AS (
  SELECT
    amd_doc_key,
    amd_doc_name,
    amd_solicitation_id,
    edge_type,
    target_label,
    MIN(hop_count) AS hop_count,
    MAX(amd_number) AS graph_amendment_number,
    MAX(amd_effective_date) AS graph_effective_date
  FROM (
    SELECT * FROM doc_hops
    UNION ALL
    SELECT * FROM section_hops
  )
  GROUP BY amd_doc_key, amd_doc_name, amd_solicitation_id, edge_type, target_label
)
SELECT
  c.chunk_id,
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
  d.edge_type,
  d.target_label,
  d.hop_count
FROM discovered d
JOIN {chunks_fqn} c
  ON c.solicitation_id = d.amd_solicitation_id
 AND c.doc_name = d.amd_doc_name
WHERE c.tier_profile = 'multimodal'
  AND NOT STARTS_WITH(c.solicitation_id, 'upl_')
  AND COALESCE(c.amendment_number, 0) > 0{acl_clause}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY c.chunk_id
  ORDER BY d.hop_count ASC, d.edge_type ASC, d.target_label ASC
) = 1
ORDER BY c.amendment_number DESC, d.hop_count ASC, c.page_number ASC, c.chunk_id ASC
LIMIT {limit};"""


def build_graph_amendment_lookup_gql(
    project_id: str,
    dataset_id: str,
    chunks_table_id: str = "document_chunks_v3",
    graph_name: str = DEFAULT_GRAPH_NAME,
    *,
    limit: int = 6,
    include_acl_filter: bool = False,
) -> str:
    """Parameterized ISO GQL ``GRAPH_TABLE`` lookup for ADK ``check_amendments`` over ``procurement_graph``."""
    proj, dataset, chunks_tbl = _fqn(project_id, dataset_id, chunks_table_id)
    gname = validate_graph_name(graph_name)
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 20):
        raise ValueError("limit must be an integer between 1 and 20")
    acl_clause = f"\n  {ACL_PREDICATE_SQL.lstrip()}" if include_acl_filter else ""
    graph_fqn = f"`{proj}.{dataset}.{gname}`"
    chunks_fqn = f"`{proj}.{dataset}.{chunks_tbl}`"
    return f"""WITH matched_amendments AS (
  SELECT DISTINCT
    amd_solicitation_id,
    amd_doc_name,
    sec_id,
    amd_number,
    amd_effective_date
  FROM GRAPH_TABLE (
    {graph_fqn}
    MATCH (amd:ContractDoc)-[a:AMENDS_SECTION]->(sec:ContractSection)
    COLUMNS (
      amd.solicitation_id AS amd_solicitation_id,
      amd.doc_name AS amd_doc_name,
      sec.section_id AS sec_id,
      a.amendment_number AS amd_number,
      a.effective_date AS amd_effective_date
    )
  )
  WHERE amd_solicitation_id = @solicitation_id
    AND (
      UPPER(sec_id) = UPPER(@section_id)
      OR STARTS_WITH(UPPER(sec_id), CONCAT(UPPER(@section_id), '.'))
      OR STARTS_WITH(UPPER(@section_id), CONCAT(UPPER(sec_id), '.'))
    )
)
SELECT
  c.chunk_id,
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
  c.bbox_source
FROM matched_amendments m
JOIN {chunks_fqn} c
  ON c.solicitation_id = m.amd_solicitation_id
 AND c.doc_name = m.amd_doc_name
WHERE c.tier_profile = 'multimodal'
  AND COALESCE(c.amendment_number, 0) > 0{acl_clause}
ORDER BY c.amendment_number DESC, c.effective_date DESC, LENGTH(c.content) DESC, c.chunk_id
LIMIT {limit};"""


__all__ = [
    "DEFAULT_GRAPH_NAME",
    "GRAPH_AMENDS_TABLE",
    "GRAPH_DEFINES_TABLE",
    "GRAPH_DOCS_TABLE",
    "GRAPH_SECTIONS_TABLE",
    "GRAPH_SUPERSEDES_TABLE",
    "GRAPH_TABLE_NAMES",
    "build_graph_amendment_lookup_gql",
    "build_graph_expand_gql",
    "build_graph_table_ddl",
    "build_property_graph_ddl",
    "validate_graph_name",
]
