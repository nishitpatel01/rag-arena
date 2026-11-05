"""Ephemeral ScaNN data loader, verifier, and golden benchmark recorder (Release 1.4.0).

Loads existing 3072-d ``gemini-embedding-2`` ``hybrid`` chunks directly from BigQuery (zero re-embedding cost) into:
  - Level 7 (``alloydb_scann``): PostgreSQL ``vector`` + ``alloydb_scann`` cosine ANN + GIN FTS (trains tree-AH
    centroids AFTER bulk upsert, followed by ``ANALYZE``), or
  - Level 8 (``vertex_vector_v2``): Vertex AI Vector Search 2.0 Collection + ``dataObjects:batchCreate`` (dense 3072-d
    + sparse token vectors) + ``denseScann`` index LRO,

and records measured golden benchmark runs across ``(synthesis_model, judge_model)`` pairs into
``data/recordings/<engine>/`` with strict ``assert_redacted`` verification on every written file.

Run: ``PYTHONPATH=backend .venv/bin/python scripts/record_scann_benchmarks.py <subcommand> [options]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Awaitable, Callable, Sequence
import uuid

from psycopg import sql as pg_sql

from app.core.config import _PROJECT_ROOT, ArenaSettings, _load_dotenv_file, get_settings
from app.core.gcp_clients import get_access_token, get_alloydb_client, get_bigquery_client
from app.core.models import EngineResult, EvalOutcome
from app.core.models_catalog import ModelCatalog, RunModels, load_catalog
from app.core.telemetry import QueueStageEmitter, utc_now_iso
from app.core.vvs2_client import (
    Vvs2Client,
    Vvs2HttpError,
    Vvs2Resources,
    collection_body,
    data_object_request,
    dense_scann_index_body,
)
from app.engines import build_engine
from app.engines.alloydb_scann import (
    SCANN_DEFAULT_NUM_LEAVES,
    build_alloydb_scann_ddl,
    format_pgvector_literal,
    validate_pg_identifier,
)
from app.api.query_runs import build_query_run_row
from app.engines.base import BaseRAGEngine
from app.engines.replay_engine import recording_store_for
from app.evaluation.evaluator import run_evaluation
from app.ingestion.live_backends import LiveQueryRunWriter
from app.recording.recorder import build_recording, record_run
from app.recording.redaction import assert_redacted, redact_text, redaction_tokens

VALID_SCANN_ENGINES: tuple[str, ...] = ("alloydb_scann", "vertex_vector_v2")
DEFAULT_SYNTHESIS_MODELS: tuple[str, ...] = ("gemini-4-argon", "gemini-3.8-flash")
DEFAULT_JUDGE_MODEL_ID: str = "claude-opus-5-5"
DEFAULT_BENCHMARK_PATH: Path = _PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
DEFAULT_VECTOR_DIM: int = 3072
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,29}$")
_BQ_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_EXPORT_ROWS: int = 10_000
_MAX_BENCHMARK_QUESTIONS: int = 50


def build_bq_hybrid_export_sql(
    *,
    project_id: str,
    dataset: str,
    table: str,
    max_rows: int = _MAX_EXPORT_ROWS,
) -> str:
    """Build identifier-validated BigQuery SQL exporting non-upload ``hybrid`` corpus chunks + 3072-d embeddings."""
    proj = project_id.strip() if isinstance(project_id, str) else ""
    ds = dataset.strip() if isinstance(dataset, str) else ""
    tbl = table.strip() if isinstance(table, str) else ""
    if not _PROJECT_RE.fullmatch(proj):
        raise ValueError(f"Invalid GCP project_id for BigQuery export: {project_id!r}")
    if not _BQ_IDENT_RE.fullmatch(ds):
        raise ValueError(f"Invalid BigQuery dataset identifier: {dataset!r}")
    if not _BQ_IDENT_RE.fullmatch(tbl):
        raise ValueError(f"Invalid BigQuery table identifier: {table!r}")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or not 1 <= max_rows <= 100_000:
        raise ValueError("max_rows must be an integer in [1, 100000]")

    fqn = f"`{proj}.{ds}.{tbl}`"
    return f"""SELECT
  chunk_id,
  tier_profile,
  solicitation_id,
  doc_name,
  doc_type,
  element_type,
  section_id,
  amendment_number,
  effective_date,
  amends_section,
  page_number,
  TO_JSON_STRING(bbox) AS bbox_json,
  content,
  embedding
