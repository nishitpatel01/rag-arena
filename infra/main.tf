terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.30.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.30.0"
    }
  }
}

variable "project_id" {
  description = "Google Cloud Project ID for The RAG Arena"
  type        = string
}

variable "region" {
  description = "Google Cloud region for BigQuery and Vertex AI resources"
  type        = string
  default     = "us-central1"
}

variable "bq_dataset_id" {
  description = "BigQuery dataset ID for RAG Arena document chunks"
  type        = string
  default     = "rag_arena"
}

variable "docai_location" {
  description = "Document AI multi-region for the Level 3 Layout Parser processor (us or eu)"
  type        = string
  default     = "us"
}

variable "docai_layout_parser_display_name" {
  description = "Display name of the Level 3 Layout Parser processor (the app identifies its processor by this exact name)"
  type        = string
  default     = "rag-arena-layout-parser"
}

variable "vais_location" {
  description = "Discovery Engine (Vertex AI Search) location for the Level 5 data store and search app (global, us, eu)"
  type        = string
  default     = "global"
}

variable "vais_data_store_id" {
  description = "Level 5 data store id (VAIS_DATA_STORE_ID in .env)"
  type        = string
  default     = "rag-arena-procurement"
}

variable "vais_engine_id" {
  description = "Level 5 search app (engine) id (VAIS_ENGINE_ID in .env)"
  type        = string
  default     = "rag-arena-procurement-search"
}

variable "vais_uploads_data_store_id" {
  description = "v1.1 Level 5 uploads data store id (VAIS_UPLOADS_DATA_STORE_ID in .env; separate from the corpus store)"
  type        = string
  default     = "rag-arena-uploads"
}

variable "vais_uploads_engine_id" {
  description = "v1.1 Level 5 uploads search app (engine) id (VAIS_UPLOADS_ENGINE_ID in .env)"
  type        = string
  default     = "rag-arena-uploads-search"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "bigquery.googleapis.com",
    "discoveryengine.googleapis.com",
    "documentai.googleapis.com",
    "storage.googleapis.com",
    "alloydb.googleapis.com",
  ])
}

resource "google_project_service" "rag_arena_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "corpus_bucket" {
  name                        = "${var.project_id}-rag-arena-corpus"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_project_service.rag_arena_apis]
}

resource "google_bigquery_dataset" "rag_arena" {
  dataset_id                 = var.bq_dataset_id
  friendly_name              = "The RAG Arena Enterprise Procurement Dataset"
  description                = "Stores multi-tier document chunks, authority metadata, and Gemini embeddings"
  location                   = var.region
  delete_contents_on_destroy = false
  depends_on                 = [google_project_service.rag_arena_apis]
}

resource "google_bigquery_table" "document_chunks" {
  dataset_id          = google_bigquery_dataset.rag_arena.dataset_id
  table_id            = "document_chunks"
  deletion_protection = false
  clustering          = ["tier_profile", "solicitation_id"]

  schema = jsonencode([
    { name = "chunk_id", type = "STRING", mode = "REQUIRED" },
    { name = "tier_profile", type = "STRING", mode = "REQUIRED" },
    { name = "solicitation_id", type = "STRING", mode = "REQUIRED" },
    { name = "doc_name", type = "STRING", mode = "REQUIRED" },
    { name = "doc_type", type = "STRING", mode = "REQUIRED" },
    { name = "element_type", type = "STRING", mode = "REQUIRED" },
    { name = "section_id", type = "STRING", mode = "NULLABLE" },
    { name = "amendment_number", type = "INT64", mode = "NULLABLE" },
    { name = "effective_date", type = "DATE", mode = "NULLABLE" },
    { name = "amends_section", type = "STRING", mode = "NULLABLE" },
    { name = "page_number", type = "INT64", mode = "NULLABLE" },
    {
      name = "bbox"
      type = "RECORD"
      mode = "NULLABLE"
      fields = [
        { name = "page", type = "INT64", mode = "NULLABLE" },
        { name = "x0", type = "FLOAT64", mode = "NULLABLE" },
        { name = "y0", type = "FLOAT64", mode = "NULLABLE" },
        { name = "x1", type = "FLOAT64", mode = "NULLABLE" },
        { name = "y1", type = "FLOAT64", mode = "NULLABLE" }
      ]
    },
    { name = "content", type = "STRING", mode = "NULLABLE" },
    { name = "embedding", type = "FLOAT64", mode = "REPEATED" },
    { name = "embedding_multimodal", type = "FLOAT64", mode = "REPEATED" },
    { name = "bbox_source", type = "STRING", mode = "NULLABLE" }
  ])
}

