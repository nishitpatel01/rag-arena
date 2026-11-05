"""Level 7: AlloyDB for PostgreSQL + ScaNN Hybrid (``alloydb_scann``) — ScaNN Cosine + GIN FTS RRF + Ranking API.

Executes parameterized hybrid SQL inside a PostgreSQL transaction using ``psycopg``:
  1. ``SET LOCAL scann.num_leaves_to_search = 8`` (sub-millisecond two-level tree-AH ScaNN ANN probe),
  2. Dense cosine leg ``embedding <=> %(query_vector)s::vector`` + PostgreSQL ``ts_rank_cd`` GIN full-text leg,
  3. In-database Reciprocal Rank Fusion (``k = 60``), followed by Discovery Engine Ranking API cross-encoder
     reranking and catalog-selected LLM synthesis.

Works identically against Cloud AlloyDB (via loopback ``alloydb-auth-proxy``) and local AlloyDB Omni Docker
(``scripts/ephemeral_alloydb.sh --omni``). Replay is served by ``ReplayRAGEngine``; live/record fails loud when
``ALLOYDB_DSN`` is unset or the table is empty (spec P2).
"""

from __future__ import annotations

from dataclasses import replace
import json
import re
import threading
import time
from typing import Any, Callable

from psycopg import sql as pg_sql

from app.core import gcp_clients
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_alloydb_client
from app.core.models import BoundingBox, DocumentChunk, EngineFlags, EngineResult
from app.core.models_catalog import ModelSpec, spec_for_settings_model
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import RankingClient, ranking_url, redact_project
from app.core.telemetry import StageEmitter
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

LAYOUT_SOURCE = "alloydb_scann"
RETRIEVE_SERVICE = "AlloyDB for PostgreSQL · ScaNN ANN + GIN FTS (RRF)"
SCANN_DEFAULT_NUM_LEAVES = 24
SCANN_DEFAULT_LEAVES_TO_SEARCH = 8
RRF_K = 60
_PG_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_EMBEDDING_PREVIEW_DIMS = 4
_MAX_CONTEXT_CHUNK_CHARS = 2400
_CONN_EXEC_LOCK = threading.Lock()


def validate_pg_identifier(name: str, field: str = "table") -> str:
    """Strictly validate an unqualified PostgreSQL identifier before wrapping in ``psycopg.sql.Identifier``."""
    if not isinstance(name, str) or not _PG_IDENT_RE.fullmatch(name):
        raise ValueError(
            f"Unsafe PostgreSQL identifier for {field}: {name!r}; expected [a-zA-Z_][a-zA-Z0-9_]{{0,62}}"
        )
    return name


def format_pgvector_literal(values: list[float]) -> str:
    """Format validated floats as a compact pgvector literal ``[v1,v2,...]`` passed via ``%(query_vector)s``."""
    if not values:
        raise ValueError("query_vector must be non-empty")
    return "[" + ",".join(f"{float(v):.8g}" for v in values) + "]"