FROM {fqn}
WHERE tier_profile = 'hybrid'
  AND NOT STARTS_WITH(solicitation_id, 'upl_')
ORDER BY solicitation_id ASC, doc_name ASC, page_number ASC, chunk_id ASC
LIMIT {max_rows}"""


def fetch_hybrid_corpus_rows(settings: ArenaSettings, *, bq_client: Any = None) -> list[dict[str, Any]]:
    """Fetch ``hybrid`` corpus chunks + existing 3072-d vectors from BigQuery without calling any embedding API."""
    client = bq_client or get_bigquery_client(settings)
    sql_text = build_bq_hybrid_export_sql(
        project_id=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        table=settings.bq_table,
    )
    job = client.query(sql_text)
    rows: list[dict[str, Any]] = []
    for raw in job.result(timeout=120.0):
        item = dict(raw.items())
        vec = [float(v) for v in (item.get("embedding") or [])]
        if len(vec) != DEFAULT_VECTOR_DIM:
            raise ValueError(
                f"Chunk {item.get('chunk_id')!r} has embedding dim {len(vec)}, expected {DEFAULT_VECTOR_DIM}"
            )
        item["embedding"] = vec
        rows.append(item)
    if not rows:
        raise RuntimeError(
            "BigQuery returned 0 'hybrid' corpus chunks — ingest Level 2 before loading ephemeral ScaNN stores"
        )
    return rows


def _validate_chunk_rows(rows: Sequence[dict[str, Any]], vector_dim: int) -> None:
    if not rows:
        raise ValueError("rows must be non-empty when loading an ephemeral ScaNN store")
    for row in rows:
        cid = str(row.get("chunk_id") or "").strip()
        vec = row.get("embedding")
        if not cid or not isinstance(vec, list) or len(vec) != vector_dim:
            raise ValueError(
                f"Invalid chunk row {cid!r}: expected list embedding of dimension {vector_dim}"
            )


def load_alloydb(
    conn: Any,
    rows: Sequence[dict[str, Any]],
    *,
    table: str = "document_chunks",
    vector_dim: int = DEFAULT_VECTOR_DIM,
    num_leaves: int = SCANN_DEFAULT_NUM_LEAVES,
    batch_size: int = 64,
) -> dict[str, Any]:
    """Create schema, bulk-upsert chunks, build ScaNN tree-AH index AFTER load, run ANALYZE, and verify."""
    ident = validate_pg_identifier(table, "table")
    _validate_chunk_rows(rows, vector_dim)
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be in [1, 500]")

    ddl = build_alloydb_scann_ddl(table=ident, vector_dim=vector_dim, num_leaves=num_leaves)
    scann_idx = validate_pg_identifier(f"idx_{ident[:40]}_scann", "scann_index")
    upsert_sql = f"""INSERT INTO "{ident}" (
    chunk_id, tier_profile, solicitation_id, doc_name, doc_type, element_type,
    section_id, amendment_number, effective_date, amends_section, page_number,
    bbox_json, content, embedding
) VALUES (
    %(chunk_id)s, %(tier_profile)s, %(solicitation_id)s, %(doc_name)s, %(doc_type)s, %(element_type)s,
    %(section_id)s, %(amendment_number)s, %(effective_date)s, %(amends_section)s, %(page_number)s,
    %(bbox_json)s::jsonb, %(content)s, %(embedding)s::vector
)
ON CONFLICT (chunk_id) DO UPDATE SET
    tier_profile = EXCLUDED.tier_profile,
    solicitation_id = EXCLUDED.solicitation_id,
    doc_name = EXCLUDED.doc_name,
    doc_type = EXCLUDED.doc_type,
    element_type = EXCLUDED.element_type,
    section_id = EXCLUDED.section_id,
    amendment_number = EXCLUDED.amendment_number,
    effective_date = EXCLUDED.effective_date,
    amends_section = EXCLUDED.amends_section,
    page_number = EXCLUDED.page_number,
    bbox_json = EXCLUDED.bbox_json,
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding;"""

    with conn.cursor() as cur:
        for stmt in ddl[:-1]:
            cur.execute(stmt)
    conn.commit()

    batches = 0
    total = len(rows)
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        params_seq = [
            {
                "chunk_id": str(r["chunk_id"]),
                "tier_profile": str(r.get("tier_profile") or "hybrid"),
                "solicitation_id": str(r.get("solicitation_id") or ""),
                "doc_name": str(r.get("doc_name") or ""),
                "doc_type": str(r.get("doc_type") or "SOLICITATION"),
                "element_type": str(r.get("element_type") or "narrative_text"),
                "section_id": r.get("section_id"),
                "amendment_number": int(r.get("amendment_number") or 0),
                "effective_date": r.get("effective_date"),
                "amends_section": r.get("amends_section"),
                "page_number": int(r.get("page_number") or 1),
                "bbox_json": (
                    json.dumps(r["bbox"])
                    if isinstance(r.get("bbox"), dict)
                    else (str(r["bbox_json"]) if r.get("bbox_json") else None)
                ),
                "content": str(r["content"]),
                "embedding": format_pgvector_literal(list(r["embedding"])),
            }
            for r in batch
        ]
        with conn.cursor() as cur:
            cur.executemany(upsert_sql, params_seq)
        conn.commit()
        batches += 1

    # Build/rebuild ScaNN index AFTER populating rows so two-level tree-AH centroids train on the corpus vectors.
    with conn.cursor() as cur:
        cur.execute(f'DROP INDEX IF EXISTS "{scann_idx}";')
        cur.execute(ddl[-1])
        cur.execute(f'ANALYZE "{ident}";')
    conn.commit()

    verified = verify_alloydb(conn, table=ident)
    return {
        "table": ident,
        "loaded": total,
        "batches": batches,
        "scann_index": verified["scann_index"],
        "verified_count": verified["hybrid_rows"],
    }


def verify_alloydb(conn: Any, *, table: str = "document_chunks") -> dict[str, Any]:
    """Verify non-zero ``hybrid`` row count and active ``scann`` index in PostgreSQL."""
    ident = validate_pg_identifier(table, "table")
    with conn.cursor() as cur:
        cur.execute(
            pg_sql.SQL("SELECT COUNT(*) FROM {} WHERE tier_profile = 'hybrid';").format(
                pg_sql.Identifier(ident)
            )
        )
        cnt_row = cur.fetchone()
        count = int(cnt_row[0]) if cnt_row else 0
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = %(table)s AND indexdef ILIKE '%%scann%%';",
            {"table": ident},
        )
        idx_row = cur.fetchone()
    conn.commit()
    if count <= 0:
        raise RuntimeError(f"AlloyDB table '{ident}' has 0 hybrid rows")
    if not idx_row:
        raise RuntimeError(f"AlloyDB table '{ident}' is missing its USING scann index")
    return {"table": ident, "hybrid_rows": count, "scann_index": str(idx_row[0])}


def _maybe_wait_vvs2_lro(client: Vvs2Client, resp: dict[str, Any], *, timeout_seconds: float) -> None:
    op_name = resp.get("name")
    if isinstance(op_name, str) and "/operations/" in op_name and resp.get("done") is not True:
        client.wait_operation(op_name, timeout_seconds=timeout_seconds)


def ensure_vvs2_collection(
    client: Vvs2Client,
    res: Vvs2Resources,
    *,
    vector_dim: int = DEFAULT_VECTOR_DIM,
    timeout_seconds: float = 600.0,
) -> str:
    """Idempotently ensure ``res.collection_name`` exists; return ``'exists'`` or ``'created'``."""
    try:
        client.request("GET", res.collection_name)
        return "exists"
    except Vvs2HttpError as exc:
        if exc.status_code != 404:
            raise
    body = collection_body(display_name=res.collection_id, vector_dim=vector_dim)
    try:
        op, _ = client.request(
            "POST",
            f"{res.parent}/collections",
            params={"collectionId": res.collection_id},
            json_body=body,
        )
    except Vvs2HttpError as exc:
        if exc.status_code == 409:
            return "exists"
        raise
    _maybe_wait_vvs2_lro(client, op, timeout_seconds=timeout_seconds)
    return "created"


def ensure_vvs2_scann_index(
    client: Vvs2Client,
    res: Vvs2Resources,
    *,
    timeout_seconds: float = 5400.0,
) -> str:
    """Idempotently ensure ``res.index_name`` DenseScann index exists after data load; wait on LRO."""
    try:
        client.request("GET", res.index_name)
        return "exists"
    except Vvs2HttpError as exc:
        if exc.status_code != 404:
            raise
    body = dense_scann_index_body(display_name=res.index_id)
    try:
        op, _ = client.request(
            "POST",
            f"{res.collection_name}/indexes",
            params={"indexId": res.index_id},
            json_body=body,
        )
    except Vvs2HttpError as exc:
        if exc.status_code == 409:
            return "exists"
        raise
    _maybe_wait_vvs2_lro(client, op, timeout_seconds=timeout_seconds)
    return "created"


def load_vvs2(
    client: Vvs2Client,
    res: Vvs2Resources,
    rows: Sequence[dict[str, Any]],
    *,
    vector_dim: int = DEFAULT_VECTOR_DIM,
    batch_size: int = 100,
    index_timeout_seconds: float = 5400.0,
) -> dict[str, Any]:
    """Create Collection, batch-upsert DataObjects (<=100/batch), build DenseScann index LRO, and verify."""
    _validate_chunk_rows(rows, vector_dim)
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 100:
        raise ValueError("VVS2 batch_size must be in [1, 100]")

    print(f"[vvs2] 1/4 Ensuring collection {res.collection_id} (dim={vector_dim})...", file=sys.stderr, flush=True)
    col_state = ensure_vvs2_collection(client, res, vector_dim=vector_dim)
    print(f"[vvs2] 2/4 Upserting {len(rows)} DataObjects (batch_size={batch_size})...", file=sys.stderr, flush=True)
    batches = 0
    total = len(rows)
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        reqs = [
            data_object_request(
                collection_name=res.collection_name,
                chunk_row=r,
                dense_embedding=list(r["embedding"]),
            )
            for r in batch
        ]
        try:
            client.request(
                "POST",
                f"{res.collection_name}/dataObjects:batchCreate",
                json_body={"requests": reqs},
                timeout_seconds=120.0,
            )
        except Vvs2HttpError as exc:
            if exc.status_code != 409:
                raise
            updates = [
                {
                    "dataObject": {
                        "name": f"{res.collection_name}/dataObjects/{req['dataObjectId']}",
                        **req["dataObject"],
                    },
                    "updateMask": "data,vectors",
                }
                for req in reqs
            ]
            client.request(
                "POST",
                f"{res.collection_name}/dataObjects:batchUpdate",
                json_body={"requests": updates},
                timeout_seconds=120.0,
            )
        batches += 1

    print(f"[vvs2] 3/4 Building DenseScann index {res.index_id} (LRO timeout={index_timeout_seconds}s)...", file=sys.stderr, flush=True)
    idx_state = ensure_vvs2_scann_index(client, res, timeout_seconds=index_timeout_seconds)
    print(f"[vvs2] 4/4 Verifying collection aggregate count...", file=sys.stderr, flush=True)
    verified = verify_vvs2(client, res)
    return {
        "collection": res.redacted_collection_name,
        "collection_state": col_state,
        "upserted": total,
        "batches": batches,
        "index": res.redacted_index_name,
        "index_state": idx_state,
        "verified_count": verified["count"],
    }


def _extract_aggregate_count(doc: dict[str, Any]) -> int:
    for candidate in (
        (doc.get("aggregateResult") or {}).get("count") if isinstance(doc.get("aggregateResult"), dict) else None,
        doc.get("count"),
    ):
        if candidate is not None:
            return int(candidate)
    if isinstance(doc.get("aggregateResults"), list) and doc["aggregateResults"]:
        first = doc["aggregateResults"][0]
        if isinstance(first, dict) and first.get("count") is not None:
            return int(first["count"])
    return 0


def verify_vvs2(client: Vvs2Client, res: Vvs2Resources) -> dict[str, Any]:
    """Verify non-zero DataObject count via ``dataObjects:aggregate`` and DenseScann index presence."""
    agg_doc, _ = client.request(
        "POST",
        f"{res.collection_name}/dataObjects:aggregate",
        json_body={"aggregate": "COUNT"},
    )
    count = _extract_aggregate_count(agg_doc)
    if count <= 0:
        raise RuntimeError(f"Vector Search 2.0 collection '{res.collection_id}' reported COUNT={count}")
    client.request("GET", res.index_name)
    return {
        "collection": res.redacted_collection_name,
        "index": res.redacted_index_name,
        "count": count,
    }


def load_benchmark_questions(
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    *,
    only_ids: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Read bounded golden benchmark questions from ``procurement_benchmark.json``."""
    raw = json.loads(benchmark_path.read_text(encoding="utf-8"))
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"No questions found in {benchmark_path}")
    wanted = {q.strip() for q in only_ids if q.strip()} if only_ids else None
    out: list[dict[str, str]] = []
    for item in questions[:_MAX_BENCHMARK_QUESTIONS]:
        qid = str(item.get("question_id") or "").strip()
        qtext = str(item.get("question") or "").strip()
        if not qid or not qtext:
            continue
        if wanted is not None and qid not in wanted:
            continue
        out.append({"question_id": qid, "question": qtext})
    if not out:
        raise ValueError(f"0 matching golden benchmark questions in {benchmark_path} for filter {wanted}")
    return out