# Level 3: Document AI Layout Parser processor (tables, headings, figures). Put its short id (last path segment of
# the resource name, never the full name) in the git-ignored .env as DOCAI_PROCESSOR_ID; the processor version used
# per request is DOCAI_PROCESSOR_VERSION (e.g. pretrained-layout-parser-v1.5-2025-08-25).
resource "google_document_ai_processor" "layout_parser" {
  location     = var.docai_location
  display_name = var.docai_layout_parser_display_name
  type         = "LAYOUT_PARSER_PROCESSOR"
  depends_on   = [google_project_service.rag_arena_apis]
}

output "docai_layout_parser_processor_id" {
  description = "Short processor id for DOCAI_PROCESSOR_ID"
  value       = element(split("/", google_document_ai_processor.layout_parser.id), length(split("/", google_document_ai_processor.layout_parser.id)) - 1)
}

# Level 5: Vertex AI Search data store (unstructured PDFs imported from gs://<bucket>/sample_docs/procurement_corpus/)
# with the default digital parser and NO chunking config — layout-based chunking makes the search API reject
# extractiveContentSpec, and Level 5 cites extractive segments with page numbers. Documents are imported by the
# managed_vertex ingestion job (documents:import, INCREMENTAL), not by Terraform.
resource "google_discovery_engine_data_store" "procurement" {
  location                    = var.vais_location
  data_store_id               = var.vais_data_store_id
  display_name                = var.vais_data_store_id
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false
  depends_on                  = [google_project_service.rag_arena_apis]
}

# Level 5: search app (engine) — Enterprise tier + LLM add-on (extractive segments + summaries).
resource "google_discovery_engine_search_engine" "procurement" {
  engine_id         = var.vais_engine_id
  collection_id     = "default_collection"
  location          = google_discovery_engine_data_store.procurement.location
  display_name      = var.vais_engine_id
  industry_vertical = "GENERIC"
  data_store_ids    = [google_discovery_engine_data_store.procurement.data_store_id]
  search_engine_config {
    search_tier    = "SEARCH_TIER_ENTERPRISE"
    search_add_ons = ["SEARCH_ADD_ON_LLM"]
  }
}

output "vais_data_store_id" {
  description = "Short data store id for VAIS_DATA_STORE_ID"
  value       = google_discovery_engine_data_store.procurement.data_store_id
}

output "vais_engine_id" {
  description = "Short engine id for VAIS_ENGINE_ID"
  value       = google_discovery_engine_search_engine.procurement.engine_id
}

# v1.1 Level 5 uploads data store + schema + search app (separate from the corpus store; filtered by workspace_id).
resource "google_discovery_engine_data_store" "uploads" {
  location                    = var.vais_location
  data_store_id               = var.vais_uploads_data_store_id
  display_name                = var.vais_uploads_data_store_id
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false
  depends_on                  = [google_project_service.rag_arena_apis]
}

resource "google_discovery_engine_schema" "uploads_default" {
  location      = google_discovery_engine_data_store.uploads.location
  data_store_id = google_discovery_engine_data_store.uploads.data_store_id
  schema_id     = "default_schema"
  json_schema = jsonencode({
    "$schema"               = "https://json-schema.org/draft/2020-12/schema"
    type                    = "object"
    datetime_detection      = true
    geolocation_detection   = true
    properties = {
      workspace_id = { type = "string", indexable = true, retrievable = true, dynamicFacetable = false }
      doc_name     = { type = "string", indexable = true, retrievable = true }
    }
  })
}

