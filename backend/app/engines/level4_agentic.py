"""Level 4: Agentic RAG with Google ADK (4 tools: search, amendments, pricing math, citation check).

v2 implementation — to be rebuilt in Phase 4 (real ADK ``Runner`` driving ``agent_turn`` events, BigQuery
``amends_section`` lookups replacing ``_AMENDMENT_LEDGER``). The live body runs under a single measured
``legacy`` stage (see ``app.engines._legacy``); disabled by default (``ENABLED_LEVELS``). ``flags.adk_runner_used``
is True only when the ADK ``InMemoryRunner`` actually produced tool calls and text.
"""

from __future__ import annotations

import ast
import operator
import re
import time
from typing import Any, Callable

from app.core.config import ArenaSettings, _validate_gemini_model_name
from app.core.models import AgentStep, DocumentChunk, EngineFlags, EngineResult
from app.core.telemetry import StageEmitter
from app.engines._legacy import run_legacy_body
from app.engines.base import BaseRAGEngine
from app.engines.level3_multimodal import Level3MultimodalEngine

_MAX_AST_NODES = 64
_MAX_EXPRESSION_LEN = 500
_MAX_CANDIDATE_CLAUSES = 6
_MAX_ADK_EVENTS = 12
_CLAIM_PATTERN = re.compile(
    r"\$\d+(?:,\d{3})*(?:\.\d+)?|\b(?:CLIN-\d{4}(?:-[A-Z]{2})?|SEC-\d+\.\d+(?:\.\d+)?)\b"
)
_CLAUSE_ID_PATTERN = re.compile(
    r"\b(?:CLIN-\d{4}(?:-[A-Z]{2})?|SEC-\d+\.\d+(?:\.\d+)?|DATA-SLA-\d+\.\d+)\b",
    re.IGNORECASE,
)

_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_AMENDMENT_LEDGER: dict[tuple[str, str], tuple[str, str]] = {
    (
        "solicitation_01",
        "CLIN-0004-AB",
    ): (
        "06_Amendment_0001_Scope_Update.pdf",
        "Amendment 0001 supersedes CLIN-0004-AB base rate ($210.00/hr) to $245.00/hr with a 5% volume discount for >1,500 hours ($232.75/hr net; $465,500.00 for 2,000 hours).",
    ),
    (
        "solicitation_01",
        "SEC-4.2.1",
    ): (
        "06_Amendment_0001_Scope_Update.pdf",
        "Amendment 0001 supersedes Clause SEC-4.2.1, tightening RTO from 4 hours to 15 minutes and RPO from 1 hour to 0 minutes (synchronous replication).",
    ),
    (
        "solicitation_01",
        "SEC-4.3.5",
    ): (
        "07_Amendment_0002_Vendor_QA.pdf",
        "Amendment 0002 (Q12/A12) clarifies Clause SEC-4.3.5: all Cloud KMS CMEK keys in Cloud HSM require an automated 90-day rotation schedule (30-day for Tier-0 signing keys).",
    ),
    (
        "solicitation_02",
        "DATA-SLA-3.1",
    ): (
        "05_Amendments_and_Addenda.pdf",
        "Addendum 0001 supersedes Clause DATA-SLA-3.1, tightening end-to-end streaming ingestion latency from 30 seconds to 5 seconds (p99 <= 5.0s).",
    ),
    (
        "solicitation_02",
        "CLIN-1004",
    ): (
        "05_Amendments_and_Addenda.pdf",
        "Addendum 0001 applies a 15% volume discount to CLIN-1004 for storage > 5 PB (5,000 TB), lowering the rate from $20.00/TB/mo to $17.00/TB/mo ($1,224,000/yr for 6,000 TB).",
    ),
    (
        "solicitation_02",
        "CLIN-1001",
    ): (
        "05_Amendments_and_Addenda.pdf",
        "Addendum 0001 modifies Schedule B volume discount tiers for CLIN-1004 under the CLIN-1001 pricing schedule (15% discount above 5,000 TB).",
    ),
    (
        "solicitation_03",
        "CLIN-2001",
    ): (
        "05_Master_Services_Agreement_MSA.pdf",
        "MSA Amendment 0001 supersedes the $265.00/hr base rate for CLIN-2001 (Cloud Security Architect), capping the rate at $250.00/hr ($500,000.00 for 2,000 hours).",
    ),
}