EngineBuilderFn = Callable[[str, ArenaSettings, RunModels], BaseRAGEngine]
EvaluatorFn = Callable[..., Awaitable[EvalOutcome]]


async def record_engine_benchmarks(
    *,
    settings: ArenaSettings,
    engine_id: str,
    synthesis_ids: Sequence[str] = DEFAULT_SYNTHESIS_MODELS,
    judge_id: str = DEFAULT_JUDGE_MODEL_ID,
    question_ids: Sequence[str] | None = None,
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    write_query_runs: bool = False,
    catalog: ModelCatalog | None = None,
    engine_builder: EngineBuilderFn = build_engine,
    evaluator: EvaluatorFn = run_evaluation,
) -> list[Path]:
    """Execute golden benchmark questions on ``engine_id`` and persist leak-verified recordings."""
    if engine_id not in VALID_SCANN_ENGINES:
        raise ValueError(f"engine_id must be one of {VALID_SCANN_ENGINES}, got {engine_id!r}")
    if settings.demo_mode != "record":
        raise ValueError(f"record_engine_benchmarks requires DEMO_MODE=record, got {settings.demo_mode!r}")

    questions = load_benchmark_questions(benchmark_path, only_ids=question_ids)
    resolved_catalog = catalog or load_catalog(settings)
    store = recording_store_for(settings)
    tokens = redaction_tokens(settings)
    writer = LiveQueryRunWriter(settings) if write_query_runs else None
    timeout = float(settings.query_timeout_seconds)
    saved_paths: list[Path] = []

    for synth_id in synthesis_ids:
        run_models = resolved_catalog.select(synth_id, judge_id)
        engine = engine_builder(engine_id, settings, run_models)
        for q in questions:
            qid = q["question_id"]
            qtext = q["question"]
            run_id = f"run_{uuid.uuid4().hex[:12]}"
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            emitter = QueueStageEmitter(queue, run_id=run_id, engine=engine_id)
            started_at = utc_now_iso()

            result: EngineResult = await asyncio.wait_for(
                engine.run_query(qtext, question_id=qid, solicitation_filter=None, emitter=emitter),
                timeout=timeout,
            )
            outcome: EvalOutcome = await asyncio.wait_for(
                evaluator(
                    qtext,
                    result,
                    question_id=qid,
                    settings=settings,
                    emitter=emitter,
                    models=run_models,
                    solicitation_filter=None,
                ),
                timeout=timeout,
            )
            recording = build_recording(
                settings=settings,
                engine_id=engine_id,
                question_id=qid,
                query=qtext,
                run_id=run_id,
                events=emitter.events,
                result=result,
                evaluation=outcome,
                models=run_models,
            )
            path = record_run(store, recording)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            assert_redacted(persisted, tokens)
            saved_paths.append(path)

            if writer is not None:
                row = build_query_run_row(
                    settings=settings,
                    run_id=run_id,
                    question_id=qid,
                    query=qtext,
                    started_at=started_at,
                    emitter=emitter,
                    result=result,
                    outcome=outcome,
                    solicitation_filter=None,
                )
                writer.write_query_run(row)
            print(
                f"[{engine_id}] {synth_id}__{judge_id} / {qid}: "
                f"latency={result.latency_ms}ms G={outcome.metrics.groundedness:.2f} "
                f"QA={outcome.metrics.qa_relevance:.2f} CP={outcome.metrics.context_precision:.2f} -> {path.name}",
                file=sys.stderr,
            )
    return saved_paths