resource "google_discovery_engine_search_engine" "uploads" {
  engine_id         = var.vais_uploads_engine_id
  collection_id     = "default_collection"
  location          = google_discovery_engine_data_store.uploads.location
  display_name      = var.vais_uploads_engine_id
  industry_vertical = "GENERIC"
  data_store_ids    = [google_discovery_engine_data_store.uploads.data_store_id]
  search_engine_config {
    search_tier    = "SEARCH_TIER_ENTERPRISE"
    search_add_ons = ["SEARCH_ADD_ON_LLM"]
  }
}

output "vais_uploads_data_store_id" {
  description = "Short uploads data store id for VAIS_UPLOADS_DATA_STORE_ID"
  value       = google_discovery_engine_data_store.uploads.data_store_id
}

output "vais_uploads_engine_id" {
  description = "Short uploads engine id for VAIS_UPLOADS_ENGINE_ID"
  value       = google_discovery_engine_search_engine.uploads.engine_id
}

# =====================================================================================================================
# Phase 6: Cloud Run deployment (parity with scripts/deploy_cloudrun.sh — the script is the SUPPORTED, tested path;
# this block is provided for Terraform users and has not been applied by the project). Build and push the image first
# (`gcloud builds submit --tag <var.image>`), then `terraform apply -var image=... -var 'iap_members=["user:you@x.com"]'`.
# Direct Cloud Run IAP (`iap_enabled`) needs a recent provider (hashicorp/google >= 6.x); if your provider rejects
# `iap_enabled`, deploy with the script instead.
# =====================================================================================================================

variable "service_name" {
  description = "Live Cloud Run service name"
  type        = string
  default     = "rag-arena"
}

variable "image" {
  description = "Container image built from the root Dockerfile (<region>-docker.pkg.dev/<project>/<repo>/rag-arena:<tag>); empty = skip Cloud Run resources"
  type        = string
  default     = ""
}

variable "artifact_repo" {
  description = "Artifact Registry docker repository for the image"
  type        = string
  default     = "rag-arena"
}

variable "access_mode" {
  description = "iap (Identity-Aware Proxy allowlist, recommended) or public (anyone can start billed live runs)"
  type        = string
  default     = "iap"
  validation {
    condition     = contains(["iap", "public"], var.access_mode)
    error_message = "access_mode must be iap or public."
  }
}

variable "iap_members" {
  description = "Principals allowed through IAP (user:EMAIL, group:EMAIL, serviceAccount:EMAIL, domain:DOMAIN)"
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for m in var.iap_members : can(regex("^(user|group|serviceAccount|domain):", m))])
    error_message = "iap_members entries must start with user:, group:, serviceAccount: or domain:."
  }
}

variable "deploy_replay" {
  description = "Also deploy a public, role-less replay-only copy (DEMO_MODE=replay)"
  type        = bool
  default     = true
}

variable "live_ingest" {
  description = "Grant write roles so the live service can run ingestion jobs (documents:import + bucket object writes)"
  type        = bool
  default     = true
}

variable "live_env" {
  description = "App settings for the live service (same keys as .env; DEMO_MODE/GCP_PROJECT_ID are set automatically)"
  type        = map(string)
  default     = {}
}

