#!/usr/bin/env python3
"""Provision The RAG Arena BigQuery Property Graph (``procurement_graph``) at $0/mo standing idle.

Materializes five tiny derived node/edge tables (<50 KiB total) from ``document_chunks_v3``
(``tier_profile='multimodal'``) and executes ``CREATE OR REPLACE PROPERTY GRAPH`` so Level 9
(``bigquery_graphrag``) and Level 10 ADK ``check_amendments`` can run live ISO GQL queries.

Usage:
  PYTHONPATH=backend .venv/bin/python scripts/provision_bq_graph.py --dry-run
  PYTHONPATH=backend .venv/bin/python scripts/provision_bq_graph.py --apply
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.gcp_clients import get_bigquery_client  # noqa: E402
from app.ingestion.bq_graph import (  # noqa: E402
    GRAPH_TABLE_NAMES,
    build_graph_table_ddl,
    build_property_graph_ddl,
)
from app.recording.redaction import redact_text, redaction_tokens  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision BigQuery Property Graph for RAG Arena ($0/mo idle).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print redacted DDL plan without touching BigQuery.")
    mode.add_argument("--apply", action="store_true", help="Create/replace graph tables and property graph in BigQuery.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    if args.apply and not settings.is_live:
        settings = replace(settings, demo_mode="live")

    proj = settings.gcp_project_id or "GCP_PROJECT_ID"
    table_ddls = build_graph_table_ddl(proj, settings.bq_dataset, settings.bq_table)
    graph_ddl = build_property_graph_ddl(proj, settings.bq_dataset, settings.bq_graph_name)
    tokens = redaction_tokens(settings)

    if args.dry_run:
        summary = {
            "mode": "dry-run",
            "dataset": settings.bq_dataset,
            "source_table": settings.bq_table,
            "graph_name": settings.bq_graph_name,
            "tables": list(GRAPH_TABLE_NAMES),
            "statements": len(table_ddls) + 1,
            "standing_cost_usd_per_month": 0.0,
        }
        print(redact_text(json.dumps(summary, indent=2), tokens))
        return 0

    from google.cloud import bigquery

    bq = get_bigquery_client(settings)
    labels: dict[str, str] = {}
    raw_label = os.environ.get("BQ_JOB_LABEL", "").strip()
    if ":" in raw_label:
        k, v = raw_label.split(":", 1)
        if k.strip() and v.strip():
            labels[k.strip()] = v.strip()
    job_cfg = bigquery.QueryJobConfig(labels=labels) if labels else bigquery.QueryJobConfig()
    total_bytes = 0
    for idx, stmt in enumerate((*table_ddls, graph_ddl), start=1):
        job = bq.query(stmt, job_config=job_cfg)
        job.result(timeout=120)
        total_bytes += int(getattr(job, "total_bytes_processed", 0) or 0)
        print(f"[{idx}/{len(table_ddls) + 1}] OK ({int(getattr(job, 'total_bytes_processed', 0) or 0)} B processed)")

    counts: dict[str, int] = {}
    for tbl in GRAPH_TABLE_NAMES:
        count_sql = f"SELECT COUNT(*) AS n FROM `{settings.gcp_project_id}.{settings.bq_dataset}.{tbl}`"
        job = bq.query(count_sql, job_config=job_cfg)
        rows = list(job.result(timeout=60))
        counts[tbl] = int(rows[0].n) if rows else 0

    out = {
        "status": "ok",
        "dataset": settings.bq_dataset,
        "graph_name": settings.bq_graph_name,
        "total_bytes_processed": total_bytes,
        "counts": counts,
    }
    print(redact_text(json.dumps(out, indent=2), tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
