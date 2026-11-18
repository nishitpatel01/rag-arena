"""Pure deterministic FCIS Enterprise TCO & Workload-Fit Engine (Slice #1, v1.6.1).

Functional Core invariants (spec.md §2, P3, P5):
- Zero I/O, zero clocks, zero network calls, and acyclic ``app.core`` imports.
- All model/API rates read directly from ``app.core.pricing.PRICE_TABLE``; GCP infrastructure
  list prices (us-central1 pay-as-you-go) carry source URLs and ``price_as_of`` dates.
- Never fabricates or hardcodes benchmark evaluation metrics (QA / Groundedness / Context Precision):
  ``recorded_cohort`` and ``alternate_cohort`` are ``None`` unless caller passes measured cohorts.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from app.core.config import ALL_LEVEL_IDS
from app.core.models import _SerializableMixin
from app.core.pricing import PRICE_TABLE

DATA_GRAVITY_OPTIONS: tuple[str, ...] = (
    "bigquery_warehouse",
    "postgres_oltp",
    "unstructured_pdfs",
    "managed_turnkey",
)
LATENCY_SLA_OPTIONS: tuple[str, ...] = (
    "sub_second_retrieval",
    "interactive_rag",
    "deep_agentic_reasoning",
)
COMPLIANCE_POSTURE_OPTIONS: tuple[str, ...] = (
    "standard_serverless",
    "managed_turnkey",
    "cmek_vpc",
)
DOC_COMPLEXITY_OPTIONS: tuple[str, ...] = (
    "plain_text",
    "tables_diagrams",
    "multi_hop_math",
)
TCO_SYNTHESIS_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-4-argon",
)

MONTHLY_QUERIES_BOUNDS: tuple[int, int] = (1_000, 10_000_000)
CORPUS_PAGES_BOUNDS: tuple[int, int] = (100, 500_000)

_ALLOWED_WORKLOAD_KEYS: frozenset[str] = frozenset(
    {
        "data_gravity",
        "monthly_queries",
        "corpus_pages",
        "latency_sla",
        "compliance_posture",
        "doc_complexity",
        "synthesis_model",
    }
)

# Published GCP infrastructure list prices (us-central1 pay-as-you-go, read 2026-11-01).
HOURS_PER_MONTH: int = 730
BYTES_PER_GIB: float = float(1024**3)
BYTES_PER_TIB: float = float(1024**4)

BQ_SCAN_USD_PER_TIB: float = 6.25
BQ_MIN_SCAN_BYTES: int = 10 * 1024 * 1024
BQ_UNCACHED_QUERY_RATIO: float = 0.80
BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH: float = 0.020
GCS_STANDARD_USD_PER_GIB_MONTH: float = 0.020
PDF_BYTES_PER_PAGE: int = 120 * 1024

DOCAI_LAYOUT_USD_PER_1K_PAGES: float = 10.00
VISION_PROMPT_TOKENS_PER_DIAGRAM_PAGE: int = 1_800
VISION_OUTPUT_TOKENS_PER_DIAGRAM_PAGE: int = 900
VISION_MODEL_ID: str = "gemini-4-argon"

VAIS_FREE_STORAGE_GIB: float = 10.0
VAIS_STORAGE_USD_PER_GIB_MONTH: float = 2.50

RAG_ENGINE_STORAGE_USD_PER_GIB_MONTH: float = 0.023
RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD: float = 65.70

ALLOYDB_VCPU_USD_PER_HOUR: float = 0.06636
ALLOYDB_RAM_GIB_USD_PER_HOUR: float = 0.01126
ALLOYDB_BASE_VCPUS: int = 2
ALLOYDB_BASE_RAM_GIB: int = 16
ALLOYDB_BASE_MONTHLY_USD: float = round(
    (ALLOYDB_BASE_VCPUS * ALLOYDB_VCPU_USD_PER_HOUR + ALLOYDB_BASE_RAM_GIB * ALLOYDB_RAM_GIB_USD_PER_HOUR)
    * HOURS_PER_MONTH,
    4,
)
ALLOYDB_QUERIES_PER_UNIT_MONTH: int = 2_500_000
ALLOYDB_STORAGE_USD_PER_GIB_MONTH: float = 0.30
ALLOYDB_MIN_STORAGE_GIB: float = 10.0

VVS2_REPLICA_USD_PER_HOUR: float = 0.2688
VVS2_BASE_REPLICA_MONTHLY_USD: float = round(VVS2_REPLICA_USD_PER_HOUR * HOURS_PER_MONTH, 4)
VVS2_QUERIES_PER_REPLICA_MONTH: int = 3_500_000
VVS2_STORAGE_USD_PER_GIB_MONTH: float = 0.040
VVS2_MIN_STORAGE_GIB: float = 1.0


class TcoValidationError(ValueError):
    """Raised when workload parameters fail strict type, enum, or numeric bound checks."""


def _round2(value: float) -> float:
    """Deterministic half-up cent rounding matching JavaScript Math.floor(x * 100 + 0.5) / 100."""
    return math.floor(value * 100.0 + 0.5 + 1e-12) / 100.0


def _require_strict_int(name: str, value: Any, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TcoValidationError(
            f"Field '{name}' must be an integer in [{bounds[0]}, {bounds[1]}], got {type(value).__name__}"
        )
    low, high = bounds
    if value < low or value > high:
        raise TcoValidationError(f"Field '{name}' must be in [{low}, {high}], got {value}")
    return value


def _require_enum(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise TcoValidationError(f"Field '{name}' must be one of {list(allowed)}, got {value!r}")
    return value


@dataclass(frozen=True)
class TcoWorkload(_SerializableMixin):
    """Validated enterprise workload profile driving both TCO math and architecture fit."""

    data_gravity: str = "unstructured_pdfs"
    monthly_queries: int = 25_000
    corpus_pages: int = 5_000
    latency_sla: str = "interactive_rag"
    compliance_posture: str = "standard_serverless"
    doc_complexity: str = "tables_diagrams"
    synthesis_model: str = "gemini-3.8-flash"

    def __post_init__(self) -> None:
        _require_enum("data_gravity", self.data_gravity, DATA_GRAVITY_OPTIONS)
        _require_strict_int("monthly_queries", self.monthly_queries, MONTHLY_QUERIES_BOUNDS)
        _require_strict_int("corpus_pages", self.corpus_pages, CORPUS_PAGES_BOUNDS)
        _require_enum("latency_sla", self.latency_sla, LATENCY_SLA_OPTIONS)
        _require_enum("compliance_posture", self.compliance_posture, COMPLIANCE_POSTURE_OPTIONS)
        _require_enum("doc_complexity", self.doc_complexity, DOC_COMPLEXITY_OPTIONS)
        _require_enum("synthesis_model", self.synthesis_model, TCO_SYNTHESIS_MODELS)


DEFAULT_WORKLOAD = TcoWorkload()


def parse_tco_workload(raw: Mapping[str, Any] | None) -> TcoWorkload:
    """Parse an untrusted JSON mapping at module ingress into a validated ``TcoWorkload``."""
    if raw is None:
        return DEFAULT_WORKLOAD
    if not isinstance(raw, Mapping):
        raise TcoValidationError("Request body must be a JSON object")
    unknown = sorted(set(raw.keys()) - _ALLOWED_WORKLOAD_KEYS)
    if unknown:
        raise TcoValidationError(f"Unknown workload field(s): {unknown}")
    return TcoWorkload(
        data_gravity=raw.get("data_gravity", DEFAULT_WORKLOAD.data_gravity),
        monthly_queries=raw.get("monthly_queries", DEFAULT_WORKLOAD.monthly_queries),
        corpus_pages=raw.get("corpus_pages", DEFAULT_WORKLOAD.corpus_pages),
        latency_sla=raw.get("latency_sla", DEFAULT_WORKLOAD.latency_sla),
        compliance_posture=raw.get("compliance_posture", DEFAULT_WORKLOAD.compliance_posture),
        doc_complexity=raw.get("doc_complexity", DEFAULT_WORKLOAD.doc_complexity),
        synthesis_model=raw.get("synthesis_model", DEFAULT_WORKLOAD.synthesis_model),
    )


@dataclass(frozen=True)
class ScenarioPreset(_SerializableMixin):
    """Curated enterprise scenario for 1-click executive / CE architecture walkthroughs."""

    preset_id: str
    title: str
    subtitle: str
    workload: TcoWorkload


ENTERPRISE_SCENARIO_PRESETS: tuple[ScenarioPreset, ...] = (
    ScenarioPreset(
        preset_id="procurement_multimodal",
        title="RFP Tables, Exhibits & Diagrams",
        subtitle="Unstructured PDFs with pricing schedules, SOC matrices, and architecture diagrams",
        workload=TcoWorkload(
            data_gravity="unstructured_pdfs",
            monthly_queries=25_000,
            corpus_pages=5_000,
            latency_sla="interactive_rag",
            compliance_posture="standard_serverless",
            doc_complexity="tables_diagrams",
            synthesis_model="gemini-3.8-flash",
        ),
    ),
    ScenarioPreset(
        preset_id="contract_audit_agentic",
        title="Multi-Hop Contract & SLA Audit",
        subtitle="Superseding amendments, cross-document rate cards, and verified financial math",
        workload=TcoWorkload(
            data_gravity="unstructured_pdfs",
            monthly_queries=15_000,
            corpus_pages=10_000,
            latency_sla="deep_agentic_reasoning",
            compliance_posture="cmek_vpc",
            doc_complexity="multi_hop_math",
            synthesis_model="gemini-4-argon",
        ),
    ),
    ScenarioPreset(
        preset_id="bq_warehouse_rag",
        title="BigQuery Lakehouse RAG",
        subtitle="SQL-governed enterprise documents already curated in BigQuery at $0/mo idle",
        workload=TcoWorkload(
            data_gravity="bigquery_warehouse",
            monthly_queries=50_000,
            corpus_pages=20_000,
            latency_sla="interactive_rag",
            compliance_posture="standard_serverless",
            doc_complexity="plain_text",
            synthesis_model="gemini-3.8-flash",
        ),
    ),
    ScenarioPreset(
        preset_id="turnkey_managed_paas",
        title="Zero-Ops Managed Enterprise PaaS",
        subtitle="Turnkey GCS corpus import without maintaining SQL chunk tables or vector indexes",
        workload=TcoWorkload(
            data_gravity="managed_turnkey",
            monthly_queries=30_000,
            corpus_pages=10_000,
            latency_sla="interactive_rag",
            compliance_posture="managed_turnkey",
            doc_complexity="plain_text",
            synthesis_model="gemini-3.8-flash",
        ),
    ),
    ScenarioPreset(
        preset_id="high_qps_operational",
        title="High-QPS Operational ScaNN (1M/mo)",
        subtitle="Sub-second retrieval for customer-facing transactional apps at 1M queries/month",
        workload=TcoWorkload(
            data_gravity="postgres_oltp",
            monthly_queries=1_000_000,
            corpus_pages=100_000,
            latency_sla="sub_second_retrieval",
            compliance_posture="cmek_vpc",
            doc_complexity="plain_text",
            synthesis_model="gemini-3.8-flash",
        ),
    ),
)


@dataclass(frozen=True)
class RecordedCohortSnapshot(_SerializableMixin):
    """Measured benchmark metrics joined dynamically from real recordings (never hand-typed)."""

    synthesis_model: str
    judge_model: str
    runs: int
    questions_covered: int
    avg_qa_relevance: float | None
    avg_groundedness: float | None
    avg_context_precision: float | None
    avg_battle_score: float | None
    avg_latency_ms: float | None
    avg_cost_usd: float | None


@dataclass(frozen=True)
class CrossoverAnalysis(_SerializableMixin):
    """Monthly query break-even thresholds between $0/mo serverless and 24x7 dedicated ScaNN."""

    vvs2_vs_vais_queries: int
    alloydb_vs_vais_queries: int
    vvs2_vs_bq_multimodal_queries: int
    regime: str
    summary: str


@dataclass(frozen=True)
class ArchitectureTcoRow(_SerializableMixin):
    """Complete monthly TCO breakdown, workload-fit score, and joined cohort metrics for one level."""

    level_id: str
    order: int
    label: str
    short_label: str
    gcp_services: tuple[str, ...]
    idle_mode: str
    capacity_units: int
    standing_monthly_usd: float
    standing_note: str
    storage_monthly_usd: float
    embedding_monthly_usd: float
    retrieval_scan_monthly_usd: float
    rerank_monthly_usd: float
    synthesis_monthly_usd: float
    query_variable_monthly_usd: float
    total_monthly_usd: float
    cost_per_1k_queries_usd: float
    one_time_ingestion_usd: float
    ingestion_note: str
    fit_score: int
    fit_tier: str
    rationale: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    recorded_cohort: RecordedCohortSnapshot | None = None
    alternate_cohort: RecordedCohortSnapshot | None = None


@dataclass(frozen=True)
class EnterpriseTcoEstimate(_SerializableMixin):
    """Deterministic enterprise TCO + architecture recommendation payload."""

    workload: TcoWorkload
    recommended_level_id: str
    runner_up_level_id: str
    crossover: CrossoverAnalysis
    architectures: tuple[ArchitectureTcoRow, ...]


@dataclass(frozen=True)
class _LevelStaticMeta:
    level_id: str
    order: int
    default_label: str
    short_label: str
    gcp_services: tuple[str, ...]
    idle_mode: str
    embedding_model: str
    query_embed_tokens: int
    rank_counts_per_query: float
    prompt_tokens_per_query: int
    output_tokens_per_query: int
    ingest_embed_tokens_per_page: int


_LEVEL_META: tuple[_LevelStaticMeta, ...] = (
    _LevelStaticMeta(
        level_id="managed_vertex",
        order=1,
        default_label="Managed Vertex AI Search",
        short_label="L1 · Vertex AI Search",
        gcp_services=("Vertex AI Search (Enterprise)", "Cloud Storage Import", "Vertex AI Gemini"),
        idle_mode="serverless_zero",
        embedding_model="",
        query_embed_tokens=0,
        rank_counts_per_query=0.0,
        prompt_tokens_per_query=3_600,
        output_tokens_per_query=1_500,
        ingest_embed_tokens_per_page=0,
    ),
    _LevelStaticMeta(
        level_id="rag_engine",
        order=2,
        default_label="Managed Vertex AI RAG Engine",
        short_label="L2 · Vertex RAG Engine",
        gcp_services=("Vertex AI RAG Engine", "RagManagedDb Serverless", "text-embedding-005", "Vertex AI Gemini"),
        idle_mode="serverless_zero",
        embedding_model="text-embedding-005",
        query_embed_tokens=45,
        rank_counts_per_query=0.0,
        prompt_tokens_per_query=6_200,
        output_tokens_per_query=1_650,
        ingest_embed_tokens_per_page=500,
    ),
    _LevelStaticMeta(
        level_id="level1_naive",
        order=3,
        default_label="Naive Vector RAG (BigQuery)",
        short_label="L3 · BigQuery Vector",
        gcp_services=("BigQuery VECTOR_SEARCH", "Vertex AI gemini-embedding-2", "Vertex AI Gemini"),
        idle_mode="serverless_zero",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=45,
        rank_counts_per_query=0.0,
        prompt_tokens_per_query=1_800,
        output_tokens_per_query=1_500,
        ingest_embed_tokens_per_page=540,
    ),
    _LevelStaticMeta(
        level_id="level2_hybrid",
        order=4,
        default_label="BigQuery Hybrid + Reranker",
        short_label="L4 · BQ Hybrid + Rerank",
        gcp_services=("BigQuery VECTOR_SEARCH + SEARCH()", "Discovery Engine Ranking API", "gemini-embedding-2"),
        idle_mode="serverless_zero",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=45,
        rank_counts_per_query=1.0,
        prompt_tokens_per_query=3_600,
        output_tokens_per_query=1_500,
        ingest_embed_tokens_per_page=450,
    ),
    _LevelStaticMeta(
        level_id="alloydb_scann",
        order=5,
        default_label="AlloyDB PostgreSQL + ScaNN",
        short_label="L5 · AlloyDB + ScaNN",
        gcp_services=("AlloyDB for PostgreSQL (ScaNN + GIN)", "Discovery Engine Ranking API", "gemini-embedding-2"),
        idle_mode="dedicated_cluster",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=45,
        rank_counts_per_query=1.0,
        prompt_tokens_per_query=3_600,
        output_tokens_per_query=1_500,
        ingest_embed_tokens_per_page=450,
    ),
    _LevelStaticMeta(
        level_id="vertex_vector_v2",
        order=6,
        default_label="Vertex AI Vector Search 2.0",
        short_label="L6 · Vector Search 2.0",
        gcp_services=("Vector Search 2.0 (DenseScann + RRF)", "Discovery Engine Ranking API", "gemini-embedding-2"),
        idle_mode="dedicated_replica",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=45,
        rank_counts_per_query=1.0,
        prompt_tokens_per_query=3_600,
        output_tokens_per_query=1_500,
        ingest_embed_tokens_per_page=450,
    ),
    _LevelStaticMeta(
        level_id="level3_multimodal",
        order=7,
        default_label="Layout-Aware Multimodal RAG",
        short_label="L7 · DocAI + Vision + BQ",
        gcp_services=("Document AI Layout Parser", "Gemini Vision", "BigQuery Hybrid RRF", "Ranking API"),
        idle_mode="serverless_zero",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=45,
        rank_counts_per_query=1.0,
        prompt_tokens_per_query=4_000,
        output_tokens_per_query=1_550,
        ingest_embed_tokens_per_page=520,
    ),
    _LevelStaticMeta(
        level_id="level4_agentic",
        order=8,
        default_label="Agentic RAG (Google ADK)",
        short_label="L8 · Google ADK Agent",
        gcp_services=("Google ADK Runner", "Document AI + Vision", "BigQuery Search + Amendments", "AST Calculator"),
        idle_mode="serverless_zero",
        embedding_model="gemini-embedding-2",
        query_embed_tokens=160,
        rank_counts_per_query=3.5,
        prompt_tokens_per_query=19_500,
        output_tokens_per_query=4_650,
        ingest_embed_tokens_per_page=520,
    ),
)

if tuple(m.level_id for m in _LEVEL_META) != ALL_LEVEL_IDS:
    raise RuntimeError("tco._LEVEL_META must stay aligned 1:1 with config.ALL_LEVEL_IDS")


def _diagram_page_ratio(doc_complexity: str) -> float:
    if doc_complexity == "tables_diagrams":
        return 0.25
    if doc_complexity == "multi_hop_math":
        return 0.20
    return 0.05


def _bq_scan_bytes_per_query(level_id: str, corpus_pages: int) -> float:
    if level_id == "level1_naive":
        table_bytes = corpus_pages * 250 * 1024
        selectivity = 0.08 if corpus_pages >= 600 else 1.0
        return max(float(BQ_MIN_SCAN_BYTES), table_bytes * selectivity) * BQ_UNCACHED_QUERY_RATIO
    if level_id == "level2_hybrid":
        table_bytes = corpus_pages * 28 * 1024
        selectivity = 0.15 if corpus_pages >= 5_000 else 1.0
        return max(float(BQ_MIN_SCAN_BYTES), table_bytes * selectivity) * BQ_UNCACHED_QUERY_RATIO
    if level_id == "level3_multimodal":
        table_bytes = corpus_pages * 62 * 1024
        return max(float(BQ_MIN_SCAN_BYTES), float(table_bytes)) * BQ_UNCACHED_QUERY_RATIO
    if level_id == "level4_agentic":
        table_bytes = corpus_pages * 62 * 1024
        return 4.5 * max(float(BQ_MIN_SCAN_BYTES), float(table_bytes)) * BQ_UNCACHED_QUERY_RATIO
    return 0.0


def _score_workload_fit(
    meta: _LevelStaticMeta,
    workload: TcoWorkload,
    *,
    total_monthly_usd: float,
    crossover: CrossoverAnalysis,
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    lid = meta.level_id
    score = 30
    rationale: list[str] = []
    tradeoffs: list[str] = []

    if lid == "level1_naive":
        score = 22
        rationale.append("Minimal SQL baseline with $0/mo idle cost for smoke-testing dense embeddings.")
        tradeoffs.append("Misses exact CLIN/clause identifiers, scrambles PDF tables, and ignores superseding amendments.")
        return score, tuple(rationale), tuple(tradeoffs)

    # 1. Data gravity alignment (-6 .. +22)
    dg = workload.data_gravity
    if dg == "bigquery_warehouse":
        if lid == "level2_hybrid":
            score += 18
            rationale.append("Keeps vector + lexical RRF retrieval inside BigQuery with zero cross-store ETL.")
        elif lid in ("level3_multimodal", "level4_agentic"):
            score += 13
            rationale.append("Stores layout/multimodal chunks natively in BigQuery alongside enterprise warehouse tables.")
        elif lid == "rag_engine":
            score += 5
        else:
            score -= 5
    elif dg == "postgres_oltp":
        if lid == "alloydb_scann":
            score += 22
            rationale.append("Runs ScaNN cosine ANN + PostgreSQL GIN full-text RRF transactionally inside AlloyDB.")
        elif lid == "vertex_vector_v2":
            score += 8
            rationale.append("Pairs low-latency managed ScaNN ANN with operational microservices.")
        elif lid == "level2_hybrid":
            score += 3
        else:
            score -= 5
    elif dg == "unstructured_pdfs":
        if lid in ("level3_multimodal", "level4_agentic"):
            score += 14
            rationale.append("Parses complex GCS PDFs with Document AI Layout Parser + Gemini Vision diagram transcription.")
        elif lid in ("rag_engine", "managed_vertex"):
            score += 11
            rationale.append("Imports unstructured PDFs directly from Cloud Storage without custom chunkers.")
        elif lid in ("level2_hybrid", "vertex_vector_v2"):
            score += 6
    elif dg == "managed_turnkey":
        if lid == "rag_engine":
            score += 20
            rationale.append("Managed RagCorpus handles PDF chunking, text-embedding-005 indexing, and $0/query retrieval.")
        elif lid == "managed_vertex":
            score += 18
            rationale.append("Fully managed Vertex AI Search data store with turnkey extractive segments and console ops.")
        elif lid == "level2_hybrid":
            score += 3
        else:
            score -= 6

    # 2. Document complexity alignment (-10 .. +24)
    dc = workload.doc_complexity
    if dc == "plain_text":
        if lid in ("level2_hybrid", "rag_engine", "managed_vertex", "alloydb_scann", "vertex_vector_v2"):
            score += 9
            rationale.append("Narrative & clause text retrieves cleanly without needing Document AI + Vision ingest spend.")
        elif lid == "level3_multimodal":
            score += 2
            tradeoffs.append("Adds $10/1k-page Document AI Layout Parser cost that plain-text corpora rarely need.")
        elif lid == "level4_agentic":
            score -= 6
            tradeoffs.append("Multi-turn ADK agent overhead (~3.5x tokens) is unnecessary for single-hop plain text.")
    elif dc == "tables_diagrams":
        if lid == "level3_multimodal":
            score += 20
            rationale.append("Preserves Markdown rate tables, transcribes architecture diagrams, and orders amendments first.")
        elif lid == "level4_agentic":
            score += 12
            rationale.append("Searches multimodal table/diagram chunks with self-verifying citation checks.")
        elif lid in ("rag_engine", "managed_vertex"):
            score += 3
            tradeoffs.append("Managed chunkers lack explicit Gemini Vision diagram transcription and amendment headers.")
        else:
            score -= 10
            tradeoffs.append("Flat text chunking loses multi-column PDF table structure and embedded vector diagrams.")
    elif dc == "multi_hop_math":
        if lid == "level4_agentic":
            score += 24
            rationale.append(
                "Google ADK loop chains check_amendments, AST-safe calculate_contract_pricing, and verify_citations."
            )
        elif lid == "level3_multimodal":
            score += 10
            rationale.append("Provides amendment authority headers and table structure, though single-pass without calculator.")
            tradeoffs.append("Single-pass synthesis lacks deterministic multi-solicitation arithmetic verification.")
        elif lid == "rag_engine":
            score += 3
            tradeoffs.append("Top-10 context recall is strong, but lacks ADK calculator and amendment-chain tools.")
        else:
            score -= 10
            tradeoffs.append("Single-pass retrieval misses multi-hop supersession chains and exact cross-doc math.")

    # 3. Latency SLA alignment (-20 .. +14)
    sla = workload.latency_sla
    if sla == "sub_second_retrieval":
        if lid in ("alloydb_scann", "vertex_vector_v2"):
            score += 14
            rationale.append("Sub-400ms warm ScaNN ANN + hybrid RRF retrieval built for real-time user-facing SLAs.")
        elif lid in ("managed_vertex", "rag_engine"):
            score += 5
        elif lid in ("level2_hybrid", "level3_multimodal"):
            score -= 3
            tradeoffs.append("BigQuery serverless query startup adds ~1–2s retrieval overhead vs in-memory ScaNN.")
        elif lid == "level4_agentic":
            score -= 20
            tradeoffs.append("Multi-turn ADK reasoning averages 44–47s, exceeding strict sub-second/low-latency SLAs.")
    elif sla == "interactive_rag":
        if lid in ("level2_hybrid", "level3_multimodal", "rag_engine", "managed_vertex", "alloydb_scann", "vertex_vector_v2"):
            score += 6
        elif lid == "level4_agentic":
            score -= 5
            tradeoffs.append("ADK multi-turn tool loop runs ~44–47s vs ~11–29s on single-pass architectures.")
    elif sla == "deep_agentic_reasoning":
        if lid == "level4_agentic":
            score += 14
            rationale.append("SLA budget accommodates iterative ADK search, amendment lookup, and citation repair.")
        elif lid in ("level3_multimodal", "rag_engine"):
            score += 6

    # 4. Compliance & operations posture (-10 .. +10)
    cp = workload.compliance_posture
    if cp == "standard_serverless":
        if meta.idle_mode == "serverless_zero":
            score += 6
            if workload.monthly_queries < crossover.vvs2_vs_vais_queries:
                rationale.append("Scales to $0.00/mo standing compute when idle — pay strictly per query.")
        else:
            if workload.monthly_queries < crossover.vvs2_vs_vais_queries:
                score -= 10
                tradeoffs.append("24x7 standing cluster compute is underutilized below the ~66k queries/mo crossover.")
    elif cp == "managed_turnkey":
        if lid in ("rag_engine", "managed_vertex"):
            score += 10
        elif lid in ("level4_agentic", "alloydb_scann"):
            score -= 8
            tradeoffs.append("Higher operational surface (custom DB/proxy or multi-tool agent orchestration).")
    elif cp == "cmek_vpc":
        if lid in ("alloydb_scann", "level3_multimodal", "level4_agentic", "level2_hybrid", "rag_engine", "vertex_vector_v2"):
            score += 6
            if lid == "alloydb_scann":
                rationale.append("Supports regional Private IP / VPC-SC perimeters, PostgreSQL RLS, and CMEK encryption.")
            elif lid in ("level2_hybrid", "level3_multimodal", "level4_agentic"):
                rationale.append("Supports regional BigQuery + GCS CMEK and row-level security policies.")
        if lid in ("level2_hybrid", "level3_multimodal", "level4_agentic", "alloydb_scann", "vertex_vector_v2"):
            tradeoffs.append("Verify Discovery Engine Ranking API regional endpoint alignment for strict single-region VPC-SC.")
        if lid == "managed_vertex":
            score -= 5
            tradeoffs.append("Default global Discovery Engine config requires regional datastore reconfiguration under strict residency.")

    # 5. Economic & corpus-scale crossover adjustments (-8 .. +10)
    if workload.monthly_queries >= 250_000:
        if lid in ("vertex_vector_v2", "alloydb_scann"):
            score += 10
            rationale.append("Zero per-query ANN scan fee beats per-query PaaS/scan charges past the monthly TCO crossover.")
        elif lid == "managed_vertex":
            score -= 6
            tradeoffs.append("$4.00/1k Vertex AI Search query pricing compounds rapidly at >=250k queries/month.")
    if workload.corpus_pages >= 50_000 and lid in ("level3_multimodal", "level4_agentic"):
        if workload.monthly_queries >= 50_000:
            score -= 8
            tradeoffs.append(
                "BigQuery allows 1 vector index/table, so unindexed multimodal brute-force scans become costly at >=50k pages."
            )

    if lid == "rag_engine" and not tradeoffs:
        tradeoffs.append("Default top-10 retrieval without Ranking API lowers context precision; add rerank in Workbench.")
    if lid == "managed_vertex" and not tradeoffs:
        tradeoffs.append("Prefer selectable Gemini over extractive segments; built-in VAIS summary is more conservative and abstains more often.")
    if lid == "level3_multimodal" and not tradeoffs:
        tradeoffs.append("Adds one-time Document AI Layout Parser + Gemini Vision ingestion cost and higher query latency than warm ScaNN.")
    if lid == "level4_agentic" and not tradeoffs:
        tradeoffs.append("Multi-turn ADK tool execution adds token cost and higher end-to-end latency than single-pass pipelines.")
    if lid == "level2_hybrid" and not tradeoffs:
        tradeoffs.append("Text-only chunking lacks Document AI table reconstruction and multi-hop calculator tools.")
    if lid == "vertex_vector_v2" and not tradeoffs:
        tradeoffs.append(f"Carries ${VVS2_BASE_REPLICA_MONTHLY_USD:.2f}/mo minReplicaCount=1 standing compute unless torn down.")
    if lid == "alloydb_scann" and not tradeoffs:
        tradeoffs.append(f"Carries ${ALLOYDB_BASE_MONTHLY_USD:.2f}/mo Zonal primary compute (~2x for Regional HA).")

    clamped = max(10, min(98, score))
    _ = total_monthly_usd
    return clamped, tuple(rationale[:3]), tuple(tradeoffs[:2])


def _compute_crossover(workload: TcoWorkload) -> CrossoverAnalysis:
    rank_per_q = PRICE_TABLE["discovery-engine-rank"].usd_per_1k_requests / 1_000.0
    emb_per_q = (45.0 / 1_000_000.0) * PRICE_TABLE["gemini-embedding-2"].input_usd_per_1m
    vais_per_q = PRICE_TABLE["vertex-ai-search-enterprise"].usd_per_1k_requests / 1_000.0

    # Per-query retrieval+rerank+embed delta between L1 (VAIS $4/1k) and L5/L6 ($1/1k rerank + embed)
    vais_minus_scann_per_q = max(0.0001, vais_per_q - (rank_per_q + emb_per_q))
    vvs2_vs_vais = int(math.ceil(VVS2_BASE_REPLICA_MONTHLY_USD / vais_minus_scann_per_q))
    alloydb_vs_vais = int(
        math.ceil((ALLOYDB_BASE_MONTHLY_USD + ALLOYDB_MIN_STORAGE_GIB * ALLOYDB_STORAGE_USD_PER_GIB_MONTH) / vais_minus_scann_per_q)
    )

    # Per-query scan delta between L7 BigQuery unindexed multimodal scan and L6 Vector Search 2.0
    l3_scan_per_q = (_bq_scan_bytes_per_query("level3_multimodal", workload.corpus_pages) / BYTES_PER_TIB) * BQ_SCAN_USD_PER_TIB
    vvs2_vs_bq_mm = int(math.ceil(VVS2_BASE_REPLICA_MONTHLY_USD / max(0.00001, l3_scan_per_q)))

    earliest_crossover = min(vvs2_vs_vais, vvs2_vs_bq_mm)
    if workload.monthly_queries < int(earliest_crossover * 0.85):
        regime = "serverless_favorable"
        summary = (
            f"At {workload.monthly_queries:,} queries/mo, $0/mo-idle serverless architectures (L1–L4, L7–L8) minimize TCO; "
            f"dedicated ScaNN crosses below VAIS at ~{vvs2_vs_vais:,} queries/mo and below BQ multimodal scan at "
            f"~{vvs2_vs_bq_mm:,} queries/mo ({workload.corpus_pages:,} pages)."
        )
    elif workload.monthly_queries <= int(max(alloydb_vs_vais, earliest_crossover) * 1.15):
        regime = "crossover_zone"
        summary = (
            f"Near the monthly TCO crossover (~{earliest_crossover:,}–{alloydb_vs_vais:,} queries/mo): "
            f"weigh sub-400ms dedicated ScaNN latency (L5/L6) against zero-ops serverless elasticity (L1/L2/L4/L7)."
        )
    else:
        regime = "dedicated_scann_favorable"
        summary = (
            f"Above the ~{earliest_crossover:,} queries/mo crossover: dedicated ScaNN (L5 AlloyDB / L6 Vector Search 2.0) "
            f"amortizes standing compute and avoids per-query VAIS ($4/1k) and BigQuery byte-scan fees."
        )

    return CrossoverAnalysis(
        vvs2_vs_vais_queries=vvs2_vs_vais,
        alloydb_vs_vais_queries=alloydb_vs_vais,
        vvs2_vs_bq_multimodal_queries=vvs2_vs_bq_mm,
        regime=regime,
        summary=summary,
    )


def estimate_enterprise_tco(
    workload: TcoWorkload,
    *,
    level_labels: Mapping[str, str] | None = None,
    recorded_cohorts: Mapping[tuple[str, str], RecordedCohortSnapshot] | None = None,
    fallback_cohorts: Mapping[str, RecordedCohortSnapshot] | None = None,
) -> EnterpriseTcoEstimate:
    """Compute pure deterministic monthly TCO, ingestion cost, crossover, and workload fit across L1..L8."""
    if not isinstance(workload, TcoWorkload):
        raise TypeError("workload must be a TcoWorkload instance")

    synth_price = PRICE_TABLE[workload.synthesis_model]
    rank_price = PRICE_TABLE["discovery-engine-rank"]
    vais_price = PRICE_TABLE["vertex-ai-search-enterprise"]
    vision_price = PRICE_TABLE[VISION_MODEL_ID]

    crossover = _compute_crossover(workload)
    pdf_gib = (workload.corpus_pages * PDF_BYTES_PER_PAGE) / BYTES_PER_GIB
    gcs_monthly = pdf_gib * GCS_STANDARD_USD_PER_GIB_MONTH
    diagram_ratio = _diagram_page_ratio(workload.doc_complexity)

    preliminary: list[tuple[_LevelStaticMeta, dict[str, Any], int, tuple[str, ...], tuple[str, ...]]] = []

    for meta in _LEVEL_META:
        lid = meta.level_id
        label = (level_labels or {}).get(lid, meta.default_label)

        # 1. Standing compute & capacity units
        if lid == "alloydb_scann":
            units = max(1, int(math.ceil(workload.monthly_queries / ALLOYDB_QUERIES_PER_UNIT_MONTH)))
            standing_usd = _round2(units * ALLOYDB_BASE_MONTHLY_USD)
            standing_note = (
                f"{units}x 2-vCPU / 16 GiB Zonal AlloyDB primary (${ALLOYDB_BASE_MONTHLY_USD:.2f}/mo per unit; "
                f"Regional HA ~2x; $0/mo idle when torn down via scripts/ephemeral_alloydb.sh)."
            )
        elif lid == "vertex_vector_v2":
            units = max(1, int(math.ceil(workload.monthly_queries / VVS2_QUERIES_PER_REPLICA_MONTH)))
            standing_usd = _round2(units * VVS2_BASE_REPLICA_MONTHLY_USD)
            standing_note = (
                f"{units}x PERFORMANCE_OPTIMIZED replica (minReplicaCount={units}, ${VVS2_BASE_REPLICA_MONTHLY_USD:.2f}/mo "
                f"per replica; $0/mo idle when torn down via scripts/ephemeral_vvs2.sh)."
            )
        elif lid == "rag_engine":
            units = 0
            standing_usd = 0.0
            standing_note = (
                f"$0.00/mo on RagManagedDb Serverless ($0/query retrieveContexts fee); optional Spanner Basic tier "
                f"adds ~${RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD:.2f}/mo (100 PU) for dedicated enterprise SLAs."
            )
        else:
            units = 0
            standing_usd = 0.0
            standing_note = "$0.00/mo serverless idle compute (pay strictly per query + active storage)."

        # 2. Monthly storage
        if lid == "level1_naive":
            chunk_gib = (workload.corpus_pages * 250 * 1024) / BYTES_PER_GIB
            storage_usd = gcs_monthly + chunk_gib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH
        elif lid == "level2_hybrid":
            chunk_gib = (workload.corpus_pages * 28 * 1024) / BYTES_PER_GIB
            storage_usd = gcs_monthly + chunk_gib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH
        elif lid in ("level3_multimodal", "level4_agentic"):
            chunk_gib = (workload.corpus_pages * 62 * 1024) / BYTES_PER_GIB
            storage_usd = gcs_monthly + chunk_gib * BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH
        elif lid == "managed_vertex":
            billable_vais_gib = max(0.0, pdf_gib - VAIS_FREE_STORAGE_GIB)
            storage_usd = gcs_monthly + billable_vais_gib * VAIS_STORAGE_USD_PER_GIB_MONTH
        elif lid == "rag_engine":
            rag_gib = (workload.corpus_pages * 32 * 1024) / BYTES_PER_GIB
            storage_usd = gcs_monthly + rag_gib * RAG_ENGINE_STORAGE_USD_PER_GIB_MONTH
        elif lid == "alloydb_scann":
            hybrid_gib = (workload.corpus_pages * 28 * 1024) / BYTES_PER_GIB
            alloy_gib = max(ALLOYDB_MIN_STORAGE_GIB, hybrid_gib * 1.5)
            storage_usd = gcs_monthly + alloy_gib * ALLOYDB_STORAGE_USD_PER_GIB_MONTH
        else:  # vertex_vector_v2
            hybrid_gib = (workload.corpus_pages * 28 * 1024) / BYTES_PER_GIB
            vvs_gib = max(VVS2_MIN_STORAGE_GIB, hybrid_gib * 1.3)
            storage_usd = gcs_monthly + vvs_gib * VVS2_STORAGE_USD_PER_GIB_MONTH
        storage_usd = _round2(storage_usd)

        # 3. Query-variable monthly costs
        if meta.embedding_model:
            emb_price = PRICE_TABLE[meta.embedding_model]
            embedding_usd = (
                (workload.monthly_queries * meta.query_embed_tokens) / 1_000_000.0
            ) * emb_price.input_usd_per_1m
        else:
            embedding_usd = 0.0

        if lid in ("level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic"):
            scan_bytes_per_q = _bq_scan_bytes_per_query(lid, workload.corpus_pages)
            retrieval_scan_usd = ((workload.monthly_queries * scan_bytes_per_q) / BYTES_PER_TIB) * BQ_SCAN_USD_PER_TIB
        elif lid == "managed_vertex":
            retrieval_scan_usd = (workload.monthly_queries / 1_000.0) * vais_price.usd_per_1k_requests
        else:
            retrieval_scan_usd = 0.0

        rerank_usd = ((workload.monthly_queries * meta.rank_counts_per_query) / 1_000.0) * rank_price.usd_per_1k_requests
        synthesis_usd = (
            ((workload.monthly_queries * meta.prompt_tokens_per_query) / 1_000_000.0) * synth_price.input_usd_per_1m
            + ((workload.monthly_queries * meta.output_tokens_per_query) / 1_000_000.0) * synth_price.output_usd_per_1m
        )

        embedding_usd = _round2(embedding_usd)
        retrieval_scan_usd = _round2(retrieval_scan_usd)
        rerank_usd = _round2(rerank_usd)
        synthesis_usd = _round2(synthesis_usd)
        query_variable_usd = _round2(embedding_usd + retrieval_scan_usd + rerank_usd + synthesis_usd)
        total_monthly_usd = _round2(standing_usd + storage_usd + query_variable_usd)
        cost_per_1k_usd = _round2((total_monthly_usd / workload.monthly_queries) * 1_000.0)

        # 4. One-time corpus ingestion cost
        if meta.embedding_model and meta.ingest_embed_tokens_per_page > 0:
            ing_emb_price = PRICE_TABLE[meta.embedding_model]
            ingest_embed_usd = (
                (workload.corpus_pages * meta.ingest_embed_tokens_per_page) / 1_000_000.0
            ) * ing_emb_price.input_usd_per_1m
        else:
            ingest_embed_usd = 0.0

        if lid in ("level3_multimodal", "level4_agentic"):
            docai_usd = (workload.corpus_pages / 1_000.0) * DOCAI_LAYOUT_USD_PER_1K_PAGES
            diagram_pages = workload.corpus_pages * diagram_ratio
            vision_usd = (
                ((diagram_pages * VISION_PROMPT_TOKENS_PER_DIAGRAM_PAGE) / 1_000_000.0) * vision_price.input_usd_per_1m
                + ((diagram_pages * VISION_OUTPUT_TOKENS_PER_DIAGRAM_PAGE) / 1_000_000.0) * vision_price.output_usd_per_1m
            )
            one_time_ingest_usd = _round2(docai_usd + vision_usd + ingest_embed_usd)
            ingest_note = (
                f"Document AI Layout Parser ($10/1k pages = ${docai_usd:.2f}) + Gemini Vision on ~{int(diagram_ratio * 100)}% "
                f"diagram pages (${vision_usd:.2f}) + embeddings (${ingest_embed_usd:.2f})."
            )
        elif lid == "managed_vertex":
            one_time_ingest_usd = 0.0
            ingest_note = "$0.00 separate parse/embedding fee on standard digital PDF import into Vertex AI Search."
        elif lid == "rag_engine":
            one_time_ingest_usd = _round2(ingest_embed_usd)
            ingest_note = "Managed RagCorpus PDF chunking ($0 parse fee) + text-embedding-005 corpus embeddings."
        else:
            one_time_ingest_usd = _round2(ingest_embed_usd)
            ingest_note = "PyMuPDF text extraction ($0 parse fee) + gemini-embedding-2 3072-d vector indexing."

        fit_score, rationale, tradeoffs = _score_workload_fit(
            meta, workload, total_monthly_usd=total_monthly_usd, crossover=crossover
        )

        exact_cohort = (recorded_cohorts or {}).get((lid, workload.synthesis_model))
        alt_cohort = None
        if exact_cohort is None and fallback_cohorts is not None:
            candidate_alt = fallback_cohorts.get(lid)
            if candidate_alt is not None and candidate_alt.synthesis_model != workload.synthesis_model:
                alt_cohort = candidate_alt

        preliminary.append(
            (
                meta,
                {
                    "label": label,
                    "capacity_units": units,
                    "standing_monthly_usd": standing_usd,
                    "standing_note": standing_note,
                    "storage_monthly_usd": storage_usd,
                    "embedding_monthly_usd": embedding_usd,
                    "retrieval_scan_monthly_usd": retrieval_scan_usd,
                    "rerank_monthly_usd": rerank_usd,
                    "synthesis_monthly_usd": synthesis_usd,
                    "query_variable_monthly_usd": query_variable_usd,
                    "total_monthly_usd": total_monthly_usd,
                    "cost_per_1k_queries_usd": cost_per_1k_usd,
                    "one_time_ingestion_usd": one_time_ingest_usd,
                    "ingestion_note": ingest_note,
                    "recorded_cohort": exact_cohort,
                    "alternate_cohort": alt_cohort,
                },
                fit_score,
                rationale,
                tradeoffs,
            )
        )

    # Rank non-baseline architectures deterministically: highest fit_score, then lowest total_monthly_usd, then order
    ranked_candidates = sorted(
        (item for item in preliminary if item[0].level_id != "level1_naive"),
        key=lambda item: (-item[2], item[1]["total_monthly_usd"], item[0].order),
    )
    recommended_id = ranked_candidates[0][0].level_id
    runner_up_id = ranked_candidates[1][0].level_id

    rows: list[ArchitectureTcoRow] = []
    for meta, costs, fit_score, rationale, tradeoffs in preliminary:
        lid = meta.level_id
        if lid == recommended_id:
            tier = "recommended"
        elif lid == runner_up_id:
            tier = "runner_up"
        elif lid == "level1_naive" or fit_score < 45:
            tier = "baseline" if lid == "level1_naive" else "not_recommended"
        else:
            tier = "viable"
        rows.append(
            ArchitectureTcoRow(
                level_id=lid,
                order=meta.order,
                label=costs["label"],
                short_label=meta.short_label,
                gcp_services=meta.gcp_services,
                idle_mode=meta.idle_mode,
                capacity_units=costs["capacity_units"],
                standing_monthly_usd=costs["standing_monthly_usd"],
                standing_note=costs["standing_note"],
                storage_monthly_usd=costs["storage_monthly_usd"],
                embedding_monthly_usd=costs["embedding_monthly_usd"],
                retrieval_scan_monthly_usd=costs["retrieval_scan_monthly_usd"],
                rerank_monthly_usd=costs["rerank_monthly_usd"],
                synthesis_monthly_usd=costs["synthesis_monthly_usd"],
                query_variable_monthly_usd=costs["query_variable_monthly_usd"],
                total_monthly_usd=costs["total_monthly_usd"],
                cost_per_1k_queries_usd=costs["cost_per_1k_queries_usd"],
                one_time_ingestion_usd=costs["one_time_ingestion_usd"],
                ingestion_note=costs["ingestion_note"],
                fit_score=fit_score,
                fit_tier=tier,
                rationale=rationale,
                tradeoffs=tradeoffs,
                recorded_cohort=costs["recorded_cohort"],
                alternate_cohort=costs["alternate_cohort"],
            )
        )

    return EnterpriseTcoEstimate(
        workload=workload,
        recommended_level_id=recommended_id,
        runner_up_level_id=runner_up_id,
        crossover=crossover,
        architectures=tuple(rows),
    )


def pricing_basis_metadata() -> dict[str, Any]:
    """Return auditable published GCP pricing metadata used by the TCO engine."""
    return {
        "price_as_of": "2026-11-01",
        "region_basis": "us-central1 (models on global Vertex endpoint per pricing.PRICE_TABLE)",
        "hours_per_month": HOURS_PER_MONTH,
        "models": {
            model_id: {
                "input_usd_per_1m": PRICE_TABLE[model_id].input_usd_per_1m,
                "output_usd_per_1m": PRICE_TABLE[model_id].output_usd_per_1m,
                "usd_per_1k_requests": PRICE_TABLE[model_id].usd_per_1k_requests,
                "source": PRICE_TABLE[model_id].source,
                "price_as_of": PRICE_TABLE[model_id].price_as_of,
            }
            for model_id in (
                "gemini-3.8-flash",
                "gemini-4-argon",
                "gemini-embedding-2",
                "text-embedding-005",
                "discovery-engine-rank",
                "vertex-ai-search-enterprise",
            )
        },
        "infrastructure": {
            "bigquery_scan_usd_per_tib": BQ_SCAN_USD_PER_TIB,
            "bigquery_active_storage_usd_per_gib_month": BQ_ACTIVE_STORAGE_USD_PER_GIB_MONTH,
            "gcs_standard_usd_per_gib_month": GCS_STANDARD_USD_PER_GIB_MONTH,
            "docai_layout_parser_usd_per_1k_pages": DOCAI_LAYOUT_USD_PER_1K_PAGES,
            "vais_storage_usd_per_gib_month_after_10gib": VAIS_STORAGE_USD_PER_GIB_MONTH,
            "rag_engine_serverless_standing_usd": 0.0,
            "rag_engine_spanner_basic_optional_monthly_usd": RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD,
            "alloydb_zonal_2vcpu_16gib_monthly_usd": ALLOYDB_BASE_MONTHLY_USD,
            "vvs2_performance_replica_monthly_usd": VVS2_BASE_REPLICA_MONTHLY_USD,
        },
    }


__all__ = [
    "ALLOYDB_BASE_MONTHLY_USD",
    "COMPLIANCE_POSTURE_OPTIONS",
    "CORPUS_PAGES_BOUNDS",
    "DATA_GRAVITY_OPTIONS",
    "DEFAULT_WORKLOAD",
    "DOC_COMPLEXITY_OPTIONS",
    "ENTERPRISE_SCENARIO_PRESETS",
    "LATENCY_SLA_OPTIONS",
    "MONTHLY_QUERIES_BOUNDS",
    "RAG_ENGINE_SPANNER_BASIC_MONTHLY_USD",
    "TCO_SYNTHESIS_MODELS",
    "VVS2_BASE_REPLICA_MONTHLY_USD",
    "ArchitectureTcoRow",
    "CrossoverAnalysis",
    "EnterpriseTcoEstimate",
    "RecordedCohortSnapshot",
    "ScenarioPreset",
    "TcoValidationError",
    "TcoWorkload",
    "estimate_enterprise_tco",
    "parse_tco_workload",
    "pricing_basis_metadata",
]