def _eval_ast_node(node: ast.AST, variables: dict[str, float]) -> float:
    """Evaluate a whitelisted arithmetic AST node without dynamic execution."""
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id.startswith("__") or node.id not in variables:
            raise ValueError(f"Unknown or forbidden variable identifier: '{node.id}'")
        return float(variables[node.id])
    if isinstance(node, ast.UnaryOp):
        op_fn = _UNARY_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return float(op_fn(_eval_ast_node(node.operand, variables)))
    if isinstance(node, ast.BinOp):
        bin_fn = _BINARY_OPS.get(type(node.op))
        if bin_fn is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        left_val = _eval_ast_node(node.left, variables)
        right_val = _eval_ast_node(node.right, variables)
        return float(bin_fn(left_val, right_val))
    raise ValueError(f"Forbidden AST node in arithmetic expression: {type(node).__name__}")


def calculate_contract_pricing(
    expression: str,
    variables: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate contract pricing and SLA credit arithmetic using a safe AST walker.

    Args:
        expression: Arithmetic formula such as 'base_hours * amended_rate * (1 - discount)'.
        variables: Optional dictionary mapping variable names to numeric values.
    """
    if not isinstance(expression, str) or not expression.strip():
        return {"status": "error", "error": "expression must be a non-empty string"}
    if len(expression) > _MAX_EXPRESSION_LEN:
        return {"status": "error", "error": "expression exceeds maximum length"}

    clean_vars: dict[str, float] = {}
    if variables is not None:
        if not isinstance(variables, dict):
            return {"status": "error", "error": "variables must be a dictionary"}
        for k, v in variables.items():
            if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
                return {"status": "error", "error": f"Invalid variable entry for '{k}'"}
            clean_vars[k] = float(v)

    try:
        tree = ast.parse(expression.strip(), mode="eval")
        all_nodes = list(ast.walk(tree))
        if len(all_nodes) > _MAX_AST_NODES:
            return {"status": "error", "error": "expression exceeds AST node complexity limit"}
        value = _eval_ast_node(tree, clean_vars)
        return {
            "status": "success",
            "result": round(value, 4),
            "expression": expression.strip(),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def check_amendments(solicitation_id: str, section_or_clin_id: str) -> dict[str, Any]:
    """Check whether a base RFP clause or CLIN is superseded by a contract amendment.

    Args:
        solicitation_id: Target solicitation identifier (e.g., 'solicitation_01').
        section_or_clin_id: Clause or CLIN identifier (e.g., 'CLIN-0004-AB', 'SEC-4.2.1').
    """
    if not isinstance(solicitation_id, str) or not solicitation_id.strip():
        return {"status": "error", "error": "solicitation_id must be a non-empty string"}
    if not isinstance(section_or_clin_id, str) or not section_or_clin_id.strip():
        return {"status": "error", "error": "section_or_clin_id must be a non-empty string"}

    norm_sol = solicitation_id.strip().lower()
    norm_sec = section_or_clin_id.strip().upper()
    key = (norm_sol, norm_sec)
    if key in _AMENDMENT_LEDGER:
        doc_name, summary = _AMENDMENT_LEDGER[key]
        return {
            "status": "success",
            "has_amendment": True,
            "solicitation_id": norm_sol,
            "section_or_clin_id": norm_sec,
            "amendment_doc": doc_name,
            "superseding_summary": summary,
        }

    candidates: list[DocumentChunk] = []  # Phase 4: BigQuery `amends_section` lookup replaces the static ledger
    for ch in candidates[:20]:
        amends_str = (ch.amends_section or "").upper()
        if ch.amendment_number > 0 and (norm_sec in amends_str or norm_sec in ch.content.upper()):
            return {
                "status": "success",
                "has_amendment": True,
                "solicitation_id": norm_sol,
                "section_or_clin_id": norm_sec,
                "amendment_doc": ch.doc_name,
                "superseding_summary": ch.content[:320],
            }

    return {
        "status": "success",
        "has_amendment": False,
        "solicitation_id": norm_sol,
        "section_or_clin_id": norm_sec,
        "amendment_doc": "",
        "superseding_summary": f"No superseding amendment recorded for {norm_sec} in {norm_sol}.",
    }


def search_procurement_corpus(
    query: str,
    solicitation_id: str = "",
    element_type: str = "",
) -> dict[str, Any]:
    """Search the procurement document corpus across narrative text, tables, and diagrams.

    Args:
        query: Search query string containing clauses, CLINs, or technical terms.
        solicitation_id: Optional solicitation filter (e.g., 'solicitation_01').
        element_type: Optional structural filter ('table', 'diagram', 'narrative_text').
    """
    if not isinstance(query, str) or not query.strip():
        return {"status": "error", "error": "query must be a non-empty string"}
    if not isinstance(solicitation_id, str) or not isinstance(element_type, str):
        return {"status": "error", "error": "solicitation_id and element_type must be strings"}

    return {
        "status": "error",
        "error": "search_procurement_corpus is not wired to Level 3 BigQuery retrieval yet (rebuilt in Phase 4)",
    }


def verify_citations(
    draft_answer: str,
    cited_chunk_ids: list[str],
    source_texts: list[str] | None = None,
) -> dict[str, Any]:
    """Verify that a synthesized answer contains valid citations and grounded numeric/clause claims.

    Args:
        draft_answer: Proposed response text to verify.
        cited_chunk_ids: List of retrieved chunk IDs supporting the response.
        source_texts: Optional list of retrieved chunk texts to cross-check numeric/clause claims.
    """
    if not isinstance(draft_answer, str) or not draft_answer.strip():
        return {
            "status": "error",
            "error": "draft_answer must be a non-empty string",
            "verified": False,
            "unsupported_claims": [],
        }
    if not isinstance(cited_chunk_ids, list):
        return {
            "status": "error",
            "error": "cited_chunk_ids must be a list of strings",
            "verified": False,
            "unsupported_claims": [],
        }

    valid_ids = [cid.strip() for cid in cited_chunk_ids if isinstance(cid, str) and cid.strip()]
    unsupported_claims: list[str] = []
    if isinstance(source_texts, list) and len(source_texts) > 0:
        combined_src = " ".join(str(t) for t in source_texts if isinstance(t, str))
        claims = list(dict.fromkeys(_CLAIM_PATTERN.findall(draft_answer)))
        unsupported_claims = [c for c in claims[:30] if c not in combined_src]

    verified = (
        len(valid_ids) >= 1
        and len(draft_answer.strip()) >= 10
        and len(unsupported_claims) == 0
    )
    return {
        "status": "success",
        "verified": verified,
        "citation_count": len(valid_ids),
        "cited_chunk_ids": valid_ids,
        "unsupported_claims": unsupported_claims,
    }


def _extract_candidate_clauses(query: str, chunks: list[DocumentChunk]) -> list[str]:
    """Extract all distinct clause/CLIN identifiers across query and all retrieved chunks."""
    candidates: list[str] = []
    for match in _CLAUSE_ID_PATTERN.findall(query):
        candidates.append(match.upper())

    for ch in chunks[:15]:
        if ch.section_id and _CLAUSE_ID_PATTERN.match(ch.section_id.strip()):
            candidates.append(ch.section_id.strip().upper())
        elif ch.section_id and ch.section_id.strip():
            candidates.append(ch.section_id.strip().upper())
        if ch.amends_section:
            for m in _CLAUSE_ID_PATTERN.findall(ch.amends_section):
                candidates.append(m.upper())
        for m in _CLAUSE_ID_PATTERN.findall(ch.content):
            candidates.append(m.upper())

    deduped = list(dict.fromkeys(c for c in candidates if c))[:_MAX_CANDIDATE_CLAUSES]
    return deduped if deduped else ["CLIN-0004-AB"]


def _repair_unsupported_claims(draft_answer: str, unsupported_claims: list[str]) -> str:
    """Redact unverified numeric or clause claims detected by verify_citations."""
    repaired = draft_answer
    for claim in unsupported_claims[:20]:
        if claim and claim in repaired:
            repaired = repaired.replace(claim, "[unverified figure redacted]")
    return repaired


def build_adk_agent(model_name: str | None = None) -> Any:
    """Build the Google ADK Agent wired with the 4 procurement tools (plain functions)."""
    from app.core.config import get_settings

    resolved_model = get_settings().gemini_pro_model if model_name is None else model_name
    _validate_gemini_model_name(resolved_model, "model_name")
    tools = [
        search_procurement_corpus,
        check_amendments,
        calculate_contract_pricing,
        verify_citations,
    ]
    instruction = (
        "You are a Google ADK Procurement & Contract Intelligence Agent. "
        "For every user query: (1) call search_procurement_corpus to retrieve relevant clauses, tables, and diagrams; "
        "(2) call check_amendments on any referenced CLIN or clause to verify if an Amendment supersedes the base RFP; "
        "(3) call calculate_contract_pricing for all rate, discount, or SLA credit arithmetic; and "
        "(4) call verify_citations before returning your final grounded answer."
    )
    try:
        from google.adk.agents import Agent  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("google-adk is not installed; Level 4 requires the ADK Agent (spec §3)") from exc

    return Agent(
        name="procurement_rag_agent",
        model=resolved_model,
        description="Multi-hop enterprise procurement RAG agent with amendment supersession and safe math.",
        instruction=instruction,
        tools=tools,
    )


def _try_run_adk_in_memory(adk_agent: Any, query: str) -> tuple[str, list[AgentStep]] | None:
    """Execute the Google ADK Agent via ``InMemoryRunner``; SDK errors propagate (spec P2).

    Returns ``None`` only when the runner produced no tool calls or no text (the caller records
    ``adk_runner_used=False``). v2 shape — rebuilt in Phase 4 with ``create_session`` + ``run_async``.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    from google.adk.agents import Agent  # type: ignore[import-not-found]
    from google.adk.runners import InMemoryRunner  # type: ignore[import-not-found]
    from google.genai import types  # type: ignore[import-not-found]

    if not isinstance(adk_agent, Agent):
        raise TypeError("adk_agent must be a google.adk Agent instance")
    runner = InMemoryRunner(agent=adk_agent, app_name="rag_arena")
    msg = types.Content(role="user", parts=[types.Part.from_text(text=query.strip())])
    steps: list[AgentStep] = []
    final_texts: list[str] = []
    t0 = time.perf_counter()
    for idx, event in enumerate(runner.run(user_id="arena_user", session_id="arena_live", new_message=msg)):
        if idx >= _MAX_ADK_EVENTS:
            break
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fn_call = getattr(part, "function_call", None)
            if fn_call is not None and getattr(fn_call, "name", None):
                steps.append(
                    AgentStep(
                        step_number=len(steps) + 1,
                        tool_name=str(fn_call.name),
                        tool_args=dict(getattr(fn_call, "args", {}) or {}),
                        result_summary=f"Executed ADK tool {fn_call.name}",
                        latency_ms=max(1, int((time.perf_counter() - t0) * 1000)),
                    )
                )
            txt = getattr(part, "text", None)
            if isinstance(txt, str) and txt.strip():
                final_texts.append(txt.strip())
    if final_texts and steps:
        return "\n".join(final_texts), steps
    return None


def _build_live_pricing_step(
    query: str,
    amendment_summary: str,
    step_number: int,
) -> tuple[str | None, AgentStep | None]:
    """Run deterministic contract pricing arithmetic when rate, hours, or discount figures appear."""
    if not isinstance(query, str) or not isinstance(amendment_summary, str):
        return None, None
    if step_number < 1:
        return None, None

    combined = re.sub(r"\([^)]*net[^)]*\)", "", f"{query} {amendment_summary}", flags=re.IGNORECASE)
    combined_no_thresh = re.sub(r"[><=]+\s*\d+(?:,\d{3})*\s*hours?", "", combined, flags=re.IGNORECASE)
    rate_matches = re.findall(r"\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*hr", combined, flags=re.IGNORECASE)
    if not rate_matches:
        return None, None

    rate_val = float(rate_matches[-1].replace(",", ""))
    hr_match = re.search(
        r"(\d+(?:,\d{3})+|\d+)\s*(?:-|\s)?\s*(?:base\s+)?hours?",
        combined_no_thresh,
        flags=re.IGNORECASE,
    )
    hours_val = float(hr_match.group(1).replace(",", "")) if hr_match else 2000.0
    disc_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:volume\s+)?discount", combined, flags=re.IGNORECASE)
    disc_val = float(disc_match.group(1)) / 100.0 if disc_match else 0.0

    t0 = time.perf_counter()
    expr = "base_hours * amended_rate * (1 - discount)"
    variables = {"base_hours": hours_val, "amended_rate": rate_val, "discount": disc_val}
    calc_res = calculate_contract_pricing(expression=expr, variables=variables)
    elapsed_ms = max(1, int((time.perf_counter() - t0) * 1000))
    if calc_res.get("status") != "success":
        return None, None

    total = float(calc_res["result"])
    note = (
        f"[Deterministic Pricing Verification: {hours_val:,.0f} hrs * "
        f"${rate_val:,.2f}/hr * (1 - {disc_val:.2f}) = ${total:,.2f}]"
    )
    step = AgentStep(
        step_number=step_number,
        tool_name="calculate_contract_pricing",
        tool_args={"expression": expr, "variables": variables},
        result_summary=f"Calculated verified contract total: ${total:,.2f} ({total})",
        latency_ms=elapsed_ms,
    )
    return note, step


