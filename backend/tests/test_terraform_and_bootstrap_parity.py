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

    # 3. Live IAP_JWT_AUDIENCE + ACCESS_MODE + LIVE_INGEST + Replay 10-level parity + v1.6.4 restricted-org toggles
    assert "IAP_JWT_AUDIENCE" in tf
    assert "ACCESS_MODE" in tf
    assert "LIVE_INGEST" in tf
    assert "replay_enabled_levels" in tf
    assert "vertex_vector_v2" in tf
    assert "cloudsql_pgvector" in tf
    assert "bigquery_graphrag" in tf
    assert '"sqladmin.googleapis.com"' in tf
    for var_name in (
        "bq_graph_name",
        "create_service_accounts",
        "existing_live_sa_email",
        "existing_replay_sa_email",
        "vpc_network",
        "vpc_subnetwork",
        "vpc_egress",
        "replay_public_invoker",
        "corpus_bucket_kms_key",
        "bigquery_kms_key",
        "cloud_build_worker_pool",
    ):
        assert f'variable "{var_name}"' in tf, f"main.tf missing variable {var_name}"
    assert 'dynamic "vpc_access"' in tf
    assert 'dynamic "default_encryption_configuration"' in tf


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


def test_deploy_cloudrun_restricted_org_vpc_sc_and_preprovisioned_sa_dry_run() -> None:
    proc = _run_deploy(
        {
            "CLOUD_RUN_INGRESS": "internal-and-cloud-load-balancing",
            "VPC_NETWORK": "corp-shared-vpc",
            "VPC_SUBNETWORK": "rag-subnet-us-central1",
            "VPC_EGRESS": "all-traffic",
            "CLOUD_BUILD_WORKER_POOL": "projects/demo-project-1/locations/us-central1/workerPools/private-pool",
            "EXISTING_LIVE_SA_EMAIL": "preprov-live@demo-project-1.iam.gserviceaccount.com",
            "EXISTING_REPLAY_SA_EMAIL": "preprov-replay@demo-project-1.iam.gserviceaccount.com",
            "REPLAY_PUBLIC_INVOKER": "false",
            "BQ_GRAPH_NAME": "procurement_graph",
            "CLOUDSQL_DSN": "postgresql://u:secret@127.0.0.1:15433/postgres",
        },
        "--dry-run",
    )
    assert proc.returncode == 0, proc.stderr
    assert "PLAN cloud-build-worker-pool projects/demo-project-1/locations/us-central1/workerPools/private-pool" in proc.stdout
    assert "PLAN service-account preprov-live@demo-project-1.iam.gserviceaccount.com (pre-provisioned)" in proc.stdout
    assert "PLAN service-account preprov-replay@demo-project-1.iam.gserviceaccount.com (pre-provisioned, no roles)" in proc.stdout
    assert "PLAN cloud-run-ingress internal-and-cloud-load-balancing" in proc.stdout
    assert "PLAN vpc-egress network=corp-shared-vpc subnet=rag-subnet-us-central1 egress=all-traffic" in proc.stdout
    assert "(replay, authenticated, sa=preprov-replay@demo-project-1.iam.gserviceaccount.com" in proc.stdout
    assert "ENV live BQ_GRAPH_NAME=procurement_graph" in proc.stdout
    # CLOUDSQL_DSN is local-only and must never be forwarded to Cloud Run
    assert "CLOUDSQL_DSN" not in proc.stdout

    # Validation rejects half-configured VPC egress
    bad_vpc = _run_deploy({"VPC_NETWORK": "corp-shared-vpc"}, "--dry-run")
    assert bad_vpc.returncode != 0 and "VPC_SUBNETWORK" in bad_vpc.stderr

    # Validation rejects invalid ingress
    bad_ing = _run_deploy({"CLOUD_RUN_INGRESS": "open-wide"}, "--dry-run")
    assert bad_ing.returncode != 0 and "CLOUD_RUN_INGRESS" in bad_ing.stderr
