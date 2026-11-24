"""Pure Identity-Aware Document ACL & Row-Level Security domain model (v1.6.3).

Functional Core invariants:
- Zero I/O, zero clocks, zero network calls; acyclic imports within ``app.core``.
- Fail-closed authorization: unknown persona IDs raise ``AclValidationError``; caller identity
  ceiling strictly narrows (``ceiling_groups ∩ persona.groups``) so a client can never escalate
  privileges.
- Honest per-architecture capability declarations: ``rag_engine`` declares
  ``supports_chunk_acl=False`` (``corpus_boundary_only``) rather than faking shared-corpus chunk
  filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from app.core.models import _SerializableMixin

ALL_ACCESS_PERSONA_ID = "all_access"
ACL_RECORDING_MARKER = "__acl_"

VALID_ACL_GROUPS: tuple[str, ...] = (
    "corp_all",
    "procurement_pricing",
    "security_soc2",
    "legal_contracts",
    "exec_admin",
)
_VALID_GROUP_SET: frozenset[str] = frozenset(VALID_ACL_GROUPS)
_GROUP_ORDER: dict[str, int] = {g: idx for idx, g in enumerate(VALID_ACL_GROUPS)}
_GENERATED_PREFIX_RE = re.compile(r"^\d{2}_")


class AclValidationError(ValueError):
    """Raised when an access persona or ACL group list fails strict allow-list validation."""


class AclEnforcementUnsupportedError(RuntimeError):
    """Raised fail-closed when a store cannot enforce per-document chunk ACLs (e.g. RagCorpus)."""


@dataclass(frozen=True)
class AclPersona(_SerializableMixin):
    """Named enterprise access persona used to narrow caller group membership."""

    persona_id: str
    label: str
    short_label: str
    description: str
    groups: tuple[str, ...]
    blocked_summary: str

    def __post_init__(self) -> None:
        if not self.persona_id or not isinstance(self.persona_id, str):
            raise AclValidationError("persona_id must be a non-empty string")
        if not self.groups or any(g not in _VALID_GROUP_SET for g in self.groups):
            raise AclValidationError(f"persona {self.persona_id!r} contains invalid ACL groups: {self.groups!r}")


@dataclass(frozen=True)
class CallerAccessContext(_SerializableMixin):
    """Resolved, fail-closed access context attached to a query or document view request."""

    persona_id: str
    label: str
    effective_groups: tuple[str, ...]
    caller_ceiling_groups: tuple[str, ...]
    is_restricted: bool

    def __post_init__(self) -> None:
        if not self.effective_groups or any(g not in _VALID_GROUP_SET for g in self.effective_groups):
            raise AclValidationError(f"effective_groups must be a non-empty subset of {VALID_ACL_GROUPS}")
        if not set(self.effective_groups).issubset(set(self.caller_ceiling_groups)):
            raise AclValidationError("effective_groups cannot exceed caller_ceiling_groups")


@dataclass(frozen=True)
class ArchitectureAclCapability(_SerializableMixin):
    """Honest per-architecture ACL enforcement declaration (spec P4: never fake unsupported features)."""

    level_id: str
    supports_chunk_acl: bool
    enforcement_mode: str
    mechanism_label: str
    summary: str
    honest_note: str


ACL_PERSONAS: tuple[AclPersona, ...] = (
    AclPersona(
        persona_id="all_access",
        label="All Access (Admin)",
        short_label="All Access",
        description="Unrestricted executive/procurement administrator across all corpus groups.",
        groups=VALID_ACL_GROUPS,
        blocked_summary="None — all 45 benchmark documents authorized",
    ),
    AclPersona(
        persona_id="engineering_analyst",
        label="Engineering Analyst",
        short_label="Engineering",
        description="Technical architecture, base RFPs, SOWs, and vendor Q&A only.",
        groups=("corp_all",),
        blocked_summary="Blocks pricing CLIN rate cards, rate amendments, SOC2/IR matrices, MSA, and CUAD contracts",
    ),
    AclPersona(
        persona_id="pricing_finance",
        label="Pricing & Finance",
        short_label="Pricing & Finance",
        description="General solicitations plus commercial CLIN pricing schedules and rate amendments.",
        groups=("corp_all", "procurement_pricing"),
        blocked_summary="Blocks SOC2/incident-response matrices, MSA, and third-party CUAD contracts",
    ),
    AclPersona(
        persona_id="security_soc2",
        label="Security & SOC2",
        short_label="Security & SOC2",
        description="General solicitations plus SOC2 compliance matrices and incident response SLAs.",
        groups=("corp_all", "security_soc2"),
        blocked_summary="Blocks CLIN pricing schedules, rate amendments, MSA, and third-party CUAD contracts",
    ),
    AclPersona(
        persona_id="legal_counsel",
        label="Legal Counsel",
        short_label="Legal Counsel",
        description="General solicitations plus Master Services Agreement and third-party CUAD agreements.",
        groups=("corp_all", "legal_contracts"),
        blocked_summary="Blocks CLIN pricing schedules, rate amendments, and SOC2/incident-response matrices",
    ),
)
_PERSONAS_BY_ID: dict[str, AclPersona] = {p.persona_id: p for p in ACL_PERSONAS}

ARCHITECTURE_ACL_CAPABILITIES: dict[str, ArchitectureAclCapability] = {
    "managed_vertex": ArchitectureAclCapability(
        level_id="managed_vertex",
        supports_chunk_acl=True,
        enforcement_mode="vais_filter_expression",
        mechanism_label="Vertex AI Search filter expression",
        summary="Filters documents via Discovery Engine structData allowed_groups: ANY(...) (enterprise IdP aclConfig supported).",
        honest_note="Corpus imports with structured allowed_groups metadata enforce pre-synthesis filtering at search time.",
    ),
    "rag_engine": ArchitectureAclCapability(
        level_id="rag_engine",
        supports_chunk_acl=False,
        enforcement_mode="corpus_boundary_only",
        mechanism_label="RagCorpus IAM boundary only",
        summary="Vertex AI RAG Engine enforces IAM per RagCorpus, not per-document chunk ACLs inside a shared corpus.",
        honest_note="Restricted personas fail closed on a shared RagCorpus; isolate sensitive groups into dedicated corpora or use BigQuery / AlloyDB / VAIS / VVS2.",
    ),
    "level1_naive": ArchitectureAclCapability(
        level_id="level1_naive",
        supports_chunk_acl=True,
        enforcement_mode="bigquery_parameterized_rls",
        mechanism_label="BigQuery parameterized RLS (@caller_groups)",
        summary="Pre-filters document_chunks_v3 inside VECTOR_SEARCH via UNNEST(allowed_groups) IN UNNEST(@caller_groups) + optional ROW ACCESS POLICY.",
        honest_note="Unauthorized rows never leave BigQuery and never enter the synthesis prompt.",
    ),
    "level2_hybrid": ArchitectureAclCapability(
        level_id="level2_hybrid",
        supports_chunk_acl=True,
        enforcement_mode="bigquery_parameterized_rls",
        mechanism_label="BigQuery parameterized RLS (@caller_groups)",
        summary="Pre-filters both VECTOR_SEARCH and SEARCH() RRF legs inside BigQuery via @caller_groups before Discovery Engine reranking.",
        honest_note="Unauthorized chunks are excluded before RRF fusion, reranking, and synthesis.",
    ),
    "cloudsql_pgvector": ArchitectureAclCapability(
        level_id="cloudsql_pgvector",
        supports_chunk_acl=True,
        enforcement_mode="postgres_rls_policy",
        mechanism_label="PostgreSQL Row-Level Security + GIN",
        summary="Enforces PostgreSQL CREATE POLICY (allowed_groups && current_setting('app.caller_groups')::text[]) and parameterized %(caller_groups)s on pgvector HNSW + FTS legs.",
        honest_note="Database-layer RLS enforces row visibility on Cloud SQL PostgreSQL before HNSW/RRF fusion.",
    ),
    "alloydb_scann": ArchitectureAclCapability(
        level_id="alloydb_scann",
        supports_chunk_acl=True,
        enforcement_mode="postgres_rls_policy",
        mechanism_label="PostgreSQL Row-Level Security + GIN",
        summary="Enforces PostgreSQL CREATE POLICY (allowed_groups && current_setting('app.caller_groups')::text[]) and parameterized %(caller_groups)s on ScaNN + FTS legs.",
        honest_note="Database-layer RLS enforces row visibility even against direct SQL connections.",
    ),
    "vertex_vector_v2": ArchitectureAclCapability(
        level_id="vertex_vector_v2",
        supports_chunk_acl=True,
        enforcement_mode="vvs2_filter_fields",
        mechanism_label="Vector Search 2.0 filterFields ($in)",
        summary="Applies pre-filtered {'allowed_groups': {'$in': [...]}} on both dense ScaNN and sparse RRF legs in dataObjects:batchSearch.",
        honest_note="Indexed filterFields prune unauthorized DataObjects before RRF scoring.",
    ),
    "level3_multimodal": ArchitectureAclCapability(
        level_id="level3_multimodal",
        supports_chunk_acl=True,
        enforcement_mode="bigquery_parameterized_rls",
        mechanism_label="BigQuery parameterized RLS (@caller_groups)",
        summary="Pre-filters multimodal tables, diagrams, and amendments inside BigQuery VECTOR_SEARCH + SEARCH() via @caller_groups.",
        honest_note="Unauthorized tables and visual diagrams are excluded before rerank and synthesis.",
    ),
    "bigquery_graphrag": ArchitectureAclCapability(
        level_id="bigquery_graphrag",
        supports_chunk_acl=True,
        enforcement_mode="bigquery_parameterized_rls",
        mechanism_label="BigQuery RLS + GQL graph-hop pre-filter",
        summary="Pre-filters multimodal retrieval and BigQuery Property Graph ISO GQL GRAPH_TABLE hops via @caller_groups so restricted amendment edges never surface.",
        honest_note="Both hybrid seed chunks and GQL-traversed superseding amendment chunks enforce UNNEST(allowed_groups) IN UNNEST(@caller_groups).",
    ),
    "level4_agentic": ArchitectureAclCapability(
        level_id="level4_agentic",
        supports_chunk_acl=True,
        enforcement_mode="bigquery_parameterized_rls",
        mechanism_label="ADK closure-pinned BigQuery RLS",
        summary="Closure-pins @caller_groups inside search_procurement_corpus and check_amendments so prompt-injected PDFs cannot widen access.",
        honest_note="Tool arguments cannot override caller_groups; every BigQuery tool query inherits the caller's effective groups.",
    ),
}


def list_acl_personas() -> tuple[AclPersona, ...]:
    """Return the ordered tuple of supported enterprise access personas."""
    return ACL_PERSONAS


def _normalize_groups(groups: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    for raw in groups:
        if not isinstance(raw, str) or raw not in _VALID_GROUP_SET:
            raise AclValidationError(f"Unknown ACL group {raw!r}; allowed: {list(VALID_ACL_GROUPS)}")
        seen.add(raw)
    if not seen:
        raise AclValidationError("ACL group set must be non-empty")
    return tuple(sorted(seen, key=lambda g: _GROUP_ORDER[g]))


def resolve_caller_access(
    requested_persona: str | None,
    *,
    ceiling_groups: tuple[str, ...] = VALID_ACL_GROUPS,
) -> CallerAccessContext:
    """Resolve and strictly narrow caller access (``ceiling_groups ∩ persona.groups``; fail-closed)."""
    ceiling = _normalize_groups(ceiling_groups)
    if requested_persona is None:
        persona = _PERSONAS_BY_ID[ALL_ACCESS_PERSONA_ID]
    else:
        if not isinstance(requested_persona, str) or not requested_persona.strip():
            raise AclValidationError("access_persona must be a non-empty persona id or omitted")
        pid = requested_persona.strip()
        if pid not in _PERSONAS_BY_ID:
            raise AclValidationError(
                f"Unknown access_persona {pid!r}; allowed: {[p.persona_id for p in ACL_PERSONAS]}"
            )
        persona = _PERSONAS_BY_ID[pid]

    effective = tuple(g for g in ceiling if g in persona.groups)
    if not effective:
        raise AclValidationError(
            f"Requested persona {persona.persona_id!r} has zero group overlap with caller ceiling {list(ceiling)}"
        )
    is_restricted = set(effective) != set(VALID_ACL_GROUPS) or persona.persona_id != ALL_ACCESS_PERSONA_ID
    return CallerAccessContext(
        persona_id=persona.persona_id,
        label=persona.label,
        effective_groups=effective,
        caller_ceiling_groups=ceiling,
        is_restricted=is_restricted,
    )


def allowed_groups_for_doc(doc_name: str, doc_type: str = "") -> tuple[str, ...]:
    """Deterministic O(1) mapping from a corpus PDF filename (and optional doc_type) to ``allowed_groups``.

    Every corpus document grants ``exec_admin`` so ``all_access`` is always a strict superset.
    """
    name = str(doc_name or "").strip()
    lower = name.lower()
    dtype = str(doc_type or "").strip().upper()

    if not name or not _GENERATED_PREFIX_RE.match(name):
        # Third-party commercial agreements (27 CUAD PDFs) or custom legal contracts.
        return ("legal_contracts", "exec_admin")

    if "master_services_agreement" in lower or "_msa" in lower or dtype == "MSA":
        return ("legal_contracts", "exec_admin")
    if "soc2" in lower or "compliance" in lower or "incident_response" in lower:
        return ("security_soc2", "exec_admin")
    if (
        "pricing" in lower
        or "clin" in lower
        or "amendment_0001" in lower
        or "amendments_and_addenda" in lower
        or dtype == "PRICING_CLIN"
    ):
        return ("procurement_pricing", "exec_admin")
    return ("corp_all", "exec_admin")


def is_doc_authorized(doc_name: str, access: CallerAccessContext | None, doc_type: str = "") -> bool:
    """True when ``access`` is None/unrestricted or intersects ``allowed_groups_for_doc(doc_name)``."""
    if access is None or not access.is_restricted:
        return True
    doc_groups = allowed_groups_for_doc(doc_name, doc_type=doc_type)
    return bool(set(doc_groups) & set(access.effective_groups))


def corpus_acl_summary(doc_names: Iterable[str]) -> dict[str, Any]:
    """Summarize corpus document visibility across all personas and groups (pure)."""
    names = list(doc_names)
    by_group: dict[str, int] = {g: 0 for g in VALID_ACL_GROUPS}
    for name in names:
        for g in allowed_groups_for_doc(name):
            by_group[g] += 1
    by_persona: dict[str, int] = {}
    for persona in ACL_PERSONAS:
        ctx = resolve_caller_access(persona.persona_id)
        by_persona[persona.persona_id] = sum(1 for name in names if is_doc_authorized(name, ctx))
    return {
        "total_docs": len(names),
        "authorized_by_group": by_group,
        "authorized_by_persona": by_persona,
    }


def acl_recording_question_id(question_id: str, access: CallerAccessContext | None) -> str:
    """Map ``(question_id, access)`` to the recording filename stem (unchanged for unrestricted runs)."""
    if access is None or not access.is_restricted:
        return question_id
    return f"{question_id}{ACL_RECORDING_MARKER}{access.persona_id}"


def is_acl_recording_question_id(question_id: str) -> bool:
    """True when a recording question_id encodes a restricted ACL persona run."""
    return isinstance(question_id, str) and ACL_RECORDING_MARKER in question_id


def acl_refusal_answer(access: CallerAccessContext, query: str) -> str:
    """Deterministic zero-leak refusal emitted when row-level security filters out all candidate rows."""
    groups_str = ", ".join(access.effective_groups)
    return (
        f"Access restricted under row-level security ({access.label}; authorized groups: {groups_str}). "
        "Zero authorized document chunks matched this request after database/index ACL filtering, "
        "so no restricted pricing, compliance, or legal content was passed to synthesis."
    )


def acl_stage_metadata(level_id: str, access: CallerAccessContext | None) -> dict[str, Any]:
    """Structured ACL telemetry merged into retrieve/stage payloads (honest labels, zero secrets)."""
    cap = ARCHITECTURE_ACL_CAPABILITIES.get(level_id)
    if access is None:
        ctx = resolve_caller_access(None)
    else:
        ctx = access
    return {
        "acl_persona": ctx.persona_id,
        "acl_label": ctx.label,
        "acl_groups": list(ctx.effective_groups),
        "acl_restricted": ctx.is_restricted,
        "acl_enforcement": cap.enforcement_mode if cap else "none",
        "acl_supports_chunk_rls": cap.supports_chunk_acl if cap else False,
    }


__all__ = [
    "ACL_PERSONAS",
    "ACL_RECORDING_MARKER",
    "ALL_ACCESS_PERSONA_ID",
    "ARCHITECTURE_ACL_CAPABILITIES",
    "AclEnforcementUnsupportedError",
    "AclPersona",
    "AclValidationError",
    "ArchitectureAclCapability",
    "CallerAccessContext",
    "VALID_ACL_GROUPS",
    "acl_recording_question_id",
    "acl_refusal_answer",
    "acl_stage_metadata",
    "allowed_groups_for_doc",
    "corpus_acl_summary",
    "is_acl_recording_question_id",
    "is_doc_authorized",
    "list_acl_personas",
    "resolve_caller_access",
]
