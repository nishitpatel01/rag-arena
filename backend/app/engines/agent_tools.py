"""Level 4 ADK tools over real data (no hardcoded ledgers, no defaults invented for the model).

``build_tools(deps)`` returns four async functions bound to one run's ``ToolDeps`` (closures; nothing global):

* ``search_procurement_corpus`` — the Level 3 retrieve + rerank pipeline (``Level3MultimodalEngine.search_multimodal``:
  gemini-embedding-2 multimodal → BigQuery RRF over tier 'multimodal' → Ranking API) returning the top chunks.
* ``check_amendments`` — parameterized BigQuery lookup of AMENDMENT rows whose ``amends_section`` names the clause/CLIN,
  latest amendment first, with the superseding text and effective date.
* ``calculate_contract_pricing`` — AST-validated arithmetic (``agent_calculator``).
* ``verify_citations`` — every numeric/clause claim of a draft checked against the cited chunks' text (``agent_verify``).

Argument errors (bad ids, forbidden expressions) are returned to the model as ``{"status": "error"}`` results — visible
in the ``agent_turn`` trace — so it can correct itself. Infrastructure errors (BigQuery, Vertex, Ranking API) propagate
out of the ADK runner and fail the run loudly (spec P2). Every executed call is measured and recorded in the ledger.
"""

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Any, Awaitable, Callable, Optional

from google.adk.tools.tool_context import ToolContext

from app.core.config import ArenaSettings
from app.core.models import DocumentChunk
from app.core.telemetry import NullStageEmitter
from app.engines.agent_calculator import CalculatorError, evaluate_expression
from app.engines.agent_ledger import AgentLedger, ToolRecord, bounded_summary
from app.engines.agent_verify import MAX_DRAFT_CHARS, VerificationReport, check_claims
from app.engines.level1_naive import _row_bbox
from app.engines.level3_multimodal import Level3MultimodalEngine, MultimodalSearch, authority_header
from app.ingestion.bq_indexer import build_amendment_lookup_sql

MAX_TOOL_CHUNK_CHARS = 2500
MAX_QUERY_CHARS = 500
MAX_CITED_IDS = 24
ELEMENT_TYPES: tuple[str, ...] = ("narrative_text", "table", "diagram")
_SOLICITATION_RE = re.compile(r"solicitation_\d{2}")
_SECTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-_]{0,63}")
SEARCH_TOOL = "search_procurement_corpus"
AMENDMENTS_TOOL = "check_amendments"
CALCULATOR_TOOL = "calculate_contract_pricing"
VERIFY_TOOL = "verify_citations"
TOOL_NAMES: tuple[str, ...] = (SEARCH_TOOL, AMENDMENTS_TOOL, CALCULATOR_TOOL, VERIFY_TOOL)


class ToolArgumentError(ValueError):
    """The model passed invalid arguments; reported back to the model as a tool error result."""


@dataclass(frozen=True)
class ToolOutput:
    response: dict[str, Any]
    summary: str
    chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolDeps:
    """Everything one run's tools need; built per run by the Level 4 engine."""

    settings: ArenaSettings
    retriever: Level3MultimodalEngine
    genai_client: Any
    bq_client: Any
    ledger: AgentLedger
    sequence: list[int] = field(default_factory=lambda: [0])  # per-run call counter (ids when ADK gives none)


# ---------------------------------------------------------------- shared invocation wrapper


async def _invoke(
    deps: ToolDeps, name: str, tool_context: Any, args: dict[str, Any], body: Callable[[], Awaitable[ToolOutput]]
) -> dict[str, Any]:
    """Run ``body``, measure it, record a ToolRecord; argument errors become an error result for the model."""
    deps.sequence[0] += 1
    seq = deps.sequence[0]
    call_id = str(getattr(tool_context, "function_call_id", "") or f"{name}_{seq}")
    t0 = time.perf_counter()
    try:
        out = await body()
        status = "ok"
    except (ToolArgumentError, CalculatorError) as exc:
        out = ToolOutput(response={"status": "error", "error": str(exc)}, summary=f"argument error: {exc}")
        status = "error"
    duration_ms = max(0, int((time.perf_counter() - t0) * 1000))
    deps.ledger.record_tool(
        ToolRecord(call_id=call_id, tool_name=name, args=args, summary=bounded_summary(out.summary),
                   duration_ms=duration_ms, status=status, chunk_ids=out.chunk_ids, seq=seq)
    )
    return out.response


