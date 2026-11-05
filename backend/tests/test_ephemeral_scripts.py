"""Offline tests for Release 1.4.0 ephemeral ScaNN scripts & benchmark loader/recorder.

Covers:
  1. ``scripts/record_scann_benchmarks.py``:
     - BigQuery hybrid corpus export SQL builder & identifier validation.
     - ``load_alloydb`` / ``verify_alloydb``: DDL before load, parameterized batch upserts, post-load ScaNN
       centroid index build + ``ANALYZE``, non-zero count & ScaNN index verification.
     - ``load_vvs2`` / ``verify_vvs2``: idempotent Collection creation, bounded ``<= 100`` ``dataObjects:batchCreate``
       (with 409 ``batchUpdate`` fallback), post-load ``denseScann`` LRO index creation, and ``aggregate`` COUNT check.
     - ``record_engine_benchmarks``: golden benchmark recording across ``(synthesis, judge)`` pairs in
       ``DEMO_MODE=record`` with ``assert_redacted`` verification on every persisted JSON file.
     - CLI ``--dry-run`` exits 0 with zero network/DB calls.
  2. ``scripts/ephemeral_vvs2.sh`` & ``scripts/ephemeral_alloydb.sh``:
     - Fail-closed without ``--yes`` on mutating flags and reject non-``rag-arena-ephem-*`` names.
     - ``--dry-run`` (and ``--omni --dry-run``) exits 0 with sentinel ``gcloud``/``docker`` proving zero calls,
       and binds local proxy/container strictly to ``127.0.0.1`` (never ``0.0.0.0``).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import get_settings
from app.core.models import BoundingBox, Citation, EngineFlags, EngineResult, EvalMetrics, EvalOutcome, TokenUsage
from app.core.models_catalog import ModelCatalog, ModelSpec, ROLES
from app.core.vvs2_client import Vvs2Client, Vvs2Resources
from app.engines.base import BaseRAGEngine
from app.recording.redaction import assert_redacted, redaction_tokens

import scripts.record_scann_benchmarks as scann_cli


def _sample_rows(n: int = 3, dim: int = 3072) -> list[dict[str, Any]]:
    vec = [round(0.001 * ((i % 7) + 1), 6) for i in range(dim)]
    return [
        {
            "chunk_id": f"solicitation_01_04_Pricing.pdf_hybrid_{idx:04d}",
            "tier_profile": "hybrid",
            "solicitation_id": "solicitation_01",
            "doc_name": "04_Pricing_Schedule_CLINs.pdf",
            "doc_type": "PRICING",
            "element_type": "table_text",
            "section_id": "CLIN-0004-AB",
            "amendment_number": 0,
            "effective_date": "2026-01-15",
            "amends_section": None,
            "page_number": idx + 1,
            "bbox_json": '{"page":1,"x0":0.1,"y0":0.2,"x1":0.9,"y1":0.4}',
            "content": f"CLIN-0004-AB base rate is $210.00/hr (chunk {idx}).",
            "embedding": list(vec),
        }
        for idx in range(n)
    ]


class _FakeAlloyCursor:
    def __init__(self, conn: "_FakeAlloyConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeAlloyCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(self, query: Any, params: Any = None) -> None:
        q_str = query if isinstance(query, str) else repr(query)
        self._conn.executed.append((q_str, params))
        if "SELECT COUNT(*)" in q_str:
            self._rows = [(len(self._conn.upserted_ids),)]
        elif "FROM pg_indexes" in q_str:
            has_scann = any("USING scann" in s for s, _ in self._conn.executed)
            self._rows = [("idx_document_chunks_scann",)] if has_scann else []
        else:
            if isinstance(params, dict) and "chunk_id" in params:
                self._conn.upserted_ids.add(str(params["chunk_id"]))
            self._rows = []

    def executemany(self, query: str, seq_of_params: list[dict[str, Any]]) -> None:
        for p in seq_of_params:
            self.execute(query, p)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeAlloyConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.upserted_ids: set[str] = set()
        self.commits = 0

    def cursor(self) -> _FakeAlloyCursor:
        return _FakeAlloyCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def test_bq_export_sql_validates_identifiers_and_excludes_uploads() -> None:
    sql_text = scann_cli.build_bq_hybrid_export_sql(
        project_id="demo-proj-01",
        dataset="rag_arena",
        table="document_chunks",
    )
    assert "`demo-proj-01.rag_arena.document_chunks`" in sql_text
    assert "tier_profile = 'hybrid'" in sql_text
    assert "NOT STARTS_WITH(solicitation_id, 'upl_')" in sql_text

    with pytest.raises(ValueError):
        scann_cli.build_bq_hybrid_export_sql(
            project_id="bad`proj",
            dataset="rag_arena",
            table="document_chunks",
        )
    with pytest.raises(ValueError):
        scann_cli.build_bq_hybrid_export_sql(
            project_id="demo-proj-01",
            dataset="rag_arena;DROP",
            table="document_chunks",
        )


def test_load_and_verify_alloydb_trains_scann_after_upsert() -> None:
    conn = _FakeAlloyConn()
    rows = _sample_rows(5, dim=3072)
    summary = scann_cli.load_alloydb(
        conn,
        rows,
        table="document_chunks",
        vector_dim=3072,
        num_leaves=24,
        batch_size=2,
    )
    assert summary["loaded"] == 5
    assert summary["batches"] == 3
    assert summary["verified_count"] == 5

    sql_list = [q for q, _ in conn.executed]
    first_insert_idx = next(i for i, q in enumerate(sql_list) if "INSERT INTO" in q)
    scann_create_idx = next(i for i, q in enumerate(sql_list) if "USING scann" in q)
    analyze_idx = next(i for i, q in enumerate(sql_list) if q.startswith("ANALYZE"))
    assert first_insert_idx < scann_create_idx < analyze_idx

    verified = scann_cli.verify_alloydb(conn, table="document_chunks")
    assert verified["hybrid_rows"] == 5
    assert verified["scann_index"] == "idx_document_chunks_scann"

    with pytest.raises(ValueError):
        scann_cli.load_alloydb(conn, [], table="document_chunks", vector_dim=3072)


def test_load_and_verify_vvs2_handles_collection_batches_and_index_lro() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        parsed = json.loads(body.decode("utf-8")) if body else None
        calls.append((method, url, parsed))
        if method == "GET" and url.endswith("/collections/rag-arena-ephem-col"):
            return 404, b'{"error":{"code":404,"message":"not found"}}'
        if method == "POST" and "/collections?collectionId=rag-arena-ephem-col" in url:
            return 200, b'{"name":"projects/p/locations/us-central1/operations/op-col-1","done":false}'
        if method == "GET" and url.endswith("/operations/op-col-1"):
            return 200, b'{"name":"projects/p/locations/us-central1/operations/op-col-1","done":true,"response":{}}'
        if method == "POST" and url.endswith(":batchCreate"):
            reqs = (parsed or {}).get("requests") or []
            return 200, json.dumps({"dataObjects": [{"name": r["dataObjectId"]} for r in reqs]}).encode("utf-8")
        if method == "GET" and url.endswith("/indexes/dense-scann"):
            has_created = any(m == "POST" and "/indexes?indexId=dense-scann" in u for m, u, _ in calls)
            if not has_created:
                return 404, b'{"error":{"code":404,"message":"index missing"}}'
            return 200, b'{"name":"projects/p/locations/us-central1/collections/rag-arena-ephem-col/indexes/dense-scann"}'
        if method == "POST" and "/indexes?indexId=dense-scann" in url:
            return 200, b'{"name":"projects/p/locations/us-central1/operations/op-idx-1","done":false}'
        if method == "GET" and url.endswith("/operations/op-idx-1"):
            return 200, b'{"name":"projects/p/locations/us-central1/operations/op-idx-1","done":true,"response":{}}'
        if method == "POST" and url.endswith(":aggregate"):
            return 200, b'{"aggregateResult":{"count":"4"}}'
        raise AssertionError(f"Unexpected VVS2 transport call: {method} {url}")

    settings = get_settings(
        {
            "DEMO_MODE": "live",
            "GCP_PROJECT_ID": "secret-proj-alpha",
            "GCS_CORPUS_BUCKET": "secret-bkt-alpha",
            "VVS2_LOCATION": "us-central1",
            "VVS2_COLLECTION": "rag-arena-ephem-col",
            "VVS2_INDEX_ID": "dense-scann",
        }
    )
    res = Vvs2Resources.from_settings(settings)
    client = Vvs2Client(settings, token_provider=lambda: "tok", transport=fake_transport, sleep_fn=lambda _: None)

    summary = scann_cli.load_vvs2(client, res, _sample_rows(4, dim=3072), vector_dim=3072, batch_size=2)
    assert summary["collection_state"] == "created"
    assert summary["upserted"] == 4
    assert summary["batches"] == 2
    assert summary["index_state"] == "created"
    assert summary["verified_count"] == 4

    # Ensure ScaNN index was created AFTER dataObjects:batchCreate so centroids train on populated vectors
    batch_idx = next(i for i, (_, u, _) in enumerate(calls) if u.endswith(":batchCreate"))
    index_post_idx = next(i for i, (m, u, _) in enumerate(calls) if m == "POST" and "/indexes?indexId=" in u)
    assert batch_idx < index_post_idx


class _FakeScannEngine(BaseRAGEngine):
    engine_id = "alloydb_scann"
    engine_label = "Level 7: AlloyDB PostgreSQL + ScaNN Hybrid"
    tier_profile = "alloydb_scann"
    stages = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate")

    def __init__(self, settings: Any, *, synthesis_model: ModelSpec) -> None:
        super().__init__(settings=settings)
        self.synthesis_model = synthesis_model

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None = None,
        solicitation_filter: str | None = None,
        emitter: Any = None,
    ) -> EngineResult:
        proj_num = "8" * 6
        async with self.stage(
            emitter,
            "retrieve",
            label="Hybrid ScaNN ANN + GIN FTS (RRF)",
            service="AlloyDB",
            request={"dsn_leak_check": self.settings.alloydb_dsn, "project_ref": f"projects/{proj_num}/locations/us-central1"},
        ) as ctx:
            ctx.payload = {"rows_returned": 1, "db_ms": 4}
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer="CLIN-0004-AB is $210.00/hr [1].",
            citations=[
                Citation(
                    chunk_id="c1",
                    doc_name="04_Pricing_Schedule_CLINs.pdf",
                    solicitation_id="solicitation_01",
                    page_number=1,
                    section_id="CLIN-0004-AB",
                    element_type="table_text",
                    bbox=BoundingBox(page=1, x0=0.0, y0=0.0, x1=1.0, y1=1.0),
                    excerpt="CLIN-0004-AB: $210.00/hr",
                    layout_source="alloydb_scann",
                    bbox_source="pdf_text_layer",
                )
            ],
            retrieved_chunks=[],
            agent_steps=[],
            latency_ms=42,
            cost_usd=0.0031,
            stage_log=emitter.records if emitter is not None else (),
            usage=TokenUsage(
                prompt_tokens=300,
                output_tokens=60,
                embedding_tokens=18,
                model=self.synthesis_model.id,
                embedding_model="gemini-embedding-2",
            ),
            flags=EngineFlags(reranker_used=True, adk_runner_used=False, layout_source="alloydb_scann"),
        )


def test_record_engine_benchmarks_persists_redacted_recordings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rec_dir = Path(tmp) / "recordings"
        bench_path = Path(tmp) / "bench.json"
        bench_path.write_text(
            json.dumps(
                {
                    "questions": [
                        {
                            "question_id": "q_exact_clin_lookup",
                            "question": "What is CLIN-0004-AB?",
                            "ground_truth_answer": "$210.00/hr",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        dsn = "postgresql://postgres:" + "Sup3rS3cret!" + "@127.0.0.1:15432/postgres"
        settings = get_settings(
            {
                "DEMO_MODE": "record",
                "GCP_PROJECT_ID": "secret-proj-beta",
                "GCS_CORPUS_BUCKET": "secret-bkt-beta",
                "ENABLED_LEVELS": "alloydb_scann",
                "ALLOYDB_DSN": dsn,
                "RECORDINGS_DIR": str(rec_dir),
            }
        )
        catalog = ModelCatalog(
            models=(
                ModelSpec(
                    id="gemini-4-argon",
                    label="Gemini 4 Argon",
                    provider="vertex_gemini",
                    location="us-central1",
                    roles=ROLES,
                    input_usd_per_1m=1.25,
                    output_usd_per_1m=5.0,
                    price_source="https://cloud.google.com/vertex-ai/generative-ai/pricing",
                    price_as_of="2026-10-01",
                    available=True,
                    unavailable_reason=None,
                    verified_at="2026-10-20T00:00:00Z",
                ),
                ModelSpec(
                    id="claude-opus-5-5",
                    label="Claude Opus 5.5",
                    provider="anthropic_vertex",
                    location="global",
                    roles=ROLES,
                    input_usd_per_1m=5.0,
                    output_usd_per_1m=25.0,
                    price_source="https://cloud.google.com/vertex-ai/generative-ai/pricing",
                    price_as_of="2026-10-01",
                    available=True,
                    unavailable_reason=None,
                    verified_at="2026-10-20T00:00:00Z",
                ),
            )
        )

        async def fake_eval(
            query: str,
            result: EngineResult,
            *,
            question_id: str | None,
            settings: Any,
            emitter: Any,
            models: Any,
            solicitation_filter: str | None = None,
        ) -> EvalOutcome:
            return EvalOutcome(
                metrics=EvalMetrics(groundedness=1.0, qa_relevance=0.95, context_precision=0.9),
                judge_model=models.judge.id,
                judge_provider=models.judge.provider,
                judge_location=models.judge.location,
                reference_answer_used=True,
                duration_ms=120,
                input_tokens=400,
                output_tokens=80,
                judge_cost_usd=0.004,
                rationale="Exact match.",
            )

        saved = asyncio.run(
            scann_cli.record_engine_benchmarks(
                settings=settings,
                engine_id="alloydb_scann",
                synthesis_ids=("gemini-4-argon",),
                judge_id="claude-opus-5-5",
                benchmark_path=bench_path,
                catalog=catalog,
                engine_builder=lambda eid, cfg, run_models: _FakeScannEngine(cfg, synthesis_model=run_models.synthesis),
                evaluator=fake_eval,
            )
        )
        assert len(saved) == 1
        written = json.loads(saved[0].read_text(encoding="utf-8"))
        assert_redacted(written, redaction_tokens(settings))
        raw_text = saved[0].read_text(encoding="utf-8")
        assert "Sup3rS3cret!" not in raw_text
        assert "secret-proj-beta" not in raw_text
        assert ("8" * 6) not in raw_text
        assert "${ALLOYDB_DSN}" in raw_text


def test_python_cli_dry_run_makes_zero_external_calls() -> None:
    rc = scann_cli.main(["record", "--engine", "alloydb_scann", "--dry-run"])
    assert rc == 0
    rc2 = scann_cli.main(["load-vvs2", "--dry-run"])
    assert rc2 == 0
    rc3 = scann_cli.main(["load-alloydb", "--dry-run"])
    assert rc3 == 0


def _make_sentinel_bin(bin_dir: Path, name: str) -> None:
    p = bin_dir / name
    p.write_text(
        f"#!/usr/bin/env bash\necho 'SENTINEL {name} SHOULD NOT BE EXECUTED IN DRY RUN' >&2\nexit 99\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def test_ephemeral_shell_scripts_enforce_safety_guards_and_dry_run() -> None:
    vvs2_sh = _REPO_ROOT / "scripts" / "ephemeral_vvs2.sh"
    alloy_sh = _REPO_ROOT / "scripts" / "ephemeral_alloydb.sh"
    assert vvs2_sh.is_file() and os.access(vvs2_sh, os.X_OK)
    assert alloy_sh.is_file() and os.access(alloy_sh, os.X_OK)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_sentinel_bin(bin_dir, "gcloud")
        _make_sentinel_bin(bin_dir, "docker")
        _make_sentinel_bin(bin_dir, "alloydb-auth-proxy")

        env_file = tmp_path / "test.env"
        env_file.write_text(
            "GCP_PROJECT_ID=demo-scann-proj\n"
            "GCS_CORPUS_BUCKET=demo-scann-proj-corpus\n"
            "BQ_DATASET=rag_arena\n"
            "BQ_TABLE=document_chunks\n",
            encoding="utf-8",
        )
        run_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}

        # 1. VVS2 --dry-run succeeds without touching sentinel gcloud
        r_vvs2 = subprocess.run(
            [str(vvs2_sh), "--env-file", str(env_file), "--dry-run"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_vvs2.returncode == 0, r_vvs2.stderr
        assert "rag-arena-ephem-" in r_vvs2.stdout
        assert "DRY RUN" in r_vvs2.stdout

        # 2. VVS2 --down without --yes fails closed
        r_vvs2_no_yes = subprocess.run(
            [str(vvs2_sh), "--env-file", str(env_file), "--down"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_vvs2_no_yes.returncode != 0
        assert "--yes" in r_vvs2_no_yes.stderr

        # 3. VVS2 rejects unsafe collection prefix (must start with rag-arena-ephem-)
        bad_env = tmp_path / "bad_vvs2.env"
        bad_env.write_text(
            "GCP_PROJECT_ID=demo-scann-proj\n"
            "GCS_CORPUS_BUCKET=demo-scann-proj-corpus\n"
            "VVS2_COLLECTION=prod-neighbor-collection\n",
            encoding="utf-8",
        )
        r_vvs2_bad = subprocess.run(
            [str(vvs2_sh), "--env-file", str(bad_env), "--dry-run"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_vvs2_bad.returncode != 0
        assert "rag-arena-ephem-" in r_vvs2_bad.stderr

        # 4. AlloyDB Cloud --dry-run & --omni --dry-run succeed and bind strictly to 127.0.0.1
        r_alloy = subprocess.run(
            [str(alloy_sh), "--env-file", str(env_file), "--dry-run"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_alloy.returncode == 0, r_alloy.stderr
        assert "127.0.0.1" in r_alloy.stdout
        assert "0.0.0.0" not in r_alloy.stdout
        assert "rag-arena-ephem-" in r_alloy.stdout

        r_omni = subprocess.run(
            [str(alloy_sh), "--env-file", str(env_file), "--omni", "--dry-run"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_omni.returncode == 0, r_omni.stderr
        assert "127.0.0.1:15432:5432" in r_omni.stdout
        assert "0.0.0.0" not in r_omni.stdout

        # 5. AlloyDB --full without --yes fails closed
        r_alloy_no_yes = subprocess.run(
            [str(alloy_sh), "--env-file", str(env_file), "--full"],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )
        assert r_alloy_no_yes.returncode != 0
        assert "--yes" in r_alloy_no_yes.stderr
