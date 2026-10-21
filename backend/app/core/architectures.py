"""ArchitectureDescriptor design metadata for all five levels (spec §7.2.1).

Descriptors describe which GCP services exist in a level and how they connect. They are pure design
metadata: no timings, scores, or results. All dynamic state on the diagram comes from SSE stage events
keyed by the canonical ``stage_ids`` below.
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

    def __post_init__(self) -> None:
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
    ) -> "ArchitectureDescriptor":
        """Resolve region + judge placeholders from settings (regions/judge model are never hardcoded)."""
        if not gcp_location or not vertex_genai_location:
            raise ValueError("gcp_location and vertex_genai_location must be non-empty")
        mapping = {GCP_LOCATION_PLACEHOLDER: gcp_location, GENAI_LOCATION_PLACEHOLDER: vertex_genai_location}
        if judge_location:
            mapping[JUDGE_LOCATION_PLACEHOLDER] = judge_location
        services = {JUDGE_SERVICE_PLACEHOLDER: f"Vertex AI · {judge_model}"} if judge_model else {}

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
        )


GCP_LOCATION_PLACEHOLDER = "{GCP_LOCATION}"
GENAI_LOCATION_PLACEHOLDER = "{VERTEX_GENAI_LOCATION}"
JUDGE_LOCATION_PLACEHOLDER = "{JUDGE_LOCATION}"
JUDGE_SERVICE_PLACEHOLDER = "Vertex AI · {JUDGE_MODEL}"
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


def _bq_ingest(parser_label: str, parser_service: str, embed_col: str, extra_index: str) -> tuple[
    tuple[ArchNode, ...], tuple[ArchEdge, ...]
]:
    """Shared BigQuery ingestion sub-diagram: corpus → parser → chunker → embedder → BigQuery → indexes."""
    nodes = (
        ArchNode("gcs", "Procurement corpus", "Cloud Storage", "store", 0, 0, ("discover_docs",)),
        ArchNode("parser", parser_label, parser_service, "process", 1, 0, ("parse",)),
        ArchNode("chunker", "Chunker", "Ingestion job", "process", 2, 0, ("chunk",)),
        ArchNode("embedder", "Chunk embeddings", "Vertex AI · gemini-embedding-2", "model", 3, 0, ("embed",)),
        ArchNode("bq_load", "document_chunks", "BigQuery", "store", 4, 0, ("load_bigquery", "verify_counts")),
        ArchNode("index", "Vector" + extra_index + " index", "BigQuery", "store", 5, 0, ("build_indexes",)),
    )
    edges = (
        ArchEdge("gcs", "parser", "PDF bytes"),
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


def _level2() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _bq_ingest("PDF text parser", "PyMuPDF", "embedding", " + search")
    bq = ArchNode("bq", "document_chunks", "BigQuery VECTOR_SEARCH + SEARCH()", "store", 2, 1, ("retrieve",))
    rank = ArchNode("rank", "Cross-encoder rerank", "Discovery Engine Ranking API", "api", 3, 1, ("rerank",))
    return ArchitectureDescriptor(
        level_id="level2_hybrid",
        nodes=(_USER, _EMBED, bq, rank, _LLM, _JUDGE),
        edges=(
            ArchEdge("user", "embed", "text"),
            ArchEdge("embed", "bq", "3072-d vector"),
            ArchEdge("user", "bq", "lexical query"),
            ArchEdge("bq", "rank", "RRF candidates"),
            ArchEdge("rank", "llm", "reranked chunks"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


def _level3() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _bq_ingest(
        "Layout parser + vision", "Document AI Layout Parser · gemini-4-argon vision", "embedding_multimodal", ""
    )
    bq = ArchNode(
        "bq", "document_chunks (multimodal)", "BigQuery VECTOR_SEARCH + SEARCH()", "store", 2, 1, ("retrieve",)
    )
    rank = ArchNode("rank", "Cross-encoder rerank", "Discovery Engine Ranking API", "api", 3, 1, ("rerank",))
    return ArchitectureDescriptor(
        level_id="level3_multimodal",
        nodes=(_USER, _EMBED, bq, rank, _LLM, _JUDGE),
        edges=(
            ArchEdge("user", "embed", "text"),
            ArchEdge("embed", "bq", "multimodal vector"),
            ArchEdge("bq", "rank", "pre-filtered candidates"),
            ArchEdge("rank", "llm", "authority-headed chunks"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


def _level4() -> ArchitectureDescriptor:
    ingest_nodes, ingest_edges = _bq_ingest(
        "Layout parser + vision", "Document AI Layout Parser · gemini-4-argon vision", "embedding_multimodal", ""
    )
    agent = ArchNode(
        "agent", "ADK agent loop", "Google ADK Runner · gemini-4-argon", "agent", 1, 1, ("query_prep", "agent_turn")
    )
    tools = ArchNode(
        "tools",
        "Tools: search · amendments · pricing · verify",
        "BigQuery + Level 3 retrieval",
        "api",
        2,
        1,
        ("agent_turn",),
    )
    embed = ArchNode("embed", "Query embedding", "Vertex AI · gemini-embedding-2", "model", 2, 0, ("embed_query",))
    bq = ArchNode("bq", "document_chunks", "BigQuery VECTOR_SEARCH + SEARCH()", "store", 3, 1, ("retrieve",))
    rank = ArchNode("rank", "Cross-encoder rerank", "Discovery Engine Ranking API", "api", 3, 0, ("rerank",))
    llm = ArchNode("llm", "Final response", "Google ADK · gemini-4-argon", "model", 4, 1, ("synthesize",))
    user = ArchNode("user", "User question", "Browser", "actor", 0, 1, ())
    return ArchitectureDescriptor(
        level_id="level4_agentic",
        nodes=(user, agent, tools, embed, bq, rank, llm, _JUDGE),
        edges=(
            ArchEdge("user", "agent", "text"),
            ArchEdge("agent", "tools", "tool call"),
            ArchEdge("tools", "embed", "query"),
            ArchEdge("embed", "bq", "vector"),
            ArchEdge("tools", "bq", "amends_section lookup"),
            ArchEdge("bq", "rank", "candidates"),
            ArchEdge("rank", "tools", "chunks"),
            ArchEdge("tools", "agent", "tool result"),
            ArchEdge("agent", "llm", "final turn"),
            ArchEdge("llm", "judge", "answer + context"),
        ),
        ingest_nodes=ingest_nodes,
        ingest_edges=ingest_edges,
    )


def _managed() -> ArchitectureDescriptor:
    ds = ArchNode("ds", "Discovery Engine data store", "Vertex AI Search", "store", 1, 1, ("retrieve",))
    summary = ArchNode("summary", "Search summary", "Vertex AI Search summary", "model", 2, 1, ("synthesize",))
    judge = ArchNode(
        "judge", "LLM-as-a-judge", JUDGE_SERVICE_PLACEHOLDER, "model", 3, 1, ("evaluate",),
        region=JUDGE_LOCATION_PLACEHOLDER, code_ref=_JUDGE_CODE_REF,
    )
    ingest_nodes = (
        ArchNode("gcs", "Procurement corpus", "Cloud Storage", "store", 0, 0, ("discover_docs",)),
        ArchNode(
            "import", "Managed import", "Vertex AI Search data store", "process", 1, 0,
            ("parse", "chunk", "embed", "load_bigquery", "build_indexes"),
        ),
        ArchNode("verify", "Document count", "Vertex AI Search", "store", 2, 0, ("verify_counts",)),
    )
    ingest_edges = (ArchEdge("gcs", "import", "GCS URIs"), ArchEdge("import", "verify", "imported docs"))
    return ArchitectureDescriptor(
        level_id="managed_vertex",
        nodes=(_USER, ds, summary, judge),
        edges=(
            ArchEdge("user", "ds", "search request"),
            ArchEdge("ds", "summary", "extractive segments"),
            ArchEdge("summary", "judge", "answer + segments"),
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
    "JUDGE_LOCATION_PLACEHOLDER",
    "JUDGE_SERVICE_PLACEHOLDER",
    "get_architecture_descriptor",
    "list_architecture_descriptors",
]
