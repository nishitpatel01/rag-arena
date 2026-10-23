# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-10-23

First public release (the internal "v3" rebuild: every stage measured, nothing simulated).

### Added
- **Five RAG architectures on one procurement corpus** (45 PDFs) and one 8-question golden set:
  Level 1 naive vector RAG (BigQuery `VECTOR_SEARCH`), Level 2 hybrid RRF + Vertex AI ranking API, Level 3 layout-aware
  multimodal RAG (Document AI Layout Parser + Gemini vision), Level 4 agentic RAG (Google ADK `LlmAgent` with
  retrieval, amendment, pricing and citation-check tools), Level 5 managed Vertex AI Search (managed summary or a
  selectable model over extractive segments).
- **Live, measured telemetry**: engine-emitted stage events over SSE with the real SQL / API requests, durations,
  tokens and cost; live architecture diagrams; resizable panes; screen-share safe mode.
- **Evaluation**: per-run synthesis and judge model picker (judge ≠ synthesis enforced server-side), Battle Score
  (40% QA relevance, 35% groundedness, 15% context precision, 10% efficiency vs 60 s / $0.20).
- **Compare** page (selected levels run concurrently on one question) and **Leaderboard** (BigQuery `query_runs` or
  recordings, one table per model pair, provenance per row); results persist across page navigation.
- **Zero-credential replay** of 79 recorded real runs.
- **Configurable Cloud Run deployment** (region, IAP or public access, optional public replay-only copy,
  least-privilege service accounts) with teardown script and Terraform parity.
- Offline test suites: 278 backend tests, 156 frontend tests.

### Known limitations
- Level 4 with Gemini 3.8 Flash has no recording for `q_sla_credit_calculation` (6/6 live attempts timed out).
- Ingestion jobs longer than 15 minutes must run locally (Cloud Run request timeout).
- `docker compose` (replay) is updated for the v1 image but not yet exercised in CI.
