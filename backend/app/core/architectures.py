"""ArchitectureDescriptor design metadata for all five levels (spec §7.2.1).

Descriptors describe which GCP services exist in a level and how they connect. They are pure design
metadata: no timings, scores, or results. All dynamic state on the diagram comes from SSE stage events
keyed by the canonical ``stage_ids`` below.

Model selection: the ``llm`` (synthesis) and ``judge`` node ``service`` labels are the settings-resolved
DEFAULTS (``GEMINI_FAST_MODEL`` / ``JUDGE_MODEL``). Users may pick other catalog models per run
(``app.core.models_catalog``), so the per-run truth is the ``service`` (``"Vertex AI · <label>"``) and
``request.model``/``request.provider`` carried by that run's ``synthesize`` / ``evaluate`` ``stage_started``
events — the frontend prefers those over the descriptor label once a run is in progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

CANONICAL_QUERY_STAGES: tuple[str, ...] = (
    "query_prep",
    "embed_query",
    "retrieve",
    "rerank",
    "agent_turn",
    "synthesize",
    "evaluate",
)
CANONICAL_INGEST_STAGES: tuple[str, ...] = (
    "discover_docs",
    "parse",
    "chunk",
    "embed",
    "load_bigquery",
    "build_indexes",
    "verify_counts",
)
# Level 5 (managed_vertex) ingestion: the corpus is synced to GCS and imported by Vertex AI Search; the BigQuery
# stages are emitted as ``stage_skipped`` ("managed by Vertex AI Search").
MANAGED_INGEST_STAGES: tuple[str, ...] = (
    "discover_docs",
    "upload_gcs",
    "import_documents",
    "parse",
    "chunk",
    "embed",
    "load_bigquery",
    "build_indexes",
    "verify_counts",
)
MANAGED_SKIPPED_INGEST_STAGES: tuple[str, ...] = ("parse", "chunk", "embed", "load_bigquery", "build_indexes")
ALL_INGEST_STAGES: tuple[str, ...] = CANONICAL_INGEST_STAGES + ("upload_gcs", "import_documents")


def ingest_stages_for(level_id: str) -> tuple[str, ...]:
    """Ingest stage ids a level's ingestion job emits, in order."""
    return MANAGED_INGEST_STAGES if level_id == "managed_vertex" else CANONICAL_INGEST_STAGES


_NODE_KINDS: tuple[str, ...] = ("actor", "model", "store", "api", "process", "agent")


