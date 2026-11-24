"""Level 5: Cloud SQL for PostgreSQL + pgvector 0.8.5 Hybrid (``cloudsql_pgvector``) — HNSW halfvec + GIN FTS + RLS.

Executes parameterized hybrid (or dense-only ablation) SQL inside a Cloud SQL PostgreSQL 16 transaction using ``psycopg``:
  1. ``SET LOCAL hnsw.ef_search = 40`` (and ``SET LOCAL hnsw.iterative_scan = 'relaxed_order'`` when filtered/RLS),
  2. Dense cosine leg ``embedding <=> %(query_vector)s::halfvec`` over ``halfvec(3072)`` HNSW index (supporting native
     3072-d ``gemini-embedding-2`` vectors beyond ``vector``'s 2000-d HNSW cap with $0 re-embedding) + PostgreSQL
     ``ts_rank_cd`` GIN full-text leg,
  3. In-database Reciprocal Rank Fusion (``k = 60``), PostgreSQL Row-Level Security (``CREATE POLICY`` + GIN
     ``allowed_groups``), followed by Discovery Engine Ranking API cross-encoder reranking and catalog LLM synthesis.

Replay is served by ``ReplayRAGEngine``; live/record fails loud when ``CLOUDSQL_DSN`` is unset or the table is empty.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time
from typing import Any, Callable

from psycopg import sql as pg_sql

from app.core import gcp_clients
from app.core.acl import CallerAccessContext, acl_refusal_answer, acl_stage_metadata
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_cloudsql_client
from app.core.models import EngineFlags, EngineResult, TokenUsage
from app.core.models_catalog import ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient, ranking_url, redact_project
from app.core.telemetry import StageEmitter
from app.engines.alloydb_scann import (
    RRF_K,
    _rows_to_candidates,
    format_pgvector_literal,
    validate_pg_identifier,
)
from app.engines.base import BaseRAGEngine
from app.engines.common_stages import embed_query_stage, synthesize_stage
from app.engines.level2_hybrid import (
    ACRONYM_EXPANSIONS,
    HybridCandidate,
    PreparedQuery,
    RerankedCandidate,
    apply_rank_scores,
    build_rank_records,
    build_ranking_client,
    prepare_query,
)
from app.recording.redaction import redact_text, redaction_tokens

LAYOUT_SOURCE = "cloudsql_pgvector"
RETRIEVE_SERVICE = "Cloud SQL PostgreSQL 16 · pgvector 0.8.5 HNSW + GIN FTS (RRF)"
DENSE_RETRIEVE_SERVICE = "Cloud SQL PostgreSQL 16 · pgvector 0.8.5 HNSW (Dense-Only)"
HNSW_DEFAULT_M = 16
HNSW_DEFAULT_EF_CONSTRUCTION = 64
HNSW_DEFAULT_EF_SEARCH = 40
_EMBEDDING_PREVIEW_DIMS = 4
_MAX_CONTEXT_CHUNK_CHARS = 2400
_CONN_EXEC_LOCK = threading.Lock()


def build_cloudsql_pgvector_ddl(
    *,
    table: str = "document_chunks",
    vector_dim: int = 3072,
    hnsw_m: int = HNSW_DEFAULT_M,
    hnsw_ef_construction: int = HNSW_DEFAULT_EF_CONSTRUCTION,
) -> tuple[str, ...]:
    """Return DDL statements initializing ``vector`` (pgvector 0.8.5), ``halfvec(3072)`` HNSW, GIN FTS & Postgres RLS."""
    ident = validate_pg_identifier(table, "table")
    if not isinstance(vector_dim, int) or not 1 <= vector_dim <= 4000:
        raise ValueError("vector_dim must be in [1, 4000] for pgvector halfvec HNSW")
    if not isinstance(hnsw_m, int) or not 4 <= hnsw_m <= 64:
        raise ValueError("hnsw_m must be in [4, 64]")
    if not isinstance(hnsw_ef_construction, int) or not 16 <= hnsw_ef_construction <= 512:
        raise ValueError("hnsw_ef_construction must be in [16, 512]")
    hnsw_idx = validate_pg_identifier(f"idx_{ident[:40]}_hnsw", "hnsw_index")
    fts_idx = validate_pg_identifier(f"idx_{ident[:40]}_fts", "fts_index")
    sol_idx = validate_pg_identifier(f"idx_{ident[:40]}_tier_sol", "filter_index")
    acl_idx = validate_pg_identifier(f"idx_{ident[:40]}_acl", "acl_index")
    rls_policy = validate_pg_identifier(f"rls_{ident[:40]}_acl", "rls_policy")
    return (
        "CREATE EXTENSION IF NOT EXISTS vector;",
        f"""CREATE TABLE IF NOT EXISTS "{ident}" (
    chunk_id TEXT PRIMARY KEY,
    tier_profile TEXT NOT NULL DEFAULT 'hybrid',
    solicitation_id TEXT NOT NULL,
    doc_name TEXT NOT NULL,
    doc_type TEXT NOT NULL DEFAULT 'SOLICITATION',
    element_type TEXT NOT NULL DEFAULT 'narrative_text',
    section_id TEXT,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    effective_date TEXT,
    amends_section TEXT,
    page_number INTEGER NOT NULL DEFAULT 1,
    bbox_json JSONB,
    content TEXT NOT NULL,
    allowed_groups TEXT[] NOT NULL DEFAULT ARRAY['corp_all', 'exec_admin'],
    embedding halfvec({vector_dim}) NOT NULL
);""",
        f'CREATE INDEX IF NOT EXISTS "{sol_idx}" ON "{ident}" (tier_profile, solicitation_id);',
        f'CREATE INDEX IF NOT EXISTS "{acl_idx}" ON "{ident}" USING GIN (allowed_groups);',
        f'CREATE INDEX IF NOT EXISTS "{fts_idx}" ON "{ident}" USING GIN (to_tsvector(\'english\', content));',
        f'ALTER TABLE "{ident}" ENABLE ROW LEVEL SECURITY;',
        f'DROP POLICY IF EXISTS "{rls_policy}" ON "{ident}";',
        f"""CREATE POLICY "{rls_policy}" ON "{ident}" FOR SELECT
