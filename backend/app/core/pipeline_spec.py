"""Canonical ``PipelineSpec`` (``IndexProfile`` + ``QuerySlots``), validator, presets, and dynamic descriptors.

Release 1.3.0 introduces composable RAG pipelines: Levels 1–6 remain exact 1-click Presets (preserving 100% of
existing Replay recordings), while users can mix-and-match index profiles, query transforms, retrieval modes,
rerankers, context ordering, and single-pass vs ADK agentic reasoning in the Workbench and Ablation ``/compare``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Mapping

from app.core.architectures import (
    DOCAI_LOCATION_PLACEHOLDER,
    GCP_LOCATION_PLACEHOLDER,
    GENAI_LOCATION_PLACEHOLDER,
    JUDGE_LOCATION_PLACEHOLDER,
    JUDGE_SERVICE_PLACEHOLDER,
    MM_EMBED_SERVICE_PLACEHOLDER,
    RAG_ENGINE_LOCATION_PLACEHOLDER,
    RAG_ENGINE_STORE_SERVICE_PLACEHOLDER,
    VAIS_LOCATION_PLACEHOLDER,
    VVS2_LOCATION_PLACEHOLDER,
    ArchEdge,
    ArchNode,
    ArchitectureDescriptor,
    get_architecture_descriptor,
)
from app.core.config import ALL_LEVEL_IDS, ArenaSettings
from app.core.models_catalog import (
    PROVIDER_VERTEX_GEMINI,
    RAG_ENGINE_GROUNDED_MODEL_ID,
    VAIS_SUMMARY_MODEL_ID,
    RunModels,
)

INDEX_PROFILES: tuple[str, ...] = (
    "naive",
    "hybrid",
    "multimodal",
    "vertex_ai_search",
    "rag_engine",
    "alloydb_scann",
    "vertex_vector_v2",
)
BQ_INDEX_PROFILES: tuple[str, ...] = ("naive", "hybrid", "multimodal")
MANAGED_INDEX_PROFILES: tuple[str, ...] = ("vertex_ai_search", "rag_engine")
SCANN_INDEX_PROFILES: tuple[str, ...] = ("alloydb_scann", "vertex_vector_v2")
BACKING_LEVEL_BY_PROFILE: dict[str, str] = {
    "naive": "level1_naive",
    "hybrid": "level2_hybrid",
    "multimodal": "level3_multimodal",
    "vertex_ai_search": "managed_vertex",
    "rag_engine": "rag_engine",
    "alloydb_scann": "alloydb_scann",
    "vertex_vector_v2": "vertex_vector_v2",
}
PROFILE_LABELS: dict[str, str] = {
    "naive": "BigQuery · Naive chunks",
    "hybrid": "BigQuery · Lineage chunks",
    "multimodal": "BigQuery · DocAI + Vision multimodal",
    "vertex_ai_search": "Vertex AI Search · Extractive segments",
    "rag_engine": "Vertex AI RAG Engine · RagCorpus",
    "alloydb_scann": "AlloyDB PostgreSQL · ScaNN + GIN FTS",
    "vertex_vector_v2": "Vector Search 2.0 · ScaNN + Sparse",
}
MAX_CANDIDATE_K_BY_PROFILE: dict[str, int] = {
    "naive": 100,
    "hybrid": 100,
    "multimodal": 100,
    "rag_engine": 50,
    "vertex_ai_search": 20,
    "alloydb_scann": 100,
    "vertex_vector_v2": 100,
}

QUERY_TRANSFORMS: tuple[str, ...] = ("none", "acronym", "acronym_plus_intent")
RETRIEVAL_MODES: tuple[str, ...] = ("vector_cosine", "lexical_bm25", "hybrid_rrf", "managed_retrieval")
BQ_RETRIEVAL_MODES: tuple[str, ...] = ("vector_cosine", "lexical_bm25", "hybrid_rrf")
RERANKERS: tuple[str, ...] = ("none", "vertex_ranking_api")
CONTEXT_ORDERS: tuple[str, ...] = ("score_order", "authority_amendment_first")
REASONING_MODES: tuple[str, ...] = ("single_pass", "adk_agent")
CANONICAL_AGENT_TOOLS: tuple[str, ...] = (
    "search_procurement_corpus",
    "check_amendments",
    "calculate_contract_pricing",
    "verify_citations",
)
PRESET_LEVEL_IDS: tuple[str, ...] = ALL_LEVEL_IDS
TOP_K_RANGE: tuple[int, int] = (1, 20)
_SLOT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
_MAX_LABEL_CHARS = 80


class PipelineSpecError(ValueError):
    """Raised when a ``PipelineSpec`` or slot value violates domain invariants."""


def _require_choice(value: Any, allowed: tuple[str, ...], field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PipelineSpecError(f"Field '{field_name}' must be one of {list(allowed)}, got {value!r}")
    return value


def _require_bounded_int(value: Any, lo: int, hi: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise PipelineSpecError(f"Field '{field_name}' must be an integer in [{lo}, {hi}], got {value!r}")
    return value


def _canonicalize_agent_tools(raw_tools: Any, reasoning_mode: str) -> tuple[str, ...]:
    if raw_tools is None:
        items: list[Any] = []
    elif isinstance(raw_tools, (list, tuple)):
        items = list(raw_tools)
    else:
        raise PipelineSpecError("Field 'agent_tools' must be a list or tuple of tool name strings")
    if reasoning_mode == "single_pass":
        if items:
            raise PipelineSpecError("agent_tools must be empty when reasoning_mode is 'single_pass'")
        return ()
    if len(items) > len(CANONICAL_AGENT_TOOLS) or not items:
        raise PipelineSpecError(
            f"reasoning_mode 'adk_agent' requires 1..{len(CANONICAL_AGENT_TOOLS)} agent_tools"
        )
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or item not in CANONICAL_AGENT_TOOLS:
            raise PipelineSpecError(f"Unknown agent tool {item!r}; allowed: {list(CANONICAL_AGENT_TOOLS)}")
        if item in seen:
            raise PipelineSpecError(f"Duplicate agent tool {item!r}")
        seen.add(item)
    if "search_procurement_corpus" not in seen:
        raise PipelineSpecError("reasoning_mode 'adk_agent' requires 'search_procurement_corpus' in agent_tools")
    return tuple(tool for tool in CANONICAL_AGENT_TOOLS if tool in seen)


def validate_slot_id(raw: Any) -> str:
    """Validate a caller-supplied recipe ``slot_id`` against ``^[a-zA-Z0-9_-]{1,48}$``."""
    if not isinstance(raw, str) or not _SLOT_ID_RE.fullmatch(raw):
        raise PipelineSpecError("Field 'slot_id' must match ^[a-zA-Z0-9_-]{1,48}$")
    return raw


@dataclass(frozen=True)
class QuerySlots:
    """Composable query-time slots layered over an ``index_profile``."""

    query_transform: str
    retrieval_mode: str
    candidate_k: int
    top_k: int
    reranker: str
    context_order: str
    reasoning_mode: str
    agent_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_choice(self.query_transform, QUERY_TRANSFORMS, "query_transform")
        _require_choice(self.retrieval_mode, RETRIEVAL_MODES, "retrieval_mode")
        _require_bounded_int(self.top_k, TOP_K_RANGE[0], TOP_K_RANGE[1], "top_k")
        _require_bounded_int(self.candidate_k, 1, 100, "candidate_k")
        if self.candidate_k < self.top_k:
            raise PipelineSpecError(
                f"candidate_k ({self.candidate_k}) must be >= top_k ({self.top_k})"
            )
        _require_choice(self.reranker, RERANKERS, "reranker")
        _require_choice(self.context_order, CONTEXT_ORDERS, "context_order")
        _require_choice(self.reasoning_mode, REASONING_MODES, "reasoning_mode")
        if not isinstance(self.agent_tools, tuple):
            raise PipelineSpecError("QuerySlots.agent_tools must be a tuple of strings")
        canonical = _canonicalize_agent_tools(self.agent_tools, self.reasoning_mode)
        if self.agent_tools != canonical:
            object.__setattr__(self, "agent_tools", canonical)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_transform": self.query_transform,
            "retrieval_mode": self.retrieval_mode,
            "candidate_k": self.candidate_k,
            "top_k": self.top_k,
            "reranker": self.reranker,
            "context_order": self.context_order,
            "reasoning_mode": self.reasoning_mode,
            "agent_tools": list(self.agent_tools),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuerySlots":
        if not isinstance(data, Mapping):
            raise PipelineSpecError("Field 'slots' must be a JSON object")
        tools_raw = data.get("agent_tools")
        reasoning = _require_choice(data.get("reasoning_mode"), REASONING_MODES, "reasoning_mode")
        tools = _canonicalize_agent_tools(tools_raw, reasoning)
        return cls(
            query_transform=data.get("query_transform"),  # type: ignore[arg-type]
            retrieval_mode=data.get("retrieval_mode"),  # type: ignore[arg-type]
            candidate_k=data.get("candidate_k"),  # type: ignore[arg-type]
            top_k=data.get("top_k"),  # type: ignore[arg-type]
            reranker=data.get("reranker"),  # type: ignore[arg-type]
            context_order=data.get("context_order"),  # type: ignore[arg-type]
            reasoning_mode=reasoning,
            agent_tools=tools,
        )


def _check_cross_slot_invariants(index_profile: str, slots: QuerySlots) -> None:
    max_cand = MAX_CANDIDATE_K_BY_PROFILE[index_profile]
    if slots.candidate_k > max_cand:
        raise PipelineSpecError(
            f"candidate_k ({slots.candidate_k}) exceeds max {max_cand} for index_profile '{index_profile}'"
        )
    if index_profile in BQ_INDEX_PROFILES and slots.retrieval_mode not in BQ_RETRIEVAL_MODES:
        raise PipelineSpecError(
            f"BigQuery index_profile '{index_profile}' requires retrieval_mode in {list(BQ_RETRIEVAL_MODES)}, "
            f"got '{slots.retrieval_mode}'"
        )
    if index_profile in MANAGED_INDEX_PROFILES and slots.retrieval_mode != "managed_retrieval":
        raise PipelineSpecError(
            f"Managed index_profile '{index_profile}' requires retrieval_mode 'managed_retrieval', "
            f"got '{slots.retrieval_mode}'"
        )
    if index_profile in SCANN_INDEX_PROFILES and slots.retrieval_mode != "hybrid_rrf":
        raise PipelineSpecError(
            f"ScaNN index_profile '{index_profile}' requires retrieval_mode 'hybrid_rrf', "
            f"got '{slots.retrieval_mode}'"
        )
    if slots.reasoning_mode == "adk_agent" and index_profile not in BQ_INDEX_PROFILES:
        raise PipelineSpecError(
            f"reasoning_mode 'adk_agent' requires a BigQuery index_profile {list(BQ_INDEX_PROFILES)}, "
            f"got '{index_profile}'"
        )


def _check_model_id_compatibility(spec: "PipelineSpec", synth_id: str, judge_id: str | None = None) -> None:
    if judge_id and synth_id == judge_id:
        raise PipelineSpecError(f"judge_model must differ from synthesis_model (both '{synth_id}')")
    if synth_id == VAIS_SUMMARY_MODEL_ID:
        if spec.index_profile != "vertex_ai_search":
            raise PipelineSpecError(
                f"Model '{VAIS_SUMMARY_MODEL_ID}' requires index_profile 'vertex_ai_search'"
            )
        if spec.slots.reranker != "none" or spec.slots.reasoning_mode != "single_pass":
            raise PipelineSpecError(
                f"Model '{VAIS_SUMMARY_MODEL_ID}' generates inside the managed search call and requires "
                "reranker='none' and reasoning_mode='single_pass'"
            )
    if synth_id == RAG_ENGINE_GROUNDED_MODEL_ID:
        if spec.index_profile != "rag_engine":
            raise PipelineSpecError(f"Model '{RAG_ENGINE_GROUNDED_MODEL_ID}' requires index_profile 'rag_engine'")
        if spec.slots.reranker != "none" or spec.slots.reasoning_mode != "single_pass":
            raise PipelineSpecError(
                f"Model '{RAG_ENGINE_GROUNDED_MODEL_ID}' retrieves inside VertexRagStore and requires "
                "reranker='none' and reasoning_mode='single_pass'"
            )


@dataclass(frozen=True)
class PipelineSpec:
    """Validated, immutable RAG pipeline configuration."""

    index_profile: str
    slots: QuerySlots
    synthesis_model: str | None = None
    judge_model: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        _require_choice(self.index_profile, INDEX_PROFILES, "index_profile")
        if not isinstance(self.slots, QuerySlots):
            raise PipelineSpecError("Field 'slots' must be a QuerySlots instance")
        _check_cross_slot_invariants(self.index_profile, self.slots)
        for field_name, val in (("synthesis_model", self.synthesis_model), ("judge_model", self.judge_model)):
            if val is not None and (not isinstance(val, str) or not val.strip() or len(val.strip()) > 128):
                raise PipelineSpecError(f"Field '{field_name}' must be a non-empty model id string or null")
        if not isinstance(self.label, str) or len(self.label.strip()) > _MAX_LABEL_CHARS:
            raise PipelineSpecError(f"Field 'label' must be a string of at most {_MAX_LABEL_CHARS} chars")
        if self.synthesis_model is not None:
            object.__setattr__(self, "synthesis_model", self.synthesis_model.strip())
        if self.judge_model is not None:
            object.__setattr__(self, "judge_model", self.judge_model.strip())
        object.__setattr__(self, "label", self.label.strip())
        if self.synthesis_model:
            _check_model_id_compatibility(self, self.synthesis_model, self.judge_model)

    @property
    def backing_level_id(self) -> str:
        """Ingest level whose indexed corpus/workspace data backs this pipeline."""
        return BACKING_LEVEL_BY_PROFILE[self.index_profile]

    def structural_dict(self) -> dict[str, Any]:
        return {"index_profile": self.index_profile, "slots": self.slots.to_dict()}

    @property
    def pipeline_id(self) -> str:
        """Deterministic ``pipe_<12hex>`` identifier over canonical structural JSON."""
        canonical = json.dumps(self.structural_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "pipe_" + hashlib.sha256(canonical).hexdigest()[:12]

    def preset_level_id(self, settings: ArenaSettings | None = None) -> str | None:
        """Return the matching ``ALL_LEVEL_IDS`` preset id when structural slots match 1:1, else ``None``."""
        cfg = settings or ArenaSettings()
        for level_id in PRESET_LEVEL_IDS:
            preset = preset_spec(level_id, cfg)
            if self.index_profile == preset.index_profile and self.slots == preset.slots:
                return level_id
        return None

    def summary_label(self, settings: ArenaSettings | None = None) -> str:
        """Concise human-readable recipe label for UI headers, rails, and scoreboards."""
        if self.label:
            return self.label
        preset_id = self.preset_level_id(settings)
        if preset_id:
            return f"Preset · {preset_id}"
        parts = [self.index_profile, self.slots.retrieval_mode, f"k={self.slots.top_k}"]
        if self.slots.reranker != "none":
            parts.append("rerank")
        if self.slots.reasoning_mode == "adk_agent":
            parts.append(f"adk({len(self.slots.agent_tools)}t)")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pipeline_id": self.pipeline_id,
            "index_profile": self.index_profile,
            "slots": self.slots.to_dict(),
        }
        if self.synthesis_model is not None:
            out["synthesis_model"] = self.synthesis_model
        if self.judge_model is not None:
            out["judge_model"] = self.judge_model
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PipelineSpec":
        if not isinstance(data, Mapping):
            raise PipelineSpecError("PipelineSpec payload must be a JSON object")
        profile = _require_choice(data.get("index_profile"), INDEX_PROFILES, "index_profile")
        slots = QuerySlots.from_dict(data.get("slots"))  # type: ignore[arg-type]
        return cls(
            index_profile=profile,
            slots=slots,
            synthesis_model=data.get("synthesis_model"),
            judge_model=data.get("judge_model"),
            label=str(data.get("label") or ""),
        )


def validate_pipeline_models(spec: PipelineSpec, run_models: RunModels) -> None:
    """Validate resolved ``RunModels`` against ``spec`` invariants; raise ``PipelineSpecError`` on conflict."""
    _check_model_id_compatibility(spec, run_models.synthesis.id, run_models.judge.id)
    if spec.slots.reasoning_mode == "adk_agent" and run_models.synthesis.provider != PROVIDER_VERTEX_GEMINI:
        raise PipelineSpecError(
            f"reasoning_mode 'adk_agent' requires a '{PROVIDER_VERTEX_GEMINI}' synthesis model "
            f"(got '{run_models.synthesis.id}' with provider '{run_models.synthesis.provider}')"
        )


def preset_spec(level_id: str, settings: ArenaSettings | None = None) -> PipelineSpec:
    """Return the canonical ``PipelineSpec`` corresponding to one of ``ALL_LEVEL_IDS``."""
    cfg = settings or ArenaSettings()
    if level_id == "level1_naive":
        return PipelineSpec("naive", QuerySlots("none", "vector_cosine", 5, 5, "none", "score_order", "single_pass"))
    if level_id == "level2_hybrid":
        return PipelineSpec(
            "hybrid",
            QuerySlots(
                "acronym", "hybrid_rrf", cfg.hybrid_candidate_k, cfg.rerank_top_n,
                "vertex_ranking_api", "score_order", "single_pass",
            ),
        )
    if level_id == "level3_multimodal":
        return PipelineSpec(
            "multimodal",
            QuerySlots(
                "acronym_plus_intent", "hybrid_rrf", cfg.hybrid_candidate_k, cfg.rerank_top_n,
                "vertex_ranking_api", "authority_amendment_first", "single_pass",
            ),
        )
    if level_id == "level4_agentic":
        return PipelineSpec(
            "multimodal",
            QuerySlots(
                "acronym_plus_intent", "hybrid_rrf", cfg.hybrid_candidate_k, cfg.rerank_top_n,
                "vertex_ranking_api", "authority_amendment_first", "adk_agent", CANONICAL_AGENT_TOOLS,
            ),
        )
    if level_id == "managed_vertex":
        return PipelineSpec(
            "vertex_ai_search",
            QuerySlots(
                "acronym", "managed_retrieval", cfg.vais_page_size, cfg.vais_summary_result_count,
                "none", "score_order", "single_pass",
            ),
        )
    if level_id == "rag_engine":
        return PipelineSpec(
            "rag_engine",
            QuerySlots(
                "acronym", "managed_retrieval", cfg.rag_engine_top_k, cfg.rag_engine_top_k,
                "none", "score_order", "single_pass",
            ),
        )
    if level_id == "alloydb_scann":
        return PipelineSpec(
            "alloydb_scann",
            QuerySlots(
                "acronym", "hybrid_rrf", cfg.alloydb_candidate_k, cfg.rerank_top_n,
                "vertex_ranking_api", "score_order", "single_pass",
            ),
        )
    if level_id == "vertex_vector_v2":
        return PipelineSpec(
            "vertex_vector_v2",
            QuerySlots(
                "acronym", "hybrid_rrf", cfg.vvs2_top_k, cfg.rerank_top_n,
                "vertex_ranking_api", "score_order", "single_pass",
            ),
        )
    raise PipelineSpecError(f"Unknown preset level_id {level_id!r}; expected one of {list(PRESET_LEVEL_IDS)}")


def list_preset_specs(settings: ArenaSettings | None = None) -> tuple[tuple[str, PipelineSpec], ...]:
    """Return ``(level_id, PipelineSpec)`` pairs in delivery order."""
    cfg = settings or ArenaSettings()
    return tuple((lid, preset_spec(lid, cfg)) for lid in PRESET_LEVEL_IDS)


def active_query_stages(spec: PipelineSpec) -> tuple[str, ...]:
    """Ordered subset of ``CANONICAL_QUERY_STAGES`` emitted by ``spec``."""
    if spec.slots.reasoning_mode == "adk_agent":
        return ("query_prep", "agent_turn", "synthesize", "evaluate")
    stages: list[str] = ["query_prep"]
    if (spec.index_profile in BQ_INDEX_PROFILES or spec.index_profile in SCANN_INDEX_PROFILES) and spec.slots.retrieval_mode in ("vector_cosine", "hybrid_rrf"):
        stages.append("embed_query")
    stages.append("retrieve")
    if spec.slots.reranker == "vertex_ranking_api":
        stages.append("rerank")
    stages.extend(["synthesize", "evaluate"])
    return tuple(stages)


# --------------------------------------------------------------------------- dynamic ArchitectureDescriptor builder


def _user_node(transform: str) -> ArchNode:
    labels = {
        "none": ("User question", "Browser"),
        "acronym": ("User question + acronym expansion", "Engine (local)"),
        "acronym_plus_intent": ("User question + element intent", "Engine (local)"),
    }
    label, service = labels[transform]
    return ArchNode("user", label, service, "actor", 0, 1, ("query_prep",), code_ref="app.engines.composable:ComposableRAGEngine")


def _bq_service_label(spec: PipelineSpec) -> str:
    mode = spec.slots.retrieval_mode
    k = spec.slots.candidate_k
    if mode == "vector_cosine":
        return f"BigQuery VECTOR_SEARCH (top-{k})"
    if mode == "lexical_bm25":
        return f"BigQuery SEARCH() + lexical rank (top-{k})"
    return f"BigQuery VECTOR_SEARCH + SEARCH() → RRF (top-{k})"


def _single_pass_retrieval(spec: PipelineSpec) -> tuple[list[ArchNode], list[ArchEdge], str]:
    nodes: list[ArchNode] = []
    edges: list[ArchEdge] = []
    if spec.index_profile in BQ_INDEX_PROFILES:
        bq = ArchNode(
            "bq", f"document_chunks (tier {spec.index_profile})", _bq_service_label(spec),
            "store", 2, 1, ("retrieve",), region=GCP_LOCATION_PLACEHOLDER,
        )
        if spec.slots.retrieval_mode in ("vector_cosine", "hybrid_rrf"):
            emb_svc = MM_EMBED_SERVICE_PLACEHOLDER if spec.index_profile == "multimodal" else "Vertex AI · gemini-embedding-2"
            embed = ArchNode("embed", "Query embedding", emb_svc, "model", 1, 0, ("embed_query",), region=GENAI_LOCATION_PLACEHOLDER)
            nodes.append(embed)
            edges.extend([ArchEdge("user", "embed", "prepared query"), ArchEdge("embed", "bq", "3072-d vector")])
        if spec.slots.retrieval_mode in ("lexical_bm25", "hybrid_rrf"):
            edges.append(ArchEdge("user", "bq", "lexical terms"))
        nodes.append(bq)
        return nodes, edges, "bq"
    if spec.index_profile == "alloydb_scann":
        embed = ArchNode("embed", "Query embedding", "Vertex AI · gemini-embedding-2", "model", 1, 0, ("embed_query",), region=GENAI_LOCATION_PLACEHOLDER)
        alloydb = ArchNode(
            "alloydb", "AlloyDB PostgreSQL + ScaNN",
            f"alloydb_scann cosine + GIN FTS → RRF (top-{spec.slots.candidate_k})",
            "store", 2, 1, ("retrieve",), region=GCP_LOCATION_PLACEHOLDER,
        )
        nodes.extend([embed, alloydb])
        edges.extend([ArchEdge("user", "embed", "prepared query"), ArchEdge("embed", "alloydb", "3072-d vector"), ArchEdge("user", "alloydb", "websearch tsquery")])
        return nodes, edges, "alloydb"
    if spec.index_profile == "vertex_vector_v2":
        embed = ArchNode("embed", "Query embedding", "Vertex AI · gemini-embedding-2", "model", 1, 0, ("embed_query",), region=GENAI_LOCATION_PLACEHOLDER)
        vvs2 = ArchNode(
            "vvs2", "Vector Search 2.0 Collection",
            f"dataObjects:batchSearch · ScaNN + Sparse RRF (top-{spec.slots.candidate_k})",
            "store", 2, 1, ("retrieve",), region=VVS2_LOCATION_PLACEHOLDER,
        )
        nodes.extend([embed, vvs2])
        edges.extend([ArchEdge("user", "embed", "prepared query"), ArchEdge("embed", "vvs2", "3072-d vector"), ArchEdge("user", "vvs2", "CRC32 sparse tokens")])
        return nodes, edges, "vvs2"
    if spec.index_profile == "vertex_ai_search":
        search = ArchNode("search", "Search app", "Vertex AI Search · :search", "api", 1, 1, ("retrieve",), region=VAIS_LOCATION_PLACEHOLDER)
        ds = ArchNode("ds", "Data store", "Vertex AI Search data store", "store", 2, 0, ("retrieve",), region=VAIS_LOCATION_PLACEHOLDER)
        nodes.extend([search, ds])
        edges.extend([ArchEdge("user", "search", "prepared query"), ArchEdge("search", "ds", "query"), ArchEdge("ds", "search", "extractive segments")])
        return nodes, edges, "search"
    retrieve = ArchNode("retrieve", "RetrieveContexts API", "Vertex AI RAG Engine · :retrieveContexts", "api", 1, 1, ("retrieve",), region=RAG_ENGINE_LOCATION_PLACEHOLDER)
    corpus = ArchNode("rag_corpus", "RagCorpus", RAG_ENGINE_STORE_SERVICE_PLACEHOLDER, "store", 2, 0, ("retrieve",), region=RAG_ENGINE_LOCATION_PLACEHOLDER)
    nodes.extend([retrieve, corpus])
    edges.extend([ArchEdge("user", "retrieve", "prepared query"), ArchEdge("retrieve", "rag_corpus", f"KNN (top-{spec.slots.candidate_k})"), ArchEdge("rag_corpus", "retrieve", "chunks + pageSpan")])
    return nodes, edges, "retrieve"


def _agent_nodes_and_edges(spec: PipelineSpec) -> tuple[tuple[ArchNode, ...], tuple[ArchEdge, ...]]:
    turn = ("agent_turn",)
    tools = set(spec.slots.agent_tools)
    user = _user_node(spec.slots.query_transform)
    agent = ArchNode("agent", "ADK LlmAgent loop", "Google ADK InMemoryRunner · Vertex AI Gemini", "agent", 1, 1, turn, region=GENAI_LOCATION_PLACEHOLDER)
    nodes: list[ArchNode] = [user, agent]
    edges: list[ArchEdge] = [ArchEdge("user", "agent", "prepared query")]

    bq_tools = ("search_procurement_corpus",) + (("check_amendments",) if "check_amendments" in tools else ())
    tool_search = ArchNode("tool_search", "search_procurement_corpus", f"{spec.index_profile} · {spec.slots.retrieval_mode}", "process", 2, 0, turn, tool_names=("search_procurement_corpus",))
    bq = ArchNode("bq", f"document_chunks (tier {spec.index_profile})", _bq_service_label(spec), "store", 3, 1, turn, region=GCP_LOCATION_PLACEHOLDER, tool_names=bq_tools)
    nodes.extend([tool_search, bq])
    edges.extend([ArchEdge("agent", "tool_search", "tool call"), ArchEdge("tool_search", "agent", "chunks")])

    if spec.slots.retrieval_mode in ("vector_cosine", "hybrid_rrf"):
        emb_svc = MM_EMBED_SERVICE_PLACEHOLDER if spec.index_profile == "multimodal" else "Vertex AI · gemini-embedding-2"
        nodes.append(ArchNode("embed", "Query embedding", emb_svc, "model", 3, 0, turn, region=GENAI_LOCATION_PLACEHOLDER, tool_names=("search_procurement_corpus",)))
        edges.extend([ArchEdge("tool_search", "embed", "query"), ArchEdge("embed", "bq", "3072-d vector")])
    else:
        edges.append(ArchEdge("tool_search", "bq", "lexical terms"))

    if spec.slots.reranker == "vertex_ranking_api":
        nodes.append(ArchNode("rank", "Semantic rerank", "Discovery Engine Ranking API", "api", 4, 0, turn, region="global", tool_names=("search_procurement_corpus",)))
        edges.extend([ArchEdge("bq", "rank", f"candidates (≤ {spec.slots.candidate_k})"), ArchEdge("rank", "tool_search", f"top-{spec.slots.top_k} chunks")])
    else:
        edges.append(ArchEdge("bq", "tool_search", f"top-{spec.slots.top_k} chunks"))

    if "check_amendments" in tools:
        nodes.append(ArchNode("tool_amend", "check_amendments", "BigQuery amends_section lookup", "process", 2, 1, turn, tool_names=("check_amendments",)))
        edges.extend([ArchEdge("agent", "tool_amend", "tool call"), ArchEdge("tool_amend", "bq", "@section_id"), ArchEdge("tool_amend", "agent", "superseding text")])
    if "calculate_contract_pricing" in tools:
        nodes.append(ArchNode("tool_calc", "calculate_contract_pricing", "Local AST-validated arithmetic", "process", 2, 2, turn, tool_names=("calculate_contract_pricing",)))
        edges.extend([ArchEdge("agent", "tool_calc", "tool call"), ArchEdge("tool_calc", "agent", "value")])
    if "verify_citations" in tools:
        nodes.append(ArchNode("tool_verify", "verify_citations", "Local claim ↔ cited-chunk check", "process", 2, 3, turn, tool_names=("verify_citations",)))
        edges.extend([ArchEdge("agent", "tool_verify", "tool call"), ArchEdge("tool_verify", "agent", "unsupported claims")])

    final = ArchNode("llm", "Final response (ADK)", "Google ADK · final model turn", "model", 4, 2, ("synthesize",), region=GENAI_LOCATION_PLACEHOLDER)
    judge = ArchNode("judge", "LLM-as-a-judge", JUDGE_SERVICE_PLACEHOLDER, "model", 5, 2, ("evaluate",), region=JUDGE_LOCATION_PLACEHOLDER)
    nodes.extend([final, judge])
    edges.extend([ArchEdge("agent", "llm", "final turn"), ArchEdge("llm", "judge", "answer + tool chunks")])
    return tuple(nodes), tuple(edges)


def descriptor_for_pipeline(spec: PipelineSpec, settings: ArenaSettings | None = None) -> ArchitectureDescriptor:
    """Return the exact preset ``ArchitectureDescriptor`` when matching a preset, else build a live slot diagram."""
    cfg = settings or ArenaSettings()
    preset_id = spec.preset_level_id(cfg)
    if preset_id is not None:
        return get_architecture_descriptor(preset_id, cfg)

    backing_raw = get_architecture_descriptor(spec.backing_level_id)
    if spec.slots.reasoning_mode == "adk_agent":
        q_nodes, q_edges = _agent_nodes_and_edges(spec)
    else:
        user = _user_node(spec.slots.query_transform)
        ret_nodes, ret_edges, ret_out = _single_pass_retrieval(spec)
        nodes_acc: list[ArchNode] = [user, *ret_nodes]
        edges_acc: list[ArchEdge] = list(ret_edges)
        pre_llm = ret_out
        if spec.slots.reranker == "vertex_ranking_api":
            nodes_acc.append(ArchNode("rank", "Semantic rerank", "Discovery Engine Ranking API", "api", 3, 1, ("rerank",), region="global"))
            edges_acc.append(ArchEdge(ret_out, "rank", f"candidates (≤ {spec.slots.candidate_k})"))
            pre_llm = "rank"
        llm_label = "Authority-headed synthesis" if spec.slots.context_order == "authority_amendment_first" else "Answer synthesis"
        ctx_edge = f"top-{spec.slots.top_k} · amendments first" if spec.slots.context_order == "authority_amendment_first" else f"top-{spec.slots.top_k} chunks"
        llm = ArchNode("llm", llm_label, f"Vertex AI · {cfg.gemini_fast_model}", "model", 4, 1, ("synthesize",), region=GENAI_LOCATION_PLACEHOLDER)
        judge = ArchNode("judge", "LLM-as-a-judge", JUDGE_SERVICE_PLACEHOLDER, "model", 5, 1, ("evaluate",), region=JUDGE_LOCATION_PLACEHOLDER)
        nodes_acc.extend([llm, judge])
        edges_acc.extend([ArchEdge(pre_llm, "llm", ctx_edge), ArchEdge("llm", "judge", "answer + context")])
        q_nodes, q_edges = tuple(nodes_acc), tuple(edges_acc)

    raw_desc = ArchitectureDescriptor(
        level_id=spec.pipeline_id,
        nodes=q_nodes,
        edges=q_edges,
        ingest_nodes=backing_raw.ingest_nodes,
        ingest_edges=backing_raw.ingest_edges,
    )
    return raw_desc.resolved(cfg)


__all__ = [
    "BACKING_LEVEL_BY_PROFILE",
    "BQ_INDEX_PROFILES",
    "BQ_RETRIEVAL_MODES",
    "CANONICAL_AGENT_TOOLS",
    "CONTEXT_ORDERS",
    "INDEX_PROFILES",
    "MANAGED_INDEX_PROFILES",
    "MAX_CANDIDATE_K_BY_PROFILE",
    "PRESET_LEVEL_IDS",
    "PROFILE_LABELS",
    "QUERY_TRANSFORMS",
    "REASONING_MODES",
    "RERANKERS",
    "RETRIEVAL_MODES",
    "SCANN_INDEX_PROFILES",
    "TOP_K_RANGE",
    "PipelineSpec",
    "PipelineSpecError",
    "QuerySlots",
    "active_query_stages",
    "descriptor_for_pipeline",
    "list_preset_specs",
    "preset_spec",
    "validate_pipeline_models",
    "validate_slot_id",
]