def _chunk_view(chunk: DocumentChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id, "authority_header": authority_header(chunk), "solicitation_id": chunk.solicitation_id,
        "doc_name": chunk.doc_name, "page_number": chunk.page_number, "section_id": chunk.section_id,
        "element_type": chunk.element_type, "amendment_number": chunk.amendment_number,
        "effective_date": chunk.effective_date, "amends_section": chunk.amends_section, "score": chunk.score,
        "text": chunk.content[:MAX_TOOL_CHUNK_CHARS],
    }


# ---------------------------------------------------------------- search_procurement_corpus


def validate_search_args(query: Any, solicitation_id: Any, element_types: Any) -> tuple[str, str | None, tuple[str, ...]]:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ToolArgumentError(f"query must be a non-empty string of at most {MAX_QUERY_CHARS} characters")
    sol = (solicitation_id or "").strip() if isinstance(solicitation_id, str) or solicitation_id is None else None
    if sol is None or (sol and not _SOLICITATION_RE.fullmatch(sol)):
        raise ToolArgumentError("solicitation_id must be empty or look like 'solicitation_01'")
    types_in = element_types or []
    if not isinstance(types_in, list) or len(types_in) > len(ELEMENT_TYPES) or any(t not in ELEMENT_TYPES for t in types_in):
        raise ToolArgumentError(f"element_types must be a list drawn from {list(ELEMENT_TYPES)}")
    return query.strip(), sol or None, tuple(types_in)


def _search_summary(found: MultimodalSearch) -> str:
    retrieve = found.stage_payload("retrieve")
    mb = round(int(retrieve.get("bytes_processed", 0) or 0) / 1e6, 1)
    top = found.chunks[0] if found.chunks else None
    head = f"top {top.doc_name} p.{top.page_number} ({top.score})" if top else "no chunk after filter"
    cache = " · cache hit" if retrieve.get("cache_hit") else ""
    return f"{len(found.chunks)} chunks of {found.filtered_candidates}/{found.candidates} RRF candidates · {head} · {mb} MB scanned{cache}"


async def run_search(deps: ToolDeps, query: Any, solicitation_id: Any, element_types: Any) -> ToolOutput:
    q, sol, types_ = validate_search_args(query, solicitation_id, element_types)
    emitter = NullStageEmitter(run_id=f"{deps.ledger.run_id}_search{deps.sequence[0]}", engine="level3_multimodal")
    found = await deps.retriever.search_multimodal(
        q, solicitation_filter=sol, element_types=types_, genai_client=deps.genai_client, bq_client=deps.bq_client, emitter=emitter
    )
    retrieve = found.stage_payload("retrieve")
    deps.ledger.add_usage(
        embedding_tokens=found.embedding_tokens, rank_counts=found.rank_billed_counts,
        bq_bytes=int(retrieve.get("bytes_processed", 0) or 0), bq_jobs=1, search=True,
    )
    ids = deps.ledger.register_chunks(found.chunks)
    response = {
        "status": "ok", "query": q, "solicitation_filter": sol, "element_types": list(types_),
        "rrf_candidates": found.candidates, "after_element_filter": found.filtered_candidates,
        "chunks": [_chunk_view(c) for c in found.chunks],
    }
    if not found.chunks:
        response["note"] = f"no RRF candidate had element_type in {list(types_)}; retry without element_types"
    return ToolOutput(response=response, summary=_search_summary(found), chunk_ids=ids)


# ---------------------------------------------------------------- check_amendments


def validate_amendment_args(solicitation_id: Any, section_or_clin_id: Any) -> tuple[str, str]:
    if not isinstance(solicitation_id, str) or not _SOLICITATION_RE.fullmatch(solicitation_id.strip()):
        raise ToolArgumentError("solicitation_id must look like 'solicitation_01' (use the solicitation_id of a search result)")
    if not isinstance(section_or_clin_id, str) or not _SECTION_RE.fullmatch(section_or_clin_id.strip()):
        raise ToolArgumentError("section_or_clin_id must be a clause or CLIN id such as 'CLIN-0004-AB' or 'SEC-4.2.1'")
    return solicitation_id.strip(), section_or_clin_id.strip()


def amendment_row_to_chunk(row: Any) -> DocumentChunk:
    page = int(row.page_number)
    effective = getattr(row, "effective_date", None)
    return DocumentChunk(
        chunk_id=str(row.chunk_id), tier_profile="multimodal", solicitation_id=str(row.solicitation_id),
        doc_name=str(row.doc_name), doc_type=str(row.doc_type), element_type=str(row.element_type),
        section_id=str(row.section_id) if getattr(row, "section_id", None) else None,
        amendment_number=int(row.amendment_number or 0), effective_date=str(effective) if effective else None,
        amends_section=str(row.amends_section) if getattr(row, "amends_section", None) else None,
        page_number=page, bbox=_row_bbox(row, page), content=str(row.content),
        layout_source=str(getattr(row, "layout_source", "") or ""), bbox_source=str(getattr(row, "bbox_source", "") or ""),
    )