def _merged_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    base = {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}
    if overrides:
        base.update(overrides)
    return base


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load, verify, and record ephemeral ScaNN benchmarks (Levels 7–8).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_la = sub.add_parser("load-alloydb", help="Load BigQuery hybrid chunks into AlloyDB + build ScaNN index.")
    p_la.add_argument("--batch-size", type=int, default=64)
    p_la.add_argument("--num-leaves", type=int, default=SCANN_DEFAULT_NUM_LEAVES)
    p_la.add_argument("--dry-run", action="store_true")

    p_va = sub.add_parser("verify-alloydb", help="Verify AlloyDB hybrid row count and ScaNN index.")
    p_va.add_argument("--dry-run", action="store_true")

    p_lv = sub.add_parser("load-vvs2", help="Create VVS2 Collection, batchCreate DataObjects, and build DenseScann index.")
    p_lv.add_argument("--batch-size", type=int, default=100)
    p_lv.add_argument("--index-timeout-seconds", type=float, default=5400.0)
    p_lv.add_argument("--dry-run", action="store_true")

    p_vv = sub.add_parser("verify-vvs2", help="Verify VVS2 Collection COUNT and DenseScann index.")
    p_vv.add_argument("--dry-run", action="store_true")

    p_rec = sub.add_parser("record", help="Record golden benchmark runs into data/recordings/<engine>/.")
    p_rec.add_argument("--engine", choices=VALID_SCANN_ENGINES, required=True)
    p_rec.add_argument("--synthesis-models", default=",".join(DEFAULT_SYNTHESIS_MODELS))
    p_rec.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL_ID)
    p_rec.add_argument("--questions", default="")
    p_rec.add_argument("--write-query-runs", action="store_true")
    p_rec.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if getattr(args, "dry_run", False):
        print(f"DRY RUN ({args.cmd}): validated CLI arguments; zero GCP or database calls executed.")
        return 0

    raw_env = _merged_env()
    mode = "record" if args.cmd == "record" else "live"
    enabled = ",".join(dict.fromkeys([*VALID_SCANN_ENGINES, "level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex", "rag_engine"]))
    settings = get_settings({**raw_env, "DEMO_MODE": mode, "ENABLED_LEVELS": enabled})
    tokens = redaction_tokens(settings)

    try:
        if args.cmd == "load-alloydb":
            rows = fetch_hybrid_corpus_rows(settings)
            conn = get_alloydb_client(settings)
            out = load_alloydb(
                conn,
                rows,
                table=settings.alloydb_table,
                num_leaves=args.num_leaves,
                batch_size=args.batch_size,
            )
            print(json.dumps(out))
            return 0
        if args.cmd == "verify-alloydb":
            conn = get_alloydb_client(settings)
            out = verify_alloydb(conn, table=settings.alloydb_table)
            print(json.dumps(out))
            return 0
        if args.cmd == "load-vvs2":
            res = Vvs2Resources.from_settings(settings)
            rows = fetch_hybrid_corpus_rows(settings)
            client = Vvs2Client(settings, token_provider=lambda: get_access_token(settings))
            out = load_vvs2(
                client,
                res,
                rows,
                batch_size=args.batch_size,
                index_timeout_seconds=args.index_timeout_seconds,
            )
            print(json.dumps(out))
            return 0
        if args.cmd == "verify-vvs2":
            res = Vvs2Resources.from_settings(settings)
            client = Vvs2Client(settings, token_provider=lambda: get_access_token(settings))
            out = verify_vvs2(client, res)
            print(json.dumps(out))
            return 0
        if args.cmd == "record":
            synth_ids = tuple(m.strip() for m in args.synthesis_models.split(",") if m.strip())
            q_ids = tuple(q.strip() for q in args.questions.split(",") if q.strip()) or None
            saved = asyncio.run(
                record_engine_benchmarks(
                    settings=settings,
                    engine_id=args.engine,
                    synthesis_ids=synth_ids,
                    judge_id=args.judge_model.strip(),
                    question_ids=q_ids,
                    write_query_runs=bool(args.write_query_runs),
                )
            )
            print(json.dumps({"engine": args.engine, "recorded_files": len(saved)}))
            return 0
    except Exception as exc:
        safe_err = redact_text(str(exc), tokens)
        print(f"ERROR {type(exc).__name__}: {safe_err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