locals {
  deploy_cloud_run = var.image != ""
  live_sa_roles = toset(concat(
    ["roles/aiplatform.user", "roles/bigquery.jobUser", "roles/documentai.apiUser"],
    [var.live_ingest ? "roles/discoveryengine.editor" : "roles/discoveryengine.viewer"],
  ))
  live_env = merge(var.live_env, {
    DEMO_MODE          = "live"
    GCP_PROJECT_ID     = var.project_id
    ALLOW_DEV_FIXTURES = "0"
  })
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "cloud_run_apis" {
  for_each           = local.deploy_cloud_run ? toset(["run.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "iap.googleapis.com"]) : toset([])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "rag_arena" {
  count         = local.deploy_cloud_run ? 1 : 0
  location      = var.region
  repository_id = var.artifact_repo
  format        = "DOCKER"
  description   = "The RAG Arena images"
  depends_on    = [google_project_service.cloud_run_apis]
}

resource "google_service_account" "live" {
  count        = local.deploy_cloud_run ? 1 : 0
  account_id   = "rag-arena-run"
  display_name = "RAG Arena live runtime"
}

resource "google_project_iam_member" "live" {
  for_each = local.deploy_cloud_run ? local.live_sa_roles : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.live[0].email}"
}

resource "google_bigquery_dataset_iam_member" "live_editor" {
  count      = local.deploy_cloud_run ? 1 : 0
  dataset_id = google_bigquery_dataset.rag_arena.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.live[0].email}"
}

resource "google_storage_bucket_iam_member" "live_corpus" {
  count  = local.deploy_cloud_run ? 1 : 0
  bucket = google_storage_bucket.corpus_bucket.name
  role   = var.live_ingest ? "roles/storage.objectUser" : "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.live[0].email}"
}

resource "google_cloud_run_v2_service" "live" {
  count               = local.deploy_cloud_run ? 1 : 0
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  iap_enabled         = var.access_mode == "iap"

  template {
    service_account                  = google_service_account.live[0].email
    timeout                          = "900s"
    max_instance_request_concurrency = 20
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "2Gi" }
      }
      dynamic "env" {
        for_each = local.live_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
  depends_on = [google_project_service.cloud_run_apis]
}

resource "google_project_service_identity" "iap_agent" {
  provider = google-beta
  count    = local.deploy_cloud_run && var.access_mode == "iap" ? 1 : 0
  project  = var.project_id
  service  = "iap.googleapis.com"
}

resource "google_cloud_run_v2_service_iam_member" "iap_agent_invoker" {
  count    = local.deploy_cloud_run && var.access_mode == "iap" ? 1 : 0
  name     = google_cloud_run_v2_service.live[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_member" "allowlist" {
  for_each               = local.deploy_cloud_run && var.access_mode == "iap" ? toset(var.iap_members) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.live[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

resource "google_cloud_run_v2_service_iam_member" "live_public" {
  count    = local.deploy_cloud_run && var.access_mode == "public" ? 1 : 0
  name     = google_cloud_run_v2_service.live[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_service_account" "replay" {
  count        = local.deploy_cloud_run && var.deploy_replay ? 1 : 0
  account_id   = "rag-arena-replay"
  display_name = "RAG Arena replay (no roles)"
}

resource "google_cloud_run_v2_service" "replay" {
  count               = local.deploy_cloud_run && var.deploy_replay ? 1 : 0
  name                = "${var.service_name}-replay"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.replay[0].email
    timeout                          = "300s"
    max_instance_request_concurrency = 40
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = var.image
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }
      env {
        name  = "DEMO_MODE"
        value = "replay"
      }
      env {
        name  = "ALLOW_DEV_FIXTURES"
        value = "0"
      }
    }
  }
  depends_on = [google_project_service.cloud_run_apis]
}

resource "google_cloud_run_v2_service_iam_member" "replay_public" {
  count    = local.deploy_cloud_run && var.deploy_replay ? 1 : 0
  name     = google_cloud_run_v2_service.replay[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "live_service_url" {
  description = "Live service URL (contains the project number; do not commit)"
  value       = local.deploy_cloud_run ? google_cloud_run_v2_service.live[0].uri : ""
}

output "replay_service_url" {
  description = "Public replay service URL"
  value       = local.deploy_cloud_run && var.deploy_replay ? google_cloud_run_v2_service.replay[0].uri : ""
}