def run_amendment_query(bq_client: Any, sql: str, solicitation_id: str, section_id: str, timeout_seconds: int) -> tuple[list[Any], dict[str, Any]]:
    """Execute the parameterized amendment lookup (worker thread); rows + measured job statistics."""
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("solicitation_id", "STRING", solicitation_id),
        bigquery.ScalarQueryParameter("section_id", "STRING", section_id),
    ]
    job = bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = list(job.result(timeout=timeout_seconds))
    return rows, {
        "job_id": str(getattr(job, "job_id", "") or ""), "bytes_processed": int(getattr(job, "total_bytes_processed", 0) or 0),
        "cache_hit": bool(getattr(job, "cache_hit", False)),
    }


def _amendment_view(chunk: DocumentChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id, "doc_name": chunk.doc_name, "page_number": chunk.page_number,
        "amendment_number": chunk.amendment_number, "effective_date": chunk.effective_date,
        "amends_section": chunk.amends_section, "superseding_text": chunk.content[:MAX_TOOL_CHUNK_CHARS],
    }


async def run_check_amendments(deps: ToolDeps, solicitation_id: Any, section_or_clin_id: Any) -> ToolOutput:
    sol, section = validate_amendment_args(solicitation_id, section_or_clin_id)
    s = deps.settings
    sql = build_amendment_lookup_sql(s.gcp_project_id, s.bq_dataset, s.bq_table)
    rows, stats = await asyncio.to_thread(run_amendment_query, deps.bq_client, sql, sol, section, s.query_timeout_seconds)
    chunks = [amendment_row_to_chunk(r) for r in rows]
    deps.ledger.add_usage(bq_bytes=stats["bytes_processed"], bq_jobs=1)
    ids = deps.ledger.register_chunks(chunks)
    views = [_amendment_view(c) for c in chunks]
    response = {
        "status": "ok", "solicitation_id": sol, "section_or_clin_id": section, "has_amendment": bool(chunks),
        "governing_amendment": views[0] if views else None, "amendments": views,
    }
    if chunks:
        g = chunks[0]
        summary = f"governing: Amendment {g.amendment_number:04d} (effective {g.effective_date}) · {g.doc_name} p.{g.page_number} · {len(chunks)} row(s)"
    else:
        summary = f"no amendment row names {section} in {sol}"
    return ToolOutput(response=response, summary=f"{summary} · {round(stats['bytes_processed'] / 1e6, 1)} MB scanned", chunk_ids=ids)


# ---------------------------------------------------------------- calculate_contract_pricing / verify_citations


async def run_calculator(deps: ToolDeps, expression: Any, variables: Any) -> ToolOutput:
    result = evaluate_expression(expression, variables)
    deps.ledger.add_calc_value(result.value)
    return ToolOutput(response=result.to_dict(), summary=f"{result.substituted_expression} = {result.value}")


def verify_draft(ledger: AgentLedger, draft_answer: Any, cited_chunk_ids: Any) -> VerificationReport:
    if not isinstance(draft_answer, str) or not draft_answer.strip() or len(draft_answer) > MAX_DRAFT_CHARS:
        raise ToolArgumentError(f"draft_answer must be a non-empty string of at most {MAX_DRAFT_CHARS} characters")
    if not isinstance(cited_chunk_ids, list) or not cited_chunk_ids or len(cited_chunk_ids) > MAX_CITED_IDS:
        raise ToolArgumentError(f"cited_chunk_ids must be a non-empty list of at most {MAX_CITED_IDS} chunk ids")
    ids = tuple(dict.fromkeys(str(c).strip() for c in cited_chunk_ids if str(c).strip()))
    known = tuple(i for i in ids if ledger.chunk(i) is not None)
    unknown = tuple(i for i in ids if ledger.chunk(i) is None)
    texts = []
    for cid in known:
        ch = ledger.chunk(cid)
        texts.append(f"{ch.content}\n{ch.section_id or ''}\n{ch.amends_section or ''}")
    checks = check_claims(draft_answer, texts, ledger.calc_values)
    return VerificationReport(checks=checks, unknown_chunk_ids=unknown, cited_chunk_ids=known)


