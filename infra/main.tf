terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
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

provider "google" {
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