USING (
    COALESCE(current_setting('app.caller_groups', true), '') = ''
    OR allowed_groups && string_to_array(current_setting('app.caller_groups', true), ',')
);""",
        f'CREATE INDEX IF NOT EXISTS "{hnsw_idx}" ON "{ident}" USING hnsw (embedding halfvec_cosine_ops) WITH (m = {hnsw_m}, ef_construction = {hnsw_ef_construction});',
    )


def build_cloudsql_hybrid_search_sql(
    *,
    table: str,
    query_vector: list[float],
    query_text: str,
    candidate_k: int = 25,
    solicitation_filter: str | None = None,
    rrf_k: int = RRF_K,
    caller_groups: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build parameterized Cloud SQL pgvector ``halfvec`` HNSW cosine + GIN FTS RRF SQL."""
    ident = validate_pg_identifier(table, "table")
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query_text must be a non-empty string")
    if not isinstance(candidate_k, int) or isinstance(candidate_k, bool) or not 1 <= candidate_k <= 100:
        raise ValueError("candidate_k must be an integer in [1, 100]")
    if not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValueError("rrf_k must be >= 1")

    has_sol = bool(solicitation_filter and solicitation_filter.strip())
    sol_clause = " AND solicitation_id = %(solicitation_id)s" if has_sol else ""
    has_acl = bool(caller_groups)
    acl_clause = " AND allowed_groups && %(caller_groups)s::text[]" if has_acl else ""
    quoted_tbl = f'"{ident}"'
    sql_text = f"""WITH vec_leg AS (
    SELECT
        chunk_id,
        (embedding <=> %(query_vector)s::halfvec) AS vec_distance,
        ROW_NUMBER() OVER (ORDER BY embedding <=> %(query_vector)s::halfvec ASC, chunk_id ASC) AS vec_rank
    FROM {quoted_tbl}
    WHERE tier_profile = 'hybrid'{sol_clause}{acl_clause}
    ORDER BY embedding <=> %(query_vector)s::halfvec ASC
    LIMIT %(candidate_k)s
),
lex_leg AS (
    SELECT
        chunk_id,
        ts_rank_cd(to_tsvector('english', content), websearch_to_tsquery('english', %(query_text)s)) AS lex_score,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(to_tsvector('english', content), websearch_to_tsquery('english', %(query_text)s)) DESC,
            chunk_id ASC
        ) AS lex_rank
    FROM {quoted_tbl}
    WHERE tier_profile = 'hybrid'{sol_clause}{acl_clause}
      AND to_tsvector('english', content) @@ websearch_to_tsquery('english', %(query_text)s)
    LIMIT %(candidate_k)s
),
fused AS (
    SELECT
        COALESCE(v.chunk_id, l.chunk_id) AS chunk_id,
        v.vec_distance,
        v.vec_rank,
        l.lex_score,
        l.lex_rank,
        (COALESCE(1.0 / (%(rrf_k)s + v.vec_rank), 0.0) + COALESCE(1.0 / (%(rrf_k)s + l.lex_rank), 0.0)) AS rrf_score
    FROM vec_leg v
    FULL OUTER JOIN lex_leg l ON v.chunk_id = l.chunk_id
)
SELECT
    c.chunk_id,
    c.doc_name,
    c.solicitation_id,
    c.doc_type,
    c.page_number,
    c.section_id,
    c.content,
    c.element_type,
    c.amendment_number,
    c.effective_date,
    c.amends_section,
    c.bbox_json,
    f.vec_distance,
    f.vec_rank,
    f.lex_score,
    f.lex_rank,
    f.rrf_score
FROM fused f
JOIN {quoted_tbl} c ON c.chunk_id = f.chunk_id
ORDER BY f.rrf_score DESC, COALESCE(f.vec_rank, 9999) ASC, c.chunk_id ASC
LIMIT %(candidate_k)s;"""

    params: dict[str, Any] = {
        "query_vector": format_pgvector_literal(query_vector),
        "query_text": query_text.strip(),
        "candidate_k": candidate_k,
        "rrf_k": rrf_k,
    }
    if has_sol and solicitation_filter is not None:
        params["solicitation_id"] = solicitation_filter.strip()
    if has_acl and caller_groups is not None:
        params["caller_groups"] = list(caller_groups)
    return sql_text, params


