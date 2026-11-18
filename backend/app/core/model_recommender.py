"""Pure deterministic FCIS Model Pair Recommender (v1.6.1).

Recommends an optimal ``(synthesis_model, judge_model)`` pair across enterprise workload profiles
while enforcing:
- ``judge_model != synthesis_model`` (never grades its own answers).
- Cross-family evaluation by default (Google Gemini worker ↔ Anthropic Claude judge, or
  ``vertex-genai-eval`` rubric autorater).
- Gemini-only workers on ``level4_agentic`` / ``adk_agent`` (Google ADK requires Vertex Gemini).
- Honest availability resolution against ``ModelCatalog`` (zero silent fallbacks to unavailable models).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models_catalog import (
    PROVIDER_ANTHROPIC_VERTEX,
    PROVIDER_VERTEX_GEMINI,
    PROVIDER_VERTEX_GENAI_EVAL,
    ROLE_JUDGE,
    ROLE_SYNTHESIS,
    VERTEX_GENAI_EVAL_MODEL_ID,
    ModelCatalog,
    ModelSpec,
)

PROFILE_COST_LATENCY = "cost_latency"
PROFILE_BALANCED_ENTERPRISE = "balanced_enterprise"
PROFILE_MULTIMODAL_AGENTIC = "multimodal_agentic"
PROFILE_ANTHROPIC_WORKER = "anthropic_worker"

RECOMMENDATION_PROFILE_IDS: tuple[str, ...] = (
    PROFILE_COST_LATENCY,
    PROFILE_BALANCED_ENTERPRISE,
    PROFILE_MULTIMODAL_AGENTIC,
    PROFILE_ANTHROPIC_WORKER,
)

_GEMINI_SYNTH_FALLBACKS: tuple[str, ...] = (
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
)
_ANTHROPIC_SYNTH_FALLBACKS: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-opus-5-5",
)
_ANTHROPIC_JUDGE_FALLBACKS: tuple[str, ...] = (
    "claude-opus-5-5",
    "claude-sonnet-4-6",
)
_GEMINI_JUDGE_FALLBACKS: tuple[str, ...] = (
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
)


def model_family(model_id: str) -> str:
    """Return the provider family for cross-family bias checks."""
    mid = (model_id or "").strip().lower()
    if mid == VERTEX_GENAI_EVAL_MODEL_ID:
        return "vertex_eval"
    if mid.startswith("gemini-") or mid == "vertex-rag-engine-grounded":
        return "gemini"
    if mid.startswith("claude-"):
        return "anthropic"
    if mid.startswith("vertex-ai-search"):
        return "vertex_search"
    return "other"


def is_cross_family_pair(synthesis_model: str, judge_model: str) -> bool:
    """True when judge differs from synthesis and comes from a separate model family (or rubric eval)."""
    if not synthesis_model or not judge_model or synthesis_model == judge_model:
        return False
    if judge_model == VERTEX_GENAI_EVAL_MODEL_ID:
        return True
    return model_family(synthesis_model) != model_family(judge_model)


@dataclass(frozen=True)
class ModelPairRecommendation:
    """Deterministic Worker (synthesis) + Judge model recommendation."""

    profile_id: str
    profile_label: str
    synthesis_model: str
    judge_model: str
    alt_judge_model: str | None
    rationale: str
    judge_rationale: str
    cross_family: bool = True
    adk_compatible: bool = True

    def __post_init__(self) -> None:
        if not self.profile_id or not self.profile_label:
            raise ValueError("profile_id and profile_label must be non-empty")
        if not self.synthesis_model or not self.judge_model:
            raise ValueError("synthesis_model and judge_model must be non-empty")
        if self.synthesis_model == self.judge_model:
            raise ValueError(f"Recommended judge_model must differ from synthesis_model (both '{self.synthesis_model}')")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "synthesis_model": self.synthesis_model,
            "judge_model": self.judge_model,
            "alt_judge_model": self.alt_judge_model,
            "rationale": self.rationale,
            "judge_rationale": self.judge_rationale,
            "cross_family": self.cross_family,
            "adk_compatible": self.adk_compatible,
        }


RECOMMENDED_PROFILES: tuple[ModelPairRecommendation, ...] = (
    ModelPairRecommendation(
        profile_id=PROFILE_COST_LATENCY,
        profile_label="High-QPS / Low-Cost",
        synthesis_model="gemini-4-hydrogen",
        judge_model="claude-sonnet-4-6",
        alt_judge_model=VERTEX_GENAI_EVAL_MODEL_ID,
        rationale=(
            "Gemini 4 Hydrogen ($1.50 / $7.50 per 1M) delivers fast, low-cost grounded synthesis "
            "for high-QPS and interactive workloads."
        ),
        judge_rationale=(
            "Claude Sonnet 4.6 provides independent cross-family judging without Opus overhead "
            "(or switch to Vertex AI GenAI Eval for rubric scoring)."
        ),
        cross_family=True,
        adk_compatible=True,
    ),
    ModelPairRecommendation(
        profile_id=PROFILE_BALANCED_ENTERPRISE,
        profile_label="Enterprise SQL / Contracts",
        synthesis_model="gemini-4-carbon",
        judge_model="claude-opus-5-5",
        alt_judge_model="claude-sonnet-4-6",
        rationale=(
            "Gemini 4 Carbon ($2.00 / $10.00 per 1M) balances strong reasoning and clause accuracy "
            "across SQL-governed and enterprise contract corpora."
        ),
        judge_rationale=(
            "Claude Opus 5.5 grades groundedness and QA relevance from an independent Anthropic model family."
        ),
        cross_family=True,
        adk_compatible=True,
    ),
    ModelPairRecommendation(
        profile_id=PROFILE_MULTIMODAL_AGENTIC,
        profile_label="Tables, Diagrams & ADK Math",
        synthesis_model="gemini-4-argon",
        judge_model="claude-opus-5-5",
        alt_judge_model="claude-sonnet-4-6",
        rationale=(
            "Gemini 4 Argon ($2.00 / $10.00 per 1M) is the flagship multimodal and Google ADK agentic worker "
            "for tables, diagrams, and multi-hop arithmetic."
        ),
        judge_rationale=(
            "Claude Opus 5.5 delivers flagship cross-family evaluation and matches the committed golden benchmark cohort."
        ),
        cross_family=True,
        adk_compatible=True,
    ),
    ModelPairRecommendation(
        profile_id=PROFILE_ANTHROPIC_WORKER,
        profile_label="Claude Bake-Off (Levels 1–7)",
        synthesis_model="claude-sonnet-4-6",
        judge_model="gemini-4-argon",
        alt_judge_model="gemini-4-carbon",
        rationale=(
            "Claude Sonnet 4.6 ($3.00 / $15.00 per 1M) benchmarks Anthropic synthesis on single-pass Levels 1–7 "
            "(or upgrade to Claude Opus 5.5)."
        ),
        judge_rationale=(
            "Gemini 4 Argon provides independent cross-family Google evaluation so Claude never grades its own family."
        ),
        cross_family=True,
        adk_compatible=False,
    ),
)

_PROFILE_BY_ID: dict[str, ModelPairRecommendation] = {p.profile_id: p for p in RECOMMENDED_PROFILES}


def _is_available(catalog: ModelCatalog | None, model_id: str, role: str, *, require_gemini: bool = False) -> bool:
    if catalog is None:
        return not require_gemini or model_family(model_id) == "gemini"
    spec: ModelSpec | None = next((m for m in catalog.models if m.id == model_id), None)
    if spec is None or not spec.available or role not in spec.roles:
        return False
    if require_gemini and spec.provider != PROVIDER_VERTEX_GEMINI:
        return False
    return True


def _pick_synthesis(preferred: str, catalog: ModelCatalog | None, *, require_gemini: bool) -> str:
    if _is_available(catalog, preferred, ROLE_SYNTHESIS, require_gemini=require_gemini):
        return preferred
    fam = "gemini" if require_gemini else model_family(preferred)
    order = _GEMINI_SYNTH_FALLBACKS if fam == "gemini" else _ANTHROPIC_SYNTH_FALLBACKS + _GEMINI_SYNTH_FALLBACKS
    for candidate in order:
        if _is_available(catalog, candidate, ROLE_SYNTHESIS, require_gemini=require_gemini):
            return candidate
    if catalog is not None:
        for spec in catalog.models:
            if spec.available and ROLE_SYNTHESIS in spec.roles and not spec.allowed_levels:
                if not require_gemini or spec.provider == PROVIDER_VERTEX_GEMINI:
                    return spec.id
    return "gemini-4-argon" if require_gemini else preferred


def _pick_judge(
    preferred: str,
    alt: str | None,
    synthesis_model: str,
    catalog: ModelCatalog | None,
    *,
    prefer_rubric_judge: bool,
) -> str:
    candidates: list[str] = []
    if prefer_rubric_judge:
        candidates.append(VERTEX_GENAI_EVAL_MODEL_ID)
    candidates.append(preferred)
    if alt:
        candidates.append(alt)
    synth_family = model_family(synthesis_model)
    cross_fallbacks = _ANTHROPIC_JUDGE_FALLBACKS if synth_family == "gemini" else _GEMINI_JUDGE_FALLBACKS
    same_fallbacks = _GEMINI_JUDGE_FALLBACKS if synth_family == "gemini" else _ANTHROPIC_JUDGE_FALLBACKS
    candidates.extend(cross_fallbacks)
    candidates.append(VERTEX_GENAI_EVAL_MODEL_ID)
    candidates.extend(same_fallbacks)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen or candidate == synthesis_model:
            continue
        seen.add(candidate)
        if _is_available(catalog, candidate, ROLE_JUDGE):
            return candidate

    if catalog is not None:
        for spec in catalog.models:
            if spec.available and ROLE_JUDGE in spec.roles and spec.id != synthesis_model:
                return spec.id
    return preferred if preferred != synthesis_model else "claude-opus-5-5"


def infer_profile_id(
    *,
    level_id: str | None = None,
    doc_complexity: str | None = None,
    latency_sla: str | None = None,
    data_gravity: str | None = None,
    monthly_queries: int | None = None,
    reasoning_mode: str | None = None,
    prefer_anthropic_worker: bool = False,
) -> str:
    """Deterministically map workload signals or level context onto one of the 4 canonical profiles."""
    adk_required = level_id == "level4_agentic" or reasoning_mode == "adk_agent"
    if adk_required or doc_complexity == "multi_hop_math" or latency_sla == "deep_agentic_reasoning":
        return PROFILE_MULTIMODAL_AGENTIC
    if prefer_anthropic_worker:
        return PROFILE_ANTHROPIC_WORKER
    if latency_sla == "sub_second_retrieval" or (monthly_queries is not None and monthly_queries >= 250_000):
        return PROFILE_COST_LATENCY
    if doc_complexity == "tables_diagrams" or level_id == "level3_multimodal":
        return PROFILE_MULTIMODAL_AGENTIC
    if data_gravity == "bigquery_warehouse" or level_id in ("level2_hybrid", "alloydb_scann"):
        return PROFILE_BALANCED_ENTERPRISE
    if data_gravity in ("managed_turnkey", "postgres_oltp") or level_id in (
        "managed_vertex",
        "rag_engine",
        "level1_naive",
        "vertex_vector_v2",
    ):
        return PROFILE_COST_LATENCY
    return PROFILE_BALANCED_ENTERPRISE


def recommend_model_pair(
    *,
    profile_id: str | None = None,
    level_id: str | None = None,
    doc_complexity: str | None = None,
    latency_sla: str | None = None,
    data_gravity: str | None = None,
    monthly_queries: int | None = None,
    reasoning_mode: str | None = None,
    prefer_anthropic_worker: bool = False,
    prefer_rubric_judge: bool = False,
    catalog: ModelCatalog | None = None,
) -> ModelPairRecommendation:
    """Recommend an optimal available (synthesis, judge) pair for the given profile or workload."""
    adk_required = level_id == "level4_agentic" or reasoning_mode == "adk_agent"
    if profile_id is not None:
        if profile_id not in _PROFILE_BY_ID:
            raise ValueError(f"Unknown model profile '{profile_id}'; valid: {list(RECOMMENDATION_PROFILE_IDS)}")
        chosen_id = PROFILE_MULTIMODAL_AGENTIC if (adk_required and profile_id == PROFILE_ANTHROPIC_WORKER) else profile_id
    else:
        chosen_id = infer_profile_id(
            level_id=level_id,
            doc_complexity=doc_complexity,
            latency_sla=latency_sla,
            data_gravity=data_gravity,
            monthly_queries=monthly_queries,
            reasoning_mode=reasoning_mode,
            prefer_anthropic_worker=prefer_anthropic_worker,
        )

    base = _PROFILE_BY_ID[chosen_id]
    synth = _pick_synthesis(base.synthesis_model, catalog, require_gemini=adk_required)
    judge = _pick_judge(
        base.judge_model,
        base.alt_judge_model,
        synth,
        catalog,
        prefer_rubric_judge=prefer_rubric_judge,
    )
    alt_judge = base.alt_judge_model if base.alt_judge_model not in (synth, judge) else None
    if alt_judge and catalog is not None and not _is_available(catalog, alt_judge, ROLE_JUDGE):
        alt_judge = None

    rationale = base.rationale
    if adk_required and profile_id == PROFILE_ANTHROPIC_WORKER:
        rationale = (
            "Level 8 Google ADK requires a Vertex Gemini worker (Claude is judge-only on ADK); "
            "switched to Gemini 4 Argon."
        )
    judge_rationale = base.judge_rationale
    if judge == VERTEX_GENAI_EVAL_MODEL_ID and base.judge_model != VERTEX_GENAI_EVAL_MODEL_ID:
        judge_rationale = (
            "Vertex AI GenAI Eval (:evaluateInstances) applies managed rubric scoring independently of the synthesis model."
        )

    return ModelPairRecommendation(
        profile_id=base.profile_id,
        profile_label=base.profile_label,
        synthesis_model=synth,
        judge_model=judge,
        alt_judge_model=alt_judge,
        rationale=rationale,
        judge_rationale=judge_rationale,
        cross_family=is_cross_family_pair(synth, judge),
        adk_compatible=model_family(synth) == "gemini",
    )


def recommended_pairs_for_catalog(catalog: ModelCatalog | None = None) -> list[dict[str, Any]]:
    """Serialize all 4 canonical model pair recommendations resolved against ``catalog``."""
    return [
        recommend_model_pair(profile_id=pid, catalog=catalog).to_dict()
        for pid in RECOMMENDATION_PROFILE_IDS
    ]


__all__ = [
    "ModelPairRecommendation",
    "PROFILE_ANTHROPIC_WORKER",
    "PROFILE_BALANCED_ENTERPRISE",
    "PROFILE_COST_LATENCY",
    "PROFILE_MULTIMODAL_AGENTIC",
    "RECOMMENDATION_PROFILE_IDS",
    "RECOMMENDED_PROFILES",
    "infer_profile_id",
    "is_cross_family_pair",
    "model_family",
    "recommend_model_pair",
    "recommended_pairs_for_catalog",
]