@dataclass(frozen=True)
class ArchNode:
    """One service box on the diagram; ``stage_ids`` drives highlighting from stage events."""

    id: str
    label: str
    service: str
    kind: str
    col: int
    row: int
    stage_ids: tuple[str, ...] = ()
    region: str = ""
    code_ref: str = ""
    # Level 4: the ADK tool name(s) whose calls light this node up inside the ``agent_turn`` stage.
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool_names, tuple) or not all(isinstance(t, str) and t for t in self.tool_names):
            raise TypeError("tool_names must be a tuple of non-empty strings")
        if not self.id or not self.label or not self.service:
            raise ValueError("ArchNode id, label and service must be non-empty")
        if not isinstance(self.region, str) or not isinstance(self.code_ref, str):
            raise TypeError("region and code_ref must be strings")
        if self.kind not in _NODE_KINDS:
            raise ValueError(f"ArchNode.kind must be one of {_NODE_KINDS}, got {self.kind!r}")
        if self.col < 0 or self.row < 0:
            raise ValueError("ArchNode col/row must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        """Serialize; ``region``/``code_ref`` are optional keys emitted only when known."""
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "service": self.service,
            "kind": self.kind,
            "col": self.col,
            "row": self.row,
            "stage_ids": list(self.stage_ids),
        }
        if self.region:
            out["region"] = self.region
        if self.code_ref:
            out["code_ref"] = self.code_ref
        if self.tool_names:
            out["tool_names"] = list(self.tool_names)
        return out


@dataclass(frozen=True)
class ArchEdge:
    """Directed data flow between two nodes."""

    source: str
    target: str
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "label": self.label}


@dataclass(frozen=True)
class ArchitectureDescriptor:
    """Per-level diagram description returned by ``GET /api/v1/levels``."""

    level_id: str
    nodes: tuple[ArchNode, ...]
    edges: tuple[ArchEdge, ...]
    ingest_nodes: tuple[ArchNode, ...] = field(default_factory=tuple)
    ingest_edges: tuple[ArchEdge, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for nodes, edges, name in (
            (self.nodes, self.edges, "nodes"),
            (self.ingest_nodes, self.ingest_edges, "ingest_nodes"),
        ):
            ids = [n.id for n in nodes]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate node ids in {self.level_id}.{name}")
            for edge in edges:
                if edge.source not in ids or edge.target not in ids:
                    raise ValueError(f"dangling edge {edge.source}->{edge.target} in {self.level_id}.{name}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_id": self.level_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "ingest_nodes": [n.to_dict() for n in self.ingest_nodes],
            "ingest_edges": [e.to_dict() for e in self.ingest_edges],
        }

    def with_regions(
        self,
        *,
        gcp_location: str,
        vertex_genai_location: str,
        judge_location: str = "",
        judge_model: str = "",
        docai_location: str = "",
        vision_model: str = "",
        multimodal_embedding_model: str = "",
        vais_location: str = "",
    ) -> "ArchitectureDescriptor":
        """Resolve region + model placeholders from settings (regions/models are never hardcoded)."""
        if not gcp_location or not vertex_genai_location:
            raise ValueError("gcp_location and vertex_genai_location must be non-empty")
        mapping = {GCP_LOCATION_PLACEHOLDER: gcp_location, GENAI_LOCATION_PLACEHOLDER: vertex_genai_location}
        if judge_location:
            mapping[JUDGE_LOCATION_PLACEHOLDER] = judge_location
        if docai_location:
            mapping[DOCAI_LOCATION_PLACEHOLDER] = docai_location
        if vais_location:
            mapping[VAIS_LOCATION_PLACEHOLDER] = vais_location
        services = {JUDGE_SERVICE_PLACEHOLDER: f"Vertex AI · {judge_model}"} if judge_model else {}
        if vision_model:
            services[VISION_SERVICE_PLACEHOLDER] = f"Vertex AI · {vision_model} (vision)"
        if multimodal_embedding_model:
            services[MM_EMBED_SERVICE_PLACEHOLDER] = f"Vertex AI · {multimodal_embedding_model} (multimodal)"

        def _resolve(nodes: tuple[ArchNode, ...]) -> tuple[ArchNode, ...]:
            return tuple(
                replace(n, region=mapping.get(n.region, n.region), service=services.get(n.service, n.service))
                for n in nodes
            )

        return replace(self, nodes=_resolve(self.nodes), ingest_nodes=_resolve(self.ingest_nodes))

    def resolved(self, settings: Any) -> "ArchitectureDescriptor":
        """Resolve every placeholder from an ``ArenaSettings``-like object."""
        return self.with_regions(
            gcp_location=settings.gcp_location,
            vertex_genai_location=settings.vertex_genai_location,
            judge_location=settings.judge_location,
            judge_model=settings.judge_model,
            docai_location=getattr(settings, "docai_location", ""),
            vision_model=getattr(settings, "vision_model", ""),
            multimodal_embedding_model=getattr(settings, "multimodal_embedding_model", ""),
            vais_location=getattr(settings, "vais_location", ""),
        )


GCP_LOCATION_PLACEHOLDER = "{GCP_LOCATION}"
GENAI_LOCATION_PLACEHOLDER = "{VERTEX_GENAI_LOCATION}"
JUDGE_LOCATION_PLACEHOLDER = "{JUDGE_LOCATION}"
JUDGE_SERVICE_PLACEHOLDER = "Vertex AI · {JUDGE_MODEL}"
DOCAI_LOCATION_PLACEHOLDER = "{DOCAI_LOCATION}"
VAIS_LOCATION_PLACEHOLDER = "{VAIS_LOCATION}"
VISION_SERVICE_PLACEHOLDER = "Vertex AI · {VISION_MODEL} (vision)"
MM_EMBED_SERVICE_PLACEHOLDER = "Vertex AI · {MULTIMODAL_EMBEDDING_MODEL} (multimodal)"
_GENAI_REGION = GENAI_LOCATION_PLACEHOLDER
_BQ_REGION = GCP_LOCATION_PLACEHOLDER
_USER = ArchNode("user", "User question", "Browser", "actor", 0, 1, ("query_prep",))
_EMBED = ArchNode(
    "embed", "Query embedding", "Vertex AI · gemini-embedding-2", "model", 1, 0, ("embed_query",),
    region=_GENAI_REGION, code_ref="app.engines.level1_naive:Level1NaiveEngine._embed_query",
)
_LLM = ArchNode(
    "llm", "Answer synthesis", "Vertex AI · gemini-4-argon", "model", 4, 1, ("synthesize",),
    region=_GENAI_REGION, code_ref="app.engines.level1_naive:Level1NaiveEngine._synthesize",
)
_JUDGE_CODE_REF = "app.evaluation.judges:build_judge"
_JUDGE = ArchNode(
    "judge", "LLM-as-a-judge", JUDGE_SERVICE_PLACEHOLDER, "model", 5, 1, ("evaluate",),
    region=JUDGE_LOCATION_PLACEHOLDER, code_ref=_JUDGE_CODE_REF,
)


def _bq_ingest(
    parser_label: str, parser_service: str, embed_col: str, extra_index: str, chunker_label: str = "Chunker"
) -> tuple[tuple[ArchNode, ...], tuple[ArchEdge, ...]]:
    """Shared BigQuery ingestion sub-diagram: corpus → parser → chunker → embedder → BigQuery → indexes."""
    nodes = (
        ArchNode("corpus", "Procurement corpus (local)", "Local corpus · data/sample_docs", "store", 0, 0, ("discover_docs",)),
        ArchNode("parser", parser_label, parser_service, "process", 1, 0, ("parse",)),
        ArchNode("chunker", chunker_label, "Ingestion job", "process", 2, 0, ("chunk",)),
        ArchNode("embedder", "Chunk embeddings", "Vertex AI · gemini-embedding-2", "model", 3, 0, ("embed",)),
        ArchNode("bq_load", "document_chunks", "BigQuery", "store", 4, 0, ("load_bigquery", "verify_counts")),
        ArchNode("index", "Vector" + extra_index + " index", "BigQuery", "store", 5, 0, ("build_indexes",)),
    )
    edges = (
        ArchEdge("corpus", "parser", "PDF bytes"),
        ArchEdge("parser", "chunker", "pages + bbox"),
        ArchEdge("chunker", "embedder", "chunk text"),
        ArchEdge("embedder", "bq_load", embed_col),
        ArchEdge("bq_load", "index", "CREATE INDEX"),
    )
    return nodes, edges


def _level1() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _bq_ingest("PDF text parser", "PyMuPDF", "embedding", "")
    bq = ArchNode(
        "bq", "document_chunks", "BigQuery VECTOR_SEARCH", "store", 2, 1, ("retrieve",),
        region=_BQ_REGION, code_ref="app.engines.level1_naive:Level1NaiveEngine._retrieve",
    )
    user = ArchNode(
        "user", "User question", "Browser", "actor", 0, 1, ("query_prep",),
        code_ref="app.engines.level1_naive:Level1NaiveEngine._query_prep",
    )
    return ArchitectureDescriptor(
        level_id="level1_naive",
        nodes=(user, _EMBED, bq, _LLM, _JUDGE),
        edges=(
            ArchEdge("user", "embed", "text"),
            ArchEdge("embed", "bq", "3072-d vector"),
            ArchEdge("bq", "llm", "top-k chunks"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


_L2_REF = "app.engines.level2_hybrid:Level2HybridEngine"


def _level2() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _bq_ingest(
        "PDF text parser", "PyMuPDF", "embedding", " + search", chunker_label="Contextual-lineage chunker (1 per page)"
    )
    user = ArchNode(
        "user", "User question + acronym expansion", "Engine (local)", "actor", 0, 1, ("query_prep",),
        code_ref=f"{_L2_REF}._query_prep",
    )
    embed = replace(_EMBED, code_ref="app.engines.common_stages:embed_query_stage")
    bq = ArchNode(
        "bq", "document_chunks (tier hybrid)", "BigQuery VECTOR_SEARCH + SEARCH() → RRF (k=60)", "store", 2, 1,
        ("retrieve",), region=_BQ_REGION, code_ref=f"{_L2_REF}._retrieve",
    )
    rank = ArchNode(
        "rank", "Semantic rerank", "Discovery Engine Ranking API", "api", 3, 1, ("rerank",),
        region="global", code_ref=f"{_L2_REF}._rerank",
    )
    llm = replace(_LLM, code_ref=f"{_L2_REF}._synthesize")
    return ArchitectureDescriptor(
        level_id="level2_hybrid",
        nodes=(user, embed, bq, rank, llm, _JUDGE),
        edges=(
            ArchEdge("user", "embed", "prepared query"),
            ArchEdge("embed", "bq", "3072-d vector"),
            ArchEdge("user", "bq", "lexical terms"),
            ArchEdge("bq", "rank", "RRF candidates (≤ 50)"),
            ArchEdge("rank", "llm", "top-5 reranked chunks"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


_L3_REF = "app.engines.level3_multimodal:Level3MultimodalEngine"
_L3_INGEST_REF = "app.ingestion.multimodal_job"


def _level3_ingest() -> tuple[tuple[ArchNode, ...], tuple[ArchEdge, ...]]:
    """corpus → Document AI Layout Parser → Gemini vision (diagram pages) → element chunker → embeddings → BigQuery → index."""
    nodes = (
        ArchNode("corpus", "Procurement corpus (local)", "Local corpus · data/sample_docs", "store", 0, 0, ("discover_docs",)),
        ArchNode(
            "docai", "Layout Parser (tables · headings · figures)", "Document AI Layout Parser", "api", 1, 0, ("parse",),
            region=DOCAI_LOCATION_PLACEHOLDER, code_ref="app.ingestion.layout_documentai:LayoutParser.parse",
        ),
        ArchNode(
            "vision", "Diagram transcription (diagram pages)", VISION_SERVICE_PLACEHOLDER, "model", 1, 1, ("parse",),
            region=_GENAI_REGION, code_ref="app.ingestion.vision_transcribe:GeminiVisionTranscriber.transcribe",
        ),
        ArchNode(
            "chunker", "Element chunker (tables · diagrams · heading groups)", "Ingestion job", "process", 2, 0, ("chunk",),
            code_ref="app.ingestion.multimodal:ChunkBuilder.build",
        ),
        ArchNode(
            "embedder", "Chunk embeddings (text · text+image)", MM_EMBED_SERVICE_PLACEHOLDER, "model", 3, 0, ("embed",),
            region=_GENAI_REGION, code_ref="app.ingestion.multimodal:VertexMultimodalEmbedder.embed_items",
        ),
        ArchNode("bq_load", "document_chunks (tier multimodal)", "BigQuery", "store", 4, 0, ("load_bigquery", "verify_counts"), region=_BQ_REGION),
        ArchNode("index", "Vector (embedding_multimodal) + search index", "BigQuery", "store", 5, 0, ("build_indexes",), region=_BQ_REGION),
    )
    edges = (
        ArchEdge("corpus", "docai", "PDF bytes (≤15-page ranges)"),
        ArchEdge("corpus", "vision", "diagram page PNG"),
        ArchEdge("docai", "chunker", "layout elements"),
        ArchEdge("vision", "chunker", "component/flow spec"),
        ArchEdge("chunker", "embedder", "chunk text (+ page image)"),
        ArchEdge("embedder", "bq_load", "embedding_multimodal"),
        ArchEdge("bq_load", "index", "CREATE INDEX"),
    )
    return nodes, edges


def _level3() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _level3_ingest()
    user = ArchNode(
        "user", "User question + element intent", "Engine (local)", "actor", 0, 1, ("query_prep",),
        code_ref=f"{_L3_REF}._query_prep_l3",
    )
    embed = ArchNode(
        "embed", "Query embedding", MM_EMBED_SERVICE_PLACEHOLDER, "model", 1, 0, ("embed_query",),
        region=_GENAI_REGION, code_ref="app.engines.common_stages:embed_query_stage",
    )
    bq = ArchNode(
        "bq", "document_chunks (tier multimodal)", "BigQuery VECTOR_SEARCH(embedding_multimodal) + SEARCH() → RRF", "store",
        2, 1, ("retrieve",), region=_BQ_REGION, code_ref=f"{_L3_REF}._retrieve",
    )
    rank = ArchNode(
        "rank", "Semantic rerank", "Discovery Engine Ranking API", "api", 3, 1, ("rerank",),
        region="global", code_ref=f"{_L2_REF}._rerank",
    )
    llm = replace(_LLM, label="Authority-headed synthesis", code_ref=f"{_L3_REF}._synthesize_l3")
    return ArchitectureDescriptor(
        level_id="level3_multimodal",
        nodes=(user, embed, bq, rank, llm, _JUDGE),
        edges=(
            ArchEdge("user", "embed", "prepared query"),
            ArchEdge("embed", "bq", "3072-d vector"),
            ArchEdge("user", "bq", "lexical terms + pre-filter"),
            ArchEdge("bq", "rank", "RRF candidates (≤ 50)"),
            ArchEdge("rank", "llm", "top-5 · amendments first"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


_L4_REF = "app.engines.level4_agentic:Level4AgenticEngine"
_L4_TOOLS_REF = "app.engines.agent_tools"


def _level4_tool_nodes() -> tuple[ArchNode, ...]:
    """The four ADK tools and the services they call; all light up during ``agent_turn`` (per tool via ``tool_names``)."""
    turn, search, amend = ("agent_turn",), ("search_procurement_corpus",), ("check_amendments",)
    return (
        ArchNode("tool_search", "search_procurement_corpus", "Level 3 retrieval (embed → RRF → rerank)", "process", 2, 0, turn,
                 code_ref=f"{_L4_TOOLS_REF}:run_search", tool_names=search),
        ArchNode("tool_amend", "check_amendments", "BigQuery parameterized amends_section lookup", "process", 2, 1, turn,
                 code_ref=f"{_L4_TOOLS_REF}:run_check_amendments", tool_names=amend),
        ArchNode("tool_calc", "calculate_contract_pricing", "Local AST-validated arithmetic", "process", 2, 2, turn,
                 code_ref="app.engines.agent_calculator:evaluate_expression", tool_names=("calculate_contract_pricing",)),
        ArchNode("tool_verify", "verify_citations", "Local claim ↔ cited-chunk check", "process", 2, 3, turn,
                 code_ref="app.engines.agent_verify:check_claims", tool_names=("verify_citations",)),
        ArchNode("embed", "Query embedding (multimodal space)", MM_EMBED_SERVICE_PLACEHOLDER, "model", 3, 0, turn,
                 region=_GENAI_REGION, code_ref="app.engines.common_stages:embed_query_stage", tool_names=search),
        ArchNode("bq", "document_chunks (tier multimodal)", "BigQuery VECTOR_SEARCH + SEARCH() → RRF · amendment lookup", "store", 3, 1,
                 turn, region=_BQ_REGION, code_ref=f"{_L3_REF}._retrieve", tool_names=search + amend),
        ArchNode("rank", "Semantic rerank", "Discovery Engine Ranking API", "api", 4, 0, turn, region="global",
                 code_ref=f"{_L3_REF}._rerank", tool_names=search),
    )


def _level4() -> ArchitectureDescriptor:
    """User → query_prep → ADK agent (Gemini) ⇄ tools → final response → judge (reuses the Level 3 multimodal tier)."""
    ingest_nodes, ingest_edges = _level3_ingest()
    user = ArchNode("user", "User question + query prep", "Engine (local)", "actor", 0, 1, ("query_prep",),
                    code_ref=f"{_L4_REF}._query_prep")
    agent = ArchNode("agent", "ADK LlmAgent loop", "Google ADK InMemoryRunner · Vertex AI Gemini", "agent", 1, 1, ("agent_turn",),
                     region=_GENAI_REGION, code_ref="app.engines.agent_runtime:run_agent")
    final = ArchNode("llm", "Final response (ADK)", "Google ADK · final model turn", "model", 4, 2, ("synthesize",),
                     region=_GENAI_REGION, code_ref=f"{_L4_REF}._synthesize")
    judge = replace(_JUDGE, col=5, row=2)
    return ArchitectureDescriptor(
        level_id="level4_agentic",
        nodes=(user, agent, *_level4_tool_nodes(), final, judge),
        edges=(
            ArchEdge("user", "agent", "question + expansions + intent"),
            ArchEdge("agent", "tool_search", "tool call"),
            ArchEdge("agent", "tool_amend", "tool call"),
            ArchEdge("agent", "tool_calc", "tool call"),
            ArchEdge("agent", "tool_verify", "tool call"),
            ArchEdge("tool_search", "embed", "query"),
            ArchEdge("embed", "bq", "3072-d vector"),
            ArchEdge("bq", "rank", "RRF candidates (≤ 50)"),
            ArchEdge("rank", "tool_search", "top-5 chunks"),
            ArchEdge("tool_amend", "bq", "@solicitation_id · @section_id"),
            ArchEdge("tool_search", "agent", "chunks"),
            ArchEdge("tool_amend", "agent", "superseding text"),
            ArchEdge("tool_calc", "agent", "value"),
            ArchEdge("tool_verify", "agent", "unsupported claims"),
            ArchEdge("agent", "llm", "final turn"),
            ArchEdge("llm", "judge", "answer + tool-returned chunks"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


_MV_REF = "app.engines.managed_vertex:ManagedVertexSearchEngine"
_MV_JOB_REF = "app.ingestion.managed_job"


def _managed_ingest() -> tuple[tuple[ArchNode, ...], tuple[ArchEdge, ...]]:
    """Local corpus → GCS mirror (changed files only) → documents:import → (managed parse/chunk/embed/index) → count."""
    nodes = (
        ArchNode("corpus", "Procurement corpus (local)", "Local corpus · data/sample_docs", "store", 0, 0, ("discover_docs",)),
        ArchNode("gcs", "Corpus mirror (sample_docs/)", "Cloud Storage · upload changed/missing PDFs", "store", 1, 0,
                 ("upload_gcs",), region=GCP_LOCATION_PLACEHOLDER, code_ref=f"{_MV_JOB_REF}:_upload_gcs"),
        ArchNode("import", "documents:import (INCREMENTAL)", "Vertex AI Search · Discovery Engine import LRO", "api", 2, 0,
                 ("import_documents",), region=VAIS_LOCATION_PLACEHOLDER, code_ref=f"{_MV_JOB_REF}:_import_documents"),
        ArchNode("managed", "Parse · chunk · embed · index", "Managed inside Vertex AI Search (not observable)", "process", 3, 1,
                 MANAGED_SKIPPED_INGEST_STAGES, region=VAIS_LOCATION_PLACEHOLDER),
        ArchNode("ds", "Data store documents", "Vertex AI Search · documents.list", "store", 4, 0, ("verify_counts",),
                 region=VAIS_LOCATION_PLACEHOLDER, code_ref=f"{_MV_JOB_REF}:_verify_counts"),
    )
    edges = (
        ArchEdge("corpus", "gcs", "PDFs (md5 diff)"),
        ArchEdge("gcs", "import", "gs:// URIs"),
        ArchEdge("import", "managed", "managed processing"),
        ArchEdge("managed", "ds", "indexed documents"),
    )
    return nodes, edges


def _managed() -> ArchitectureDescriptor:
    """User → query prep → Vertex AI Search app (data store; extractive segments [+ summary]) → synthesis → judge."""
    ingest_nodes, ingest_edges = _managed_ingest()
    user = ArchNode("user", "User question + query prep", "Engine (local)", "actor", 0, 1, ("query_prep",),
                    code_ref=f"{_MV_REF}._query_prep")
    search = ArchNode("search", "Search app (Enterprise + LLM add-on)", "Vertex AI Search · servingConfigs/default_search:search",
                      "api", 1, 1, ("retrieve",), region=VAIS_LOCATION_PLACEHOLDER, code_ref=f"{_MV_REF}._retrieve")
    ds = ArchNode("ds", "Data store (unstructured PDFs)", "Vertex AI Search data store · digital parser", "store", 2, 0,
                  ("retrieve",), region=VAIS_LOCATION_PLACEHOLDER)
    llm = ArchNode("llm", "Answer synthesis", "Vertex AI Search summary | catalog model", "model", 3, 1, ("synthesize",),
                   region=VAIS_LOCATION_PLACEHOLDER, code_ref=f"{_MV_REF}._synthesize_summary | _synthesize_model")
    judge = replace(_JUDGE, col=4, row=1)
    return ArchitectureDescriptor(
        level_id="managed_vertex",
        nodes=(user, search, ds, llm, judge),
        edges=(
            ArchEdge("user", "search", "prepared query"),
            ArchEdge("search", "ds", "query"),
            ArchEdge("ds", "search", "extractive segments (page numbers)"),
            ArchEdge("search", "llm", "segments (+ summary)"),
            ArchEdge("llm", "judge", "answer + segments"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


_DESCRIPTORS: tuple[ArchitectureDescriptor, ...] = (_level1(), _level2(), _level3(), _level4(), _managed())
_BY_ID: dict[str, ArchitectureDescriptor] = {d.level_id: d for d in _DESCRIPTORS}


def list_architecture_descriptors(settings: Any | None = None) -> tuple[ArchitectureDescriptor, ...]:
    """All five descriptors in delivery order (spec §3); regions resolved from ``settings`` when given."""
    if settings is None:
        return _DESCRIPTORS
    return tuple(d.resolved(settings) for d in _DESCRIPTORS)


def get_architecture_descriptor(level_id: str, settings: Any | None = None) -> ArchitectureDescriptor:
    """Return the descriptor for ``level_id`` (regions resolved from ``settings`` when given) or raise ``KeyError``."""
    if not isinstance(level_id, str) or level_id not in _BY_ID:
        raise KeyError(f"No architecture descriptor for level '{level_id}'")
    desc = _BY_ID[level_id]
    if settings is None:
        return desc
    return desc.resolved(settings)


__all__ = [
    "ArchEdge",
    "ArchNode",
    "ArchitectureDescriptor",
    "CANONICAL_INGEST_STAGES",
    "CANONICAL_QUERY_STAGES",
    "ALL_INGEST_STAGES",
    "MANAGED_INGEST_STAGES",
    "MANAGED_SKIPPED_INGEST_STAGES",
    "ingest_stages_for",
    "JUDGE_LOCATION_PLACEHOLDER",
    "JUDGE_SERVICE_PLACEHOLDER",
    "get_architecture_descriptor",
    "list_architecture_descriptors",
]