def build_cloudsql_vector_search_sql(
    *,
    table: str,
    query_vector: list[float],
    candidate_k: int = 25,
    solicitation_filter: str | None = None,
    caller_groups: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build parameterized dense-only Cloud SQL pgvector HNSW cosine SQL (for Workbench ablation)."""
    ident = validate_pg_identifier(table, "table")
    if not isinstance(candidate_k, int) or isinstance(candidate_k, bool) or not 1 <= candidate_k <= 100:
        raise ValueError("candidate_k must be an integer in [1, 100]")
    has_sol = bool(solicitation_filter and solicitation_filter.strip())
    sol_clause = " AND solicitation_id = %(solicitation_id)s" if has_sol else ""
    has_acl = bool(caller_groups)
    acl_clause = " AND allowed_groups && %(caller_groups)s::text[]" if has_acl else ""
    quoted_tbl = f'"{ident}"'
    sql_text = f"""SELECT
    c.chunk_id,
    c.doc_name,
    c.solicitation_id,
    c.doc_type,
    c.page_number,
    c.section_id,
    c.content,
    c.element_type,
    c.amendment_number,
    c.effective_date,
    c.amends_section,
    c.bbox_json,
    (c.embedding <=> %(query_vector)s::halfvec) AS vec_distance,
    ROW_NUMBER() OVER (ORDER BY c.embedding <=> %(query_vector)s::halfvec ASC, c.chunk_id ASC) AS vec_rank,
    CAST(NULL AS DOUBLE PRECISION) AS lex_score,
    CAST(NULL AS BIGINT) AS lex_rank,
    (1.0 - (c.embedding <=> %(query_vector)s::halfvec)) AS rrf_score
FROM {quoted_tbl} c
WHERE c.tier_profile = 'hybrid'{sol_clause}{acl_clause}
ORDER BY c.embedding <=> %(query_vector)s::halfvec ASC, c.chunk_id ASC
LIMIT %(candidate_k)s;"""
    params: dict[str, Any] = {
        "query_vector": format_pgvector_literal(query_vector),
        "candidate_k": candidate_k,
    }
    if has_sol and solicitation_filter is not None:
        params["solicitation_id"] = solicitation_filter.strip()
    if has_acl and caller_groups is not None:
        params["caller_groups"] = list(caller_groups)
    return sql_text, params


class CloudSQLPgVectorEngine(BaseRAGEngine):
    """Level 5: Ephemeral Cloud SQL PostgreSQL 16 + pgvector 0.8.5 HNSW (halfvec 3072-d) + GIN FTS + RLS."""

    engine_id: str = "cloudsql_pgvector"
    engine_label: str = "Level 5: Cloud SQL PostgreSQL + pgvector"
    tier_profile: str = LAYOUT_SOURCE
    stages: tuple[str, ...] = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(
        self,
        settings: ArenaSettings | None = None,
        *,
        synthesis_model: ModelSpec | None = None,
        ranking_client: RankingClient | None = None,
        conn_factory: Callable[[ArenaSettings], Any] | None = None,
    ) -> None:
        super().__init__(settings=settings)
        if synthesis_model is not None and not isinstance(synthesis_model, ModelSpec):
            raise TypeError("synthesis_model must be a ModelSpec or None")
        if ranking_client is not None and not isinstance(ranking_client, RankingClient):
            raise TypeError("ranking_client must be a RankingClient or None")
        self.synthesis_model: ModelSpec = synthesis_model or spec_for_settings_model(
            self.settings, self.settings.gemini_fast_model
        )
        self._ranking_client = ranking_client
        self._conn_factory = conn_factory or get_cloudsql_client

    async def _query_prep(self, query: str, emitter: StageEmitter) -> PreparedQuery:
        async with self.stage(
            emitter,
            "query_prep",
            label="Query preparation (acronym expansion)",
            service="Engine (local)",
            request={"query": query, "acronym_map": [{"term": t, "expansion": e} for t, e in ACRONYM_EXPANSIONS]},
        ) as ctx:
            prepared = prepare_query(query)
            ctx.payload = prepared.payload()
        return prepared

    def _execute_sql(
        self,
        sql_text: str,
        params: dict[str, Any],
        *,
        filtered: bool,
        caller_groups: tuple[str, ...] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        tokens = redaction_tokens(self.settings)
        try:
            conn = self._conn_factory(self.settings)
            with _CONN_EXEC_LOCK:
                t0 = time.perf_counter()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            pg_sql.SQL("SET LOCAL hnsw.ef_search = {};").format(
                                pg_sql.Literal(HNSW_DEFAULT_EF_SEARCH)
                            )
                        )
                        if filtered or caller_groups:
                            cur.execute(pg_sql.SQL("SET LOCAL hnsw.iterative_scan = 'relaxed_order';"))
                        if caller_groups:
                            cur.execute(
                                pg_sql.SQL("SELECT set_config('app.caller_groups', {}, true);").format(
                                    pg_sql.Literal(",".join(caller_groups))
                                )
                            )
                        cur.execute(sql_text, params)
                        cols = [col.name for col in (cur.description or [])]
                        raw_rows = cur.fetchall()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                db_ms = max(1, int((time.perf_counter() - t0) * 1000))
        except Exception as exc:
            safe_msg = redact_text(str(exc), tokens)
            raise RuntimeError(f"Cloud SQL pgvector query failed: {safe_msg}") from exc
        return [dict(zip(cols, r)) for r in raw_rows], db_ms

    async def _retrieve_candidates(
        self,
        vector: list[float],
        original_query: str,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        *,
        candidate_k: int | None = None,
        caller_access: CallerAccessContext | None = None,
        dense_only: bool = False,
    ) -> list[HybridCandidate]:
        eff_k = int(candidate_k or self.settings.cloudsql_candidate_k)
        restricted = bool(caller_access and caller_access.is_restricted)
        caller_groups = caller_access.effective_groups if restricted and caller_access is not None else None
        if dense_only:
            sql_text, params = build_cloudsql_vector_search_sql(
                table=self.settings.cloudsql_table,
                query_vector=vector,
                candidate_k=eff_k,
                solicitation_filter=solicitation_filter,
                caller_groups=caller_groups,
            )
        else:
            sql_text, params = build_cloudsql_hybrid_search_sql(
                table=self.settings.cloudsql_table,
                query_vector=vector,
                query_text=original_query,
                candidate_k=eff_k,
                solicitation_filter=solicitation_filter,
                caller_groups=caller_groups,
            )
        shown_params: dict[str, Any] = {
            "query_vector": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
            "candidate_k": eff_k,
            "hnsw.ef_search": HNSW_DEFAULT_EF_SEARCH,
        }
        if not dense_only:
            shown_params["query_text"] = original_query
            shown_params["rrf_k"] = RRF_K
        if solicitation_filter:
            shown_params["solicitation_id"] = solicitation_filter
        if caller_groups:
            shown_params["caller_groups"] = list(caller_groups)

        async with self.stage(
            emitter,
            "retrieve",
            label=(
                "Cloud SQL pgvector HNSW cosine (dense-only)"
                if dense_only
                else "Cloud SQL pgvector HNSW + GIN FTS (in-DB RRF)"
            ),
            service=DENSE_RETRIEVE_SERVICE if dense_only else RETRIEVE_SERVICE,
            request={
                "dsn": "postgresql://${CLOUDSQL_DSN}",
                "table": self.settings.cloudsql_table,
                "sql": sql_text,
                "parameters": shown_params,
            },
        ) as ctx:
            rows, db_ms = await self.blocking(
                self._execute_sql,
                sql_text,
                params,
                filtered=bool(solicitation_filter),
                caller_groups=caller_groups,
            )
            candidates = _rows_to_candidates(rows, tier_profile=self.tier_profile)
            # Tag layout_source honestly as cloudsql_pgvector
            candidates = [
                replace(c, chunk=replace(c.chunk, layout_source=LAYOUT_SOURCE))
                for c in candidates
            ]
            if not candidates and not restricted:
                raise RuntimeError(
                    f"Cloud SQL pgvector table '{self.settings.cloudsql_table}' returned 0 rows — "
                    "run scripts/ephemeral_cloudsql.sh --up to load the corpus or use DEMO_MODE=replay"
                )
            ctx.payload = {
                "table": self.settings.cloudsql_table,
                "index_type": (
                    f"pgvector HNSW (halfvec 3072-d, m={HNSW_DEFAULT_M}, ef_search={HNSW_DEFAULT_EF_SEARCH})"
                    if dense_only
                    else f"pgvector HNSW (halfvec 3072-d, m={HNSW_DEFAULT_M}, ef_search={HNSW_DEFAULT_EF_SEARCH}) + GIN FTS"
                ),
                "fusion": "dense_only" if dense_only else f"RRF(k={RRF_K})",
                "candidate_k": eff_k,
                "rows_returned": len(candidates),
                "db_exec_ms": db_ms,
                "candidates": [c.summary() for c in candidates],
                **(
                    {**acl_stage_metadata(self.engine_id, caller_access), "acl_access_denied": len(candidates) == 0}
                    if restricted
                    else {}
                ),
            }
        return candidates

    async def _rerank(
        self,
        query: str,
        candidates: list[HybridCandidate],
        emitter: StageEmitter,
        *,
        top_n: int | None = None,
    ) -> list[RerankedCandidate]:
        eff_top_n = min(int(top_n or self.settings.rerank_top_n), len(candidates))
        client = self._ranking_client or build_ranking_client(self.settings)
        records = build_rank_records(candidates)
        endpoint = redact_project(ranking_url(self.settings.gcp_project_id), self.settings.gcp_project_id)
        async with self.stage(
            emitter,
            "rerank",
            label="Cross-encoder reranking",
            service=f"Discovery Engine Ranking API · {self.settings.reranker_model}",
            request={
                "endpoint": endpoint,
                "model": self.settings.reranker_model,
                "top_n": eff_top_n,
                "records_sent": len(records),
                "query": query,
            },
        ) as ctx:
            response = await self.blocking(client.rank, query, records, len(records))
            ranked = apply_rank_scores(candidates, response.scores)
            counts = rank_billed_counts(len(records))
            ctx.payload = {
                "model": self.settings.reranker_model,
                "candidates_in": len(candidates),
                "candidates_out": eff_top_n,
                "results": [r.summary() for r in ranked],
                "rank_api_ms": response.duration_ms,
                "attempts": response.attempts,
                "records_truncated": sum(1 for r in records if r.content_truncated),
                "billed_counts": counts,
                "cost_usd": rank_cost_usd(counts),
            }
        return ranked

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        caller_access: CallerAccessContext | None = None,
    ) -> EngineResult:
        """Execute Level 5 live: query_prep -> embed_query -> Cloud SQL pgvector HNSW+FTS RRF -> Ranking API -> synthesize."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "CloudSQLPgVectorEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and (solicitation_filter.startswith("upl_") or solicitation_filter.startswith("upload_")):
            raise ValueError(
                "Upload workspaces (upl_*) are not indexed in ephemeral Cloud SQL pgvector benchmark instances; "
                "select the procurement benchmark corpus or run standing BigQuery/Managed levels on uploads"
            )
        if not self.settings.cloudsql_dsn.strip():
            raise RuntimeError(
                "CLOUDSQL_DSN is not configured — run scripts/ephemeral_cloudsql.sh --up or use DEMO_MODE=replay"
            )

        start = time.perf_counter()
        genai_client = gcp_clients.get_genai_client(self.settings)
        prepared = await self._query_prep(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(self, prepared.prepared, genai_client, emitter)
        candidates = await self._retrieve_candidates(
            vector, prepared.original, solicitation_filter, emitter, caller_access=caller_access
        )
        if not candidates and caller_access is not None and caller_access.is_restricted:
            await emitter.skipped("rerank", reason=f"0 authorized rows after PostgreSQL RLS ({caller_access.persona_id})")
            refusal = acl_refusal_answer(caller_access, cleaned)
            async with self.stage(
                emitter,
                "synthesize",
                label="Row-level security refusal (0 authorized rows)",
                service="ACL Policy (local)",
                request={"query": cleaned, "context_chunks": 0, **acl_stage_metadata(self.engine_id, caller_access)},
            ) as ctx:
                ctx.payload = {
                    "model": self.synthesis_model.id,
                    "answer": refusal,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "acl_access_denied": True,
                    **acl_stage_metadata(self.engine_id, caller_access),
                }
            usage = TokenUsage(
                embedding_tokens=embedding_tokens,
                model=self.synthesis_model.id,
                embedding_model=self.settings.text_embedding_model,
            )
            return EngineResult(
                engine_id=self.engine_id,
                engine_label=self.engine_label,
                answer=refusal,
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
                cost_usd=round(cost_from_usage(usage, self.synthesis_model.id), 8),
                stage_log=self.stage_log_of(emitter),
                usage=usage,
                flags=EngineFlags(reranker_used=False, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
            )

        ranked = await self._rerank(prepared.original, candidates, emitter)
        top = [replace(r.candidate.chunk, score=round(r.rerank_score, 4)) for r in ranked[: self.settings.rerank_top_n]]
        context_block = "\n\n".join(
            f"[{c.doc_name} | Sec: {c.section_id or 'GENERAL'} | p.{c.page_number}]: {c.content[:_MAX_CONTEXT_CHUNK_CHARS]}"
            for c in top
        )
        answer, usage = await synthesize_stage(
            self,
            self.synthesis_model,
            cleaned,
            context_block,
            len(top),
            emitter,
            embedding_tokens,
            extra_payload={
                "context_source": "Cloud SQL PostgreSQL 16 (pgvector 0.8.5 HNSW + GIN FTS RRF)",
                "context_chunks": len(top),
            },
        )
        rank_cost = rank_cost_usd(rank_billed_counts(len(candidates)))
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=answer,
            citations=self.chunks_to_citations(top, max_citations=len(top)),
            retrieved_chunks=top,
            agent_steps=[],
            latency_ms=max(1, int((time.perf_counter() - start) * 1000)),
            cost_usd=round(cost_from_usage(usage, self.synthesis_model.id) + rank_cost, 8),
            stage_log=self.stage_log_of(emitter),
            usage=usage,
            flags=EngineFlags(reranker_used=True, adk_runner_used=False, layout_source=LAYOUT_SOURCE),
        )


__all__ = [
    "CloudSQLPgVectorEngine",
    "DENSE_RETRIEVE_SERVICE",
    "HNSW_DEFAULT_EF_CONSTRUCTION",
    "HNSW_DEFAULT_EF_SEARCH",
    "HNSW_DEFAULT_M",
    "LAYOUT_SOURCE",
    "RETRIEVE_SERVICE",
    "build_cloudsql_hybrid_search_sql",
    "build_cloudsql_pgvector_ddl",
    "build_cloudsql_vector_search_sql",
]