async def run_verify(deps: ToolDeps, draft_answer: Any, cited_chunk_ids: Any) -> ToolOutput:
    report = verify_draft(deps.ledger, draft_answer, cited_chunk_ids)
    deps.ledger.add_verification(report)
    summary = (
        f"{len(report.checks)} claims checked · {len(report.unsupported)} unsupported"
        + (f" ({', '.join(c.text for c in report.unsupported[:6])})" if report.unsupported else "")
        + (f" · {len(report.unknown_chunk_ids)} unknown chunk id(s)" if report.unknown_chunk_ids else "")
    )
    return ToolOutput(response=report.to_dict(), summary=summary)


# ---------------------------------------------------------------- ADK-facing functions


def build_tools(deps: ToolDeps) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
    """The four tools as ADK function tools bound to ``deps`` (names/docstrings are the model-facing contract)."""

    async def search_procurement_corpus(
        query: str, solicitation_id: str = "", element_types: Optional[list[str]] = None, tool_context: ToolContext = None
    ) -> dict:
        """Search the procurement corpus (RFPs, SOWs, CLIN pricing tables, SLA matrices, architecture diagrams, amendments).

        Hybrid vector + keyword retrieval with a cross-encoder rerank; returns the top chunks with chunk_id, document,
        page, section, element type, amendment metadata and text. Call it once per solicitation for multi-solicitation
        questions.

        Args:
            query: What to look for, e.g. 'CLIN-1004 Active Lakehouse Storage annual rate'.
            solicitation_id: Optional filter such as 'solicitation_01'; empty searches all solicitations.
            element_types: Optional subset of ['narrative_text', 'table', 'diagram'] to keep.
        """
        args = {"query": query, "solicitation_id": solicitation_id, "element_types": element_types}
        return await _invoke(deps, SEARCH_TOOL, tool_context, args, lambda: run_search(deps, query, solicitation_id, element_types))

    async def check_amendments(solicitation_id: str, section_or_clin_id: str, tool_context: ToolContext = None) -> dict:
        """Look up amendments that supersede a clause or CLIN (latest amendment first) with the superseding text.

        Args:
            solicitation_id: Solicitation of the clause, e.g. 'solicitation_01'.
            section_or_clin_id: Clause or CLIN identifier, e.g. 'CLIN-0004-AB', 'SEC-4.2.1', 'DATA-SLA-3.1'.
        """
        args = {"solicitation_id": solicitation_id, "section_or_clin_id": section_or_clin_id}
        return await _invoke(deps, AMENDMENTS_TOOL, tool_context, args, lambda: run_check_amendments(deps, solicitation_id, section_or_clin_id))

    async def calculate_contract_pricing(expression: str, variables: Optional[dict[str, float]] = None, tool_context: ToolContext = None) -> dict:
        """Evaluate contract arithmetic exactly: numbers, named variables, + - * / and parentheses only.

        Args:
            expression: Arithmetic such as 'hours * rate * (1 - discount)'.
            variables: Values for every name used in the expression, e.g. {'hours': 2000, 'rate': 245, 'discount': 0.05}.
        """
        args = {"expression": expression, "variables": variables}
        return await _invoke(deps, CALCULATOR_TOOL, tool_context, args, lambda: run_calculator(deps, expression, variables))

    async def verify_citations(draft_answer: str, cited_chunk_ids: list[str], tool_context: ToolContext = None) -> dict:
        """Check every number and clause id in a draft answer against the text of the chunks it cites.

        Returns unsupported claims (not found in the cited chunks and not produced by calculate_contract_pricing) and
        cited ids that no tool returned in this conversation.

        Args:
            draft_answer: The full draft answer text.
            cited_chunk_ids: chunk_id values (from tool results) that support the draft.
        """
        args = {"draft_answer": draft_answer, "cited_chunk_ids": cited_chunk_ids}
        return await _invoke(deps, VERIFY_TOOL, tool_context, args, lambda: run_verify(deps, draft_answer, cited_chunk_ids))

    return [search_procurement_corpus, check_amendments, calculate_contract_pricing, verify_citations]


__all__ = [
    "AMENDMENTS_TOOL", "CALCULATOR_TOOL", "ELEMENT_TYPES", "SEARCH_TOOL", "TOOL_NAMES", "ToolArgumentError", "ToolDeps",
    "ToolOutput", "VERIFY_TOOL", "amendment_row_to_chunk", "build_tools", "run_amendment_query", "validate_amendment_args",
    "validate_search_args", "verify_draft",
]
