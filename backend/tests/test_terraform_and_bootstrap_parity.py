"""v1.6.0 Step 5 TDD: Terraform (infra/main.tf), bootstrap_gcp.sh, and deploy_cloudrun.sh schema & IAP parity."""

from __future__ import annotations

from pathlib import Path

from test_cloudrun_deploy import _run_deploy

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_terraform_main_tf_has_v3_tables_upload_bucket_iap_audience_and_replay_levels() -> None:
    tf = (PROJECT_ROOT / "infra" / "main.tf").read_text(encoding="utf-8")

    # 1. BigQuery v3 schema + provenance tables
    assert 'default     = "document_chunks_v3"' in tf
    for col in ("ingest_run_id", "layout_source", "embedding_multimodal", "bbox_source"):
        assert f'"{col}"' in tf, f"main.tf document_chunks missing column {col}"
    assert 'resource "google_bigquery_table" "ingest_runs"' in tf
    assert 'resource "google_bigquery_table" "query_runs"' in tf
    for qr_col in ("synthesis_model", "judge_model", "solicitation_id", "scope", "pipeline_id", "pipeline_spec"):
        assert f'"{qr_col}"' in tf, f"main.tf query_runs missing column {qr_col}"

    # 2. APIs, configurable ingress, upload bucket + CMEK + IAM
    assert '"vectorsearch.googleapis.com"' in tf
    assert 'variable "cloud_run_ingress"' in tf
    assert 'resource "google_storage_bucket" "upload_bucket"' in tf
    assert "roles/storage.legacyBucketReader" in tf
    assert "gcp-sa-discoveryengine.iam.gserviceaccount.com" in tf
    assert "upload_bucket_kms_key" in tf

    # 3. Live IAP_JWT_AUDIENCE + ACCESS_MODE + LIVE_INGEST + Replay 8-level parity
    assert "IAP_JWT_AUDIENCE" in tf
    assert "ACCESS_MODE" in tf
    assert "LIVE_INGEST" in tf
    assert "replay_enabled_levels" in tf
    assert "vertex_vector_v2" in tf


def test_bootstrap_gcp_sh_defaults_to_v3_and_creates_provenance_tables() -> None:
    sh = (PROJECT_ROOT / "scripts" / "bootstrap_gcp.sh").read_text(encoding="utf-8")
    assert 'BQ_TABLE="${BQ_TABLE:-document_chunks_v3}"' in sh
    assert '"vectorsearch.googleapis.com"' in sh
    for token in ("ingest_run_id", "layout_source", "bbox_source", "ingest_runs", "query_runs", "pipeline_spec"):
        assert token in sh, f"bootstrap_gcp.sh missing {token}"


def test_deploy_cloudrun_forwards_access_mode_live_ingest_and_iap_jwt_audience() -> None:
    proc_iap = _run_deploy(
        {
            "ALLOWED_UPLOAD_ORIGINS": "https://rag.acme.example",
            "MAX_UPLOAD_PDF_PAGES": "500",
        },
        "--dry-run",
    )
    assert proc_iap.returncode == 0, proc_iap.stderr
    live_lines = [line for line in proc_iap.stdout.splitlines() if line.startswith("ENV live ")]
    assert "ENV live ACCESS_MODE=iap" in live_lines
    assert "ENV live LIVE_INGEST=true" in live_lines
    assert "ENV live IAP_JWT_AUDIENCE=/projects/<project_number>/locations/us-central1/services/rag-arena" in live_lines
    assert "ENV live ALLOWED_UPLOAD_ORIGINS=https://rag.acme.example" in live_lines
    assert "ENV live MAX_UPLOAD_PDF_PAGES=500" in live_lines

    proc_pub = _run_deploy(
        {"ACCESS_MODE": "public", "IAP_MEMBERS": None, "DEPLOY_REPLAY": "false", "LIVE_INGEST": "false"},
        "--dry-run",
    )
    assert proc_pub.returncode == 0, proc_pub.stderr
    pub_lines = [line for line in proc_pub.stdout.splitlines() if line.startswith("ENV live ")]
    assert "ENV live ACCESS_MODE=public" in pub_lines
    assert "ENV live LIVE_INGEST=false" in pub_lines
    assert not any("IAP_JWT_AUDIENCE=" in ln for ln in pub_lines)