def build_alloydb_scann_ddl(
    *,
    table: str = "document_chunks",
    vector_dim: int = 3072,
    num_leaves: int = SCANN_DEFAULT_NUM_LEAVES,
) -> tuple[str, ...]:
    """Return DDL statements initializing ``vector`` + ``alloydb_scann``, chunks table, ScaNN cosine & GIN indexes."""
    ident = validate_pg_identifier(table, "table")
    if not isinstance(vector_dim, int) or not 1 <= vector_dim <= 8192:
        raise ValueError("vector_dim must be in [1, 8192]")
    if not isinstance(num_leaves, int) or not 1 <= num_leaves <= 10000:
        raise ValueError("num_leaves must be in [1, 10000]")
    scann_idx = validate_pg_identifier(f"idx_{ident[:40]}_scann", "scann_index")
    fts_idx = validate_pg_identifier(f"idx_{ident[:40]}_fts", "fts_index")
    sol_idx = validate_pg_identifier(f"idx_{ident[:40]}_tier_sol", "filter_index")
    return (
        "CREATE EXTENSION IF NOT EXISTS vector;",
        "CREATE EXTENSION IF NOT EXISTS alloydb_scann;",
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
    embedding vector({vector_dim}) NOT NULL
);""",
        f'CREATE INDEX IF NOT EXISTS "{sol_idx}" ON "{ident}" (tier_profile, solicitation_id);',
        f'CREATE INDEX IF NOT EXISTS "{fts_idx}" ON "{ident}" USING GIN (to_tsvector(\'english\', content));',
        f'CREATE INDEX IF NOT EXISTS "{scann_idx}" ON "{ident}" USING scann (embedding cosine) WITH (num_leaves = {num_leaves});',
    )


def build_alloydb_hybrid_search_sql(
    *,
    table: str,
    query_vector: list[float],
    query_text: str,
    candidate_k: int = 25,
    solicitation_filter: str | None = None,
    rrf_k: int = RRF_K,
) -> tuple[str, dict[str, Any]]:
    """Build parameterized PostgreSQL ScaNN cosine + GIN FTS RRF SQL and its ``%(param)s`` dictionary."""
    ident = validate_pg_identifier(table, "table")
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query_text must be a non-empty string")
    if not isinstance(candidate_k, int) or isinstance(candidate_k, bool) or not 1 <= candidate_k <= 100:
        raise ValueError("candidate_k must be an integer in [1, 100]")
    if not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValueError("rrf_k must be >= 1")

    has_sol = bool(solicitation_filter and solicitation_filter.strip())
    sol_clause = " AND solicitation_id = %(solicitation_id)s" if has_sol else ""
    quoted_tbl = f'"{ident}"'
    sql_text = f"""WITH vec_leg AS (
    SELECT
        chunk_id,
        (embedding <=> %(query_vector)s::vector) AS vec_distance,
        ROW_NUMBER() OVER (ORDER BY embedding <=> %(query_vector)s::vector ASC, chunk_id ASC) AS vec_rank
    FROM {quoted_tbl}
    WHERE tier_profile = 'hybrid'{sol_clause}
    ORDER BY embedding <=> %(query_vector)s::vector ASC
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
    WHERE tier_profile = 'hybrid'{sol_clause}
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
    c.page_number,
    c.section_id,
    c.content,
    c.element_type,
    c.amendment_number,
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
    return sql_text, params


def _rows_to_candidates(rows: list[dict[str, Any]], *, tier_profile: str = LAYOUT_SOURCE) -> list[HybridCandidate]:
    out: list[HybridCandidate] = []
    for idx, row in enumerate(rows, start=1):
        page_num = max(1, int(row.get("page_number") or 1))
        rrf_score = round(float(row.get("rrf_score") or 0.0), 6)
        raw_bbox = row.get("bbox_json")
        if isinstance(raw_bbox, str) and raw_bbox.strip():
            raw_bbox = json.loads(raw_bbox)
        bbox = (
            BoundingBox(
                page=int(raw_bbox.get("page", page_num)),
                x0=float(raw_bbox.get("x0", 0.0)),
                y0=float(raw_bbox.get("y0", 0.0)),
                x1=float(raw_bbox.get("x1", 1.0)),
                y1=float(raw_bbox.get("y1", 1.0)),
            )
            if isinstance(raw_bbox, dict)
            else BoundingBox(page=page_num, x0=0.0, y0=0.0, x1=1.0, y1=1.0)
        )
        chunk = DocumentChunk(
            chunk_id=str(row["chunk_id"]),
            tier_profile=tier_profile,
            solicitation_id=str(row.get("solicitation_id") or ""),
            doc_name=str(row.get("doc_name") or "unknown.pdf"),
            doc_type=str(row.get("doc_type") or "SOLICITATION"),
            element_type=str(row.get("element_type") or "narrative_text"),
            section_id=str(row["section_id"]) if row.get("section_id") else None,
            amendment_number=int(row.get("amendment_number") or 0),
            effective_date=str(row["effective_date"]) if row.get("effective_date") else None,
            amends_section=str(row["amends_section"]) if row.get("amends_section") else None,
            page_number=page_num,
            bbox=bbox,
            content=str(row["content"]),
            score=rrf_score,
            layout_source=LAYOUT_SOURCE,
            bbox_source="pdf_text_layer",
        )
        vec_rank = int(row["vec_rank"]) if row.get("vec_rank") is not None else None
        lex_rank = int(row["lex_rank"]) if row.get("lex_rank") is not None else None
        vec_dist = round(float(row["vec_distance"]), 6) if row.get("vec_distance") is not None else None
        lex_score = round(float(row["lex_score"]), 6) if row.get("lex_score") is not None else None
        out.append(
            HybridCandidate(
                chunk=chunk,
                rrf_rank=idx,
                rrf_score=rrf_score,
                vector_rank=vec_rank,
                lexical_rank=lex_rank,
                vector_distance=vec_dist,
                lexical_score=int(round(lex_score * 1000)) if lex_score is not None else None,
            )
        )
    return out


class AlloyDBScaNNEngine(BaseRAGEngine):
    """Level 7: Ephemeral Operational AlloyDB for PostgreSQL + ScaNN Hybrid + Ranking API."""

    engine_id: str = "alloydb_scann"
    engine_label: str = "Level 7: AlloyDB PostgreSQL + ScaNN Hybrid"
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
        self._conn_factory = conn_factory or get_alloydb_client

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

    def _execute_hybrid_sql(
        self,
        sql_text: str,
        params: dict[str, Any],
        *,
        filtered: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        tokens = redaction_tokens(self.settings)
        try:
            conn = self._conn_factory(self.settings)
            with _CONN_EXEC_LOCK:
                t0 = time.perf_counter()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            pg_sql.SQL("SET LOCAL scann.num_leaves_to_search = {};").format(
                                pg_sql.Literal(SCANN_DEFAULT_LEAVES_TO_SEARCH)
                            )
                        )
                        if filtered:
                            cur.execute(pg_sql.SQL("SET LOCAL scann.satisfy_limit = 'relaxed_order';"))
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
            raise RuntimeError(f"AlloyDB ScaNN query failed: {safe_msg}") from exc
        return [dict(zip(cols, r)) for r in raw_rows], db_ms

    async def _retrieve_candidates(
        self,
        vector: list[float],
        original_query: str,
        solicitation_filter: str | None,
        emitter: StageEmitter,
        *,
        candidate_k: int | None = None,
    ) -> list[HybridCandidate]:
        eff_k = int(candidate_k or self.settings.alloydb_candidate_k)
        sql_text, params = build_alloydb_hybrid_search_sql(
            table=self.settings.alloydb_table,
            query_vector=vector,
            query_text=original_query,
            candidate_k=eff_k,
            solicitation_filter=solicitation_filter,
        )
        shown_params = {
            "query_vector": {"dimensions": len(vector), "preview": vector[:_EMBEDDING_PREVIEW_DIMS]},
            "query_text": original_query,
            "candidate_k": eff_k,
            "rrf_k": RRF_K,
            "scann.num_leaves_to_search": SCANN_DEFAULT_LEAVES_TO_SEARCH,
        }
        if solicitation_filter:
            shown_params["solicitation_id"] = solicitation_filter
        async with self.stage(
            emitter,
            "retrieve",
            label="AlloyDB ScaNN cosine + GIN FTS (in-DB RRF)",
            service=RETRIEVE_SERVICE,
            request={
                "dsn": "postgresql://${ALLOYDB_DSN}",
                "table": self.settings.alloydb_table,
                "sql": sql_text,
                "parameters": shown_params,
            },
        ) as ctx:
            rows, db_ms = await self.blocking(
                self._execute_hybrid_sql,
                sql_text,
                params,
                filtered=bool(solicitation_filter),
            )
            candidates = _rows_to_candidates(rows, tier_profile=self.tier_profile)
            if not candidates:
                raise RuntimeError(
                    f"AlloyDB ScaNN table '{self.settings.alloydb_table}' returned 0 rows — "
                    "run scripts/ephemeral_alloydb.sh --up (or --omni) to load the corpus or use DEMO_MODE=replay"
                )
            ctx.payload = {
                "table": self.settings.alloydb_table,
                "index_type": "alloydb_scann (cosine, num_leaves=24, num_leaves_to_search=8) + GIN FTS",
                "fusion": f"RRF(k={RRF_K})",
                "candidate_k": eff_k,
                "rows_returned": len(candidates),
                "db_exec_ms": db_ms,
                "candidates": [c.summary() for c in candidates],
            }
        return candidates

    async def _rerank(self, query: str, candidates: list[HybridCandidate], emitter: StageEmitter, *, top_n: int | None = None) -> list[RerankedCandidate]:
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
    ) -> EngineResult:
        """Execute Level 7 live: query_prep -> embed_query -> AlloyDB ScaNN+FTS RRF -> Ranking API -> synthesize."""
        cleaned = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "AlloyDBScaNNEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and (solicitation_filter.startswith("upl_") or solicitation_filter.startswith("upload_")):
            raise ValueError(
                "Upload workspaces (upl_*) are not indexed in ephemeral AlloyDB ScaNN benchmark instances; "
                "select the procurement benchmark corpus or run Levels 1–6 on uploads"
            )
        if not self.settings.alloydb_dsn.strip():
            raise RuntimeError(
                "ALLOYDB_DSN is not configured — run scripts/ephemeral_alloydb.sh --up (or --omni) "
                "or use DEMO_MODE=replay"
            )

        start = time.perf_counter()
        genai_client = gcp_clients.get_genai_client(self.settings)
        prepared = await self._query_prep(cleaned, emitter)
        vector, embedding_tokens = await embed_query_stage(self, prepared.prepared, genai_client, emitter)
        candidates = await self._retrieve_candidates(vector, prepared.original, solicitation_filter, emitter)
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
                "context_source": "AlloyDB for PostgreSQL (ScaNN + GIN FTS RRF)",
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
    "AlloyDBScaNNEngine",
    "LAYOUT_SOURCE",
    "RETRIEVE_SERVICE",
    "RRF_K",
    "SCANN_DEFAULT_LEAVES_TO_SEARCH",
    "SCANN_DEFAULT_NUM_LEAVES",
    "build_alloydb_hybrid_search_sql",
    "build_alloydb_scann_ddl",
    "format_pgvector_literal",
    "validate_pg_identifier",
]