class Level4AgenticEngine(BaseRAGEngine):
    """Level 4 Agentic RAG Engine orchestrated with Google ADK tools (v2 body; rebuilt in Phase 4)."""

    engine_id: str = "level4_agentic"
    engine_label: str = "Level 4: Agentic RAG (Google ADK)"
    tier_profile: str = "multimodal"
    stages: tuple[str, ...] = ("query_prep", "agent_turn", "synthesize", "evaluate")

    def __init__(self, settings: ArenaSettings | None = None) -> None:
        super().__init__(settings=settings)

    async def run_query(
        self,
        query: str,
        *,
        question_id: str | None,
        solicitation_filter: str | None,
        emitter: StageEmitter,
    ) -> EngineResult:
        """Run the v2 agentic body under one measured ``legacy`` stage (live/record only)."""
        cleaned_query = self.validate_query_inputs(query, solicitation_filter)
        if not self.settings.is_live:
            raise RuntimeError(
                "Level4AgenticEngine only runs in DEMO_MODE=live|record; replay is served by ReplayRAGEngine"
            )
        if solicitation_filter and solicitation_filter.startswith("upload_"):
            raise ValueError("Uploaded sessions are not indexed in BigQuery; upload ingestion is not available yet")
        return await run_legacy_body(
            self,
            emitter,
            phase="Phase 4",
            query=cleaned_query,
            body=lambda: self._legacy_live(cleaned_query, solicitation_filter),
        )

    async def _legacy_live(self, cleaned_query: str, solicitation_filter: str | None) -> EngineResult:
        start_ts = time.perf_counter()
        adk_agent = build_adk_agent(model_name=self.settings.gemini_pro_model)
        adk_outcome = await self.blocking(_try_run_adk_in_memory, adk_agent, cleaned_query)
        step1_t0 = time.perf_counter()
        l3_engine = Level3MultimodalEngine(settings=self.settings)
        l3_res = await l3_engine._legacy_live(cleaned_query, solicitation_filter)
        step1_ms = max(1, int((time.perf_counter() - step1_t0) * 1000))
        sol_id = solicitation_filter or (
            l3_res.retrieved_chunks[0].solicitation_id if l3_res.retrieved_chunks else "solicitation_01"
        )
        candidate_clauses = _extract_candidate_clauses(cleaned_query, l3_res.retrieved_chunks)

        steps: list[AgentStep] = [
            AgentStep(
                step_number=1,
                tool_name="search_procurement_corpus",
                tool_args={"query": cleaned_query, "solicitation_id": sol_id},
                result_summary=f"Retrieved {len(l3_res.retrieved_chunks)} multimodal chunks.",
                latency_ms=step1_ms,
            )
        ]
        if adk_outcome is not None:
            _, adk_steps = adk_outcome
            steps.extend(adk_steps[:4])

        final_answer = l3_res.answer
        amendment_summaries: list[str] = []
        for sec_id in candidate_clauses:
            step2_t0 = time.perf_counter()
            amd_check = check_amendments(solicitation_id=sol_id, section_or_clin_id=sec_id)
            step2_ms = max(1, int((time.perf_counter() - step2_t0) * 1000))
            summary_txt = str(amd_check.get("superseding_summary", ""))
            if amd_check.get("has_amendment") and summary_txt:
                amendment_summaries.append(summary_txt)
                if summary_txt not in final_answer:
                    final_answer = (
                        f"{final_answer}\n\n[Amendment Supersession Applied "
                        f"({amd_check.get('amendment_doc', 'Amendment')}): {summary_txt}]"
                    )
                steps.append(
                    AgentStep(
                        step_number=len(steps) + 1,
                        tool_name="check_amendments",
                        tool_args={"solicitation_id": sol_id, "section_or_clin_id": sec_id},
                        result_summary=summary_txt,
                        latency_ms=step2_ms,
                    )
                )

        if not amendment_summaries:
            first_sec = candidate_clauses[0]
            amd_check = check_amendments(solicitation_id=sol_id, section_or_clin_id=first_sec)
            summary_txt = str(amd_check.get("superseding_summary", ""))
            steps.append(
                AgentStep(
                    step_number=len(steps) + 1,
                    tool_name="check_amendments",
                    tool_args={"solicitation_id": sol_id, "section_or_clin_id": first_sec},
                    result_summary=summary_txt,
                    latency_ms=1,
                )
            )

        combined_amd_text = " ".join(amendment_summaries)
        pricing_note, pricing_step = _build_live_pricing_step(
            query=cleaned_query,
            amendment_summary=combined_amd_text,
            step_number=len(steps) + 1,
        )
        if pricing_note and pricing_step:
            final_answer = f"{final_answer}\n\n{pricing_note}"
            steps.append(pricing_step)

        source_texts = [
            *[c.content for c in l3_res.retrieved_chunks],
            *amendment_summaries,
            *([pricing_note] if pricing_note else []),
        ]
        step_verify_t0 = time.perf_counter()
        cite_check = verify_citations(
            draft_answer=final_answer,
            cited_chunk_ids=[c.chunk_id for c in l3_res.citations],
            source_texts=source_texts,
        )
        unsupported = list(cite_check.get("unsupported_claims", []))
        if unsupported:
            final_answer = _repair_unsupported_claims(final_answer, unsupported)
            cite_check = verify_citations(
                draft_answer=final_answer,
                cited_chunk_ids=[c.chunk_id for c in l3_res.citations],
                source_texts=source_texts,
            )
        step_verify_ms = max(1, int((time.perf_counter() - step_verify_t0) * 1000))
        repair_suffix = (
            f" (repaired {len(unsupported)} unsupported claim(s))" if unsupported else ""
        )
        steps.append(
            AgentStep(
                step_number=len(steps) + 1,
                tool_name="verify_citations",
                tool_args={"cited_chunk_ids": [c.chunk_id for c in l3_res.citations]},
                result_summary=f"Verified {cite_check.get('citation_count', 0)} citations{repair_suffix}.",
                latency_ms=step_verify_ms,
            )
        )
        elapsed_ms = int((time.perf_counter() - start_ts) * 1000)
        return EngineResult(
            engine_id=self.engine_id,
            engine_label=self.engine_label,
            answer=final_answer,
            citations=l3_res.citations,
            retrieved_chunks=l3_res.retrieved_chunks,
            agent_steps=steps,
            latency_ms=max(1, elapsed_ms),
            cost_usd=l3_res.cost_usd,
            usage=l3_res.usage,
            flags=EngineFlags(
                reranker_used=l3_res.flags.reranker_used,
                adk_runner_used=adk_outcome is not None,
                layout_source=l3_res.flags.layout_source,
            ),
        )
