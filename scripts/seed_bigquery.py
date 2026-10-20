#!/usr/bin/env python3
"""Seed BigQuery document_chunks table with multi-tier procurement corpus chunks and embeddings."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import ArenaSettings, get_settings  # noqa: E402
from app.core.gcp_clients import get_bigquery_client, get_genai_client  # noqa: E402
from app.core.models import DocumentChunk  # noqa: E402
from app.ingestion.pipeline import (  # noqa: E402
    build_embedding_contents_for_chunk,
    chunk_to_bigquery_row,
    collect_corpus_chunks,
)

_BATCH_SIZE = 50
_MAX_EMBED_WORKERS = 4


def _embed_single_chunk(
    chunk: DocumentChunk,
    live_settings: ArenaSettings,
    active_genai: Any,
    corpus_dir: Path | None,
) -> dict[str, Any]:
    """Embed a single DocumentChunk and serialize it into a BigQuery row dictionary."""
    emb_model = (
        live_settings.multimodal_embedding_model
        if chunk.tier_profile == "multimodal"
        else live_settings.text_embedding_model
    )
    contents = build_embedding_contents_for_chunk(chunk, corpus_dir=corpus_dir)
    resp = active_genai.models.embed_content(model=emb_model, contents=contents)
    vec = list(resp.embeddings[0].values)
    return chunk_to_bigquery_row(chunk, embedding=vec)


def run_seed(
    dry_run: bool = True,
    corpus_dir: Path | None = None,
    genai_client: Any = None,
    bq_client: Any = None,
) -> dict[str, Any]:
    """Collect multi-tier corpus chunks and either return a dry-run summary or insert into BigQuery."""
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be a boolean")
    if corpus_dir is not None and not isinstance(corpus_dir, Path):
        raise TypeError("corpus_dir must be a Path or None")

    chunks = collect_corpus_chunks(corpus_dir=corpus_dir)
    tier_counts = Counter(c.tier_profile for c in chunks)
    rows_preview = [chunk_to_bigquery_row(c, embedding=[]) for c in chunks]

    if dry_run:
        return {
            "status": "dry_run",
            "total_chunks": len(rows_preview),
            "tier_counts": dict(tier_counts),
            "sample_chunk_id": rows_preview[0]["chunk_id"] if rows_preview else None,
        }

    base_settings = get_settings()
    live_settings = ArenaSettings(
        demo_mode="live",
        gcp_project_id=base_settings.gcp_project_id,
        gcp_location=base_settings.gcp_location,
        bq_dataset=base_settings.bq_dataset,
        bq_table=base_settings.bq_table,
        gemini_fast_model=base_settings.gemini_fast_model,
        gemini_pro_model=base_settings.gemini_pro_model,
        text_embedding_model=base_settings.text_embedding_model,
        multimodal_embedding_model=base_settings.multimodal_embedding_model,
    )
    active_genai = genai_client if genai_client is not None else get_genai_client(live_settings)
    active_bq = bq_client if bq_client is not None else get_bigquery_client(live_settings)
    table_fqn = f"{live_settings.gcp_project_id}.{live_settings.bq_dataset}.{live_settings.bq_table}"

    inserted = 0
    for start in range(0, len(chunks), _BATCH_SIZE):
        batch = chunks[start : start + _BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=_MAX_EMBED_WORKERS) as pool:
            batch_rows = list(
                pool.map(
                    lambda c: _embed_single_chunk(c, live_settings, active_genai, corpus_dir),
                    batch,
                )
            )

        errors = active_bq.insert_rows_json(table_fqn, batch_rows)
        if errors:
            raise RuntimeError(f"BigQuery insert_rows_json failed: {errors}")
        inserted += len(batch_rows)

    return {
        "status": "seeded",
        "table": table_fqn,
        "inserted_rows": inserted,
        "tier_counts": dict(tier_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed BigQuery with multi-tier procurement chunks")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and summarize corpus chunks without calling Vertex AI or BigQuery",
    )
    args = parser.parse_args()
    summary = run_seed(dry_run=bool(args.dry_run))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
