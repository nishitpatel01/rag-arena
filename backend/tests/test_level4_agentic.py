"""Level 4 (Google ADK agent) tests — offline: ADK runner driven by a stub LLM or a fake event stream, BigQuery /
Vertex / Ranking API faked. Covers the AST calculator, the parameterized amendment lookup, claim verification, live
``agent_turn`` telemetry, bounds (turns, tool calls per turn, timeout), no fallback, citation restriction, the Claude
agent-model 400 and the architecture descriptor."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import patch

import httpx
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
import pytest
from starlette.testclient import TestClient

from app.core.architectures import get_architecture_descriptor
from app.core.models_catalog import load_catalog
from app.core.pricing import cost_from_usage, rank_cost_usd
from app.core.ranking import RankingClient
from app.core.telemetry import RecordingStageEmitter
from app.engines import build_engine
from app.engines.agent_calculator import CalculatorError, evaluate_expression
from app.engines.agent_ledger import AgentLedger
from app.engines.agent_runtime import (
    AGENT_INSTRUCTION,
    AGENT_NAME,
    AgentLimitError,
    AgentLimits,
    AgentModelNotSupportedError,
    AgentProtocolError,
    AgentTimeoutError,
    run_agent,
)
from app.engines.agent_tools import ToolDeps, run_amendment_query, run_check_amendments, verify_draft
from app.engines.agent_verify import check_claims, extract_claims
from app.engines.level3_multimodal import Level3MultimodalEngine
from app.engines.level4_agentic import Level4AgenticEngine, resolve_citations
from app.ingestion.bq_indexer import build_amendment_lookup_sql
from app.main import create_app
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run

_AMD_ID = "sol01_amd1_e1_multimodal"
_PRICE_ID = "sol01_pricing_t1_multimodal"
_BBOX = {"page": 1, "x0": 0.07, "y0": 0.15, "x1": 0.76, "y1": 0.32}


def _row(cid: str, content: str, *, amendment: int = 0, amends: str | None = None, element: str = "table", rank: int = 1) -> dict[str, Any]:
    return {
        "chunk_id": cid, "solicitation_id": "solicitation_01", "doc_name": "06_Amendment_0001_Scope_Update.pdf" if amendment else "04_Pricing_Schedule_CLINs.pdf",
        "doc_type": "AMENDMENT" if amendment else "PRICING_CLIN", "element_type": element, "section_id": "AMD-0001" if amendment else "CLIN-0004-AB",
        "amendment_number": amendment, "effective_date": "2026-03-15" if amendment else "2026-01-15", "amends_section": amends,
        "page_number": 1, "bbox": _BBOX, "content": content, "layout_source": "documentai_layout", "bbox_source": "pdf_text_layer",
        "vector_rank": rank, "vector_distance": 0.1 * rank, "lexical_rank": rank, "lexical_score": 3, "rrf_score": round(2 / (60 + rank), 6),
    }


_PRICE_ROW = _row(_PRICE_ID, "| CLIN | Rate | Hours |\n| CLIN-0004-AB | $210.00/hr | 2,000 |", rank=2)
_AMD_ROW = _row(
    _AMD_ID, "Amendment 0001 supersedes CLIN-0004-AB: rate $245.00/hr with a 5% volume discount above 1,500 hours.",
    amendment=1, amends="SEC-4.2.1,CLIN-0004-AB", element="narrative_text", rank=1,
)


class _RoutedBQ(FakeBigQueryClient):
    """RRF statements get the search rows; the amendment lookup gets the amendment rows."""

    def __init__(self, fail_amendments: bool = False) -> None:
        super().__init__(rows=[_AMD_ROW, _PRICE_ROW])
        self.fail_amendments = fail_amendments

    def query(self, sql: str, job_config: Any = None) -> Any:
        if "UNNEST(SPLIT" in sql:
            self.queries.append((sql, job_config))
            if self.fail_amendments:
                raise RuntimeError("403 Access Denied: BigQuery fake failure")
            return type(self).__mro__[1].query.__get__(FakeBigQueryClient(rows=[_AMD_ROW]))(sql, job_config)
        return super().query(sql, job_config)


def _ranking() -> RankingClient:
    def rank_post(url: str, **kw: Any) -> httpx.Response:
        ids = [r["id"] for r in kw["json"]["records"]]
        return httpx.Response(200, json={"records": [{"id": rid, "score": 0.9 - 0.1 * i} for i, rid in enumerate(ids)]}, request=httpx.Request("POST", url))

    return RankingClient(project_id="test-project", model="semantic-ranker-default@latest", attempt_timeout_seconds=5.0, budget_seconds=10.0,
                         token_provider=lambda: "tok", sleep=lambda s: None, post=rank_post)


# ---------------------------------------------------------------- stub LLM driving a REAL InMemoryRunner


def _call(name: str, args: dict[str, Any], cid: str) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args, id=cid))


_DRAFT_BAD = "CLIN-0004-AB is amended to $245.00/hr; 2,000 hours cost $465,500.00 with a cap of $999.00."
_DRAFT_OK = "CLIN-0004-AB is amended to $245.00/hr (Amendment 0001); 2,000 hours cost $465,500.00."
_SCRIPT: list[list[types.Part]] = [
    [_call("search_procurement_corpus", {"query": "CLIN-0004-AB amended rate", "solicitation_id": "solicitation_01"}, "fc1")],
    [_call("check_amendments", {"solicitation_id": "solicitation_01", "section_or_clin_id": "CLIN-0004-AB"}, "fc2"),
     _call("calculate_contract_pricing", {"expression": "hours * rate * (1 - discount)", "variables": {"hours": 2000, "rate": 245, "discount": 0.05}}, "fc3")],
    [_call("verify_citations", {"draft_answer": _DRAFT_BAD, "cited_chunk_ids": [_AMD_ID, _PRICE_ID]}, "fc4")],
    [_call("verify_citations", {"draft_answer": _DRAFT_OK, "cited_chunk_ids": [_AMD_ID, _PRICE_ID]}, "fc5")],
    [types.Part(text=f"{_DRAFT_OK}\nCITED_CHUNK_IDS: {_AMD_ID}, {_PRICE_ID}, bogus_chunk_id")],
]


class StubLlm(BaseLlm):
    """Plays a fixed script of model turns (one per LLM call); records how many calls ADK made."""

    model: str = "stub-gemini"
    script: list[Any] = []
    calls: int = 0
    delay_s: float = 0.0
    loop_forever: bool = False

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        parts = self.script[0] if self.loop_forever else self.script[min(self.calls, len(self.script)) - 1]
        if self.loop_forever:
            parts = [_call(p.function_call.name, dict(p.function_call.args), f"loop{self.calls}") for p in parts]
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10, thoughts_token_count=5)
        yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage)


StubLlm.model_rebuild(_types_namespace={"Any": Any, "types": types})  # the offline shim loads tests outside sys.modules


def _stub_builder(llm: StubLlm):
    def build(spec: Any, settings: Any, tools: list[Any]) -> LlmAgent:
        return LlmAgent(name=AGENT_NAME, model=llm, instruction=AGENT_INSTRUCTION, tools=tools)

    return build


def _run_engine(llm: StubLlm, *, bq: _RoutedBQ | None = None, synthesis: str = "gemini-4-argon", **overrides: Any):
    settings = live_settings(enabled_levels=("level4_agentic",), **overrides)
    spec = load_catalog(settings).get(synthesis)
    emitter = RecordingStageEmitter(run_id="run_l4", engine="level4_agentic")
    genai, bq = FakeGenaiClient(), bq or _RoutedBQ()
    with fake_clients(genai, bq), patch("app.engines.level4_agentic.get_genai_client", lambda s, location=None: genai), \
            patch("app.engines.level4_agentic.get_bigquery_client", lambda s: bq), \
            patch("app.engines.level4_agentic.build_agent", _stub_builder(llm)):
        retriever = Level3MultimodalEngine(settings=settings, ranking_client=_ranking())
        engine = Level4AgenticEngine(settings=settings, synthesis_model=spec, retriever=retriever)
        try:
            result = run(engine.run_query("What is the amended CLIN-0004-AB rate and 2,000-hour cost?", question_id="q",
                                          solicitation_filter=None, emitter=emitter))
        except Exception as exc:  # returned for fail-loud assertions
            return exc, emitter, bq
    return result, emitter, bq


def test_real_in_memory_runner_drives_tools_and_emits_agent_turn_per_adk_event_in_order() -> None:
    llm = StubLlm(script=_SCRIPT)
    result, emitter, bq = _run_engine(llm)
    assert not isinstance(result, Exception), result
    kinds = [(e["event"], e.get("stage") or e.get("kind"), e.get("tool_name")) for e in emitter.events]
    assert kinds[:3] == [("stage_started", "query_prep", None), ("stage_completed", "query_prep", None), ("stage_started", "agent_turn", None)]
    turns = [(k, t) for ev, k, t in kinds if ev == "agent_turn"]
    assert turns == [
        ("model", None), ("tool_call", "search_procurement_corpus"), ("tool_result", "search_procurement_corpus"),
        ("model", None), ("tool_call", "check_amendments"), ("tool_call", "calculate_contract_pricing"),
        ("tool_result", "check_amendments"), ("tool_result", "calculate_contract_pricing"),
        ("model", None), ("tool_call", "verify_citations"), ("tool_result", "verify_citations"),
        ("model", None), ("tool_call", "verify_citations"), ("tool_result", "verify_citations"),
        ("model", None),
    ]
    assert kinds[-3:] == [("stage_completed", "agent_turn", None), ("stage_started", "synthesize", None), ("stage_completed", "synthesize", None)]
    agent_events = [e for e in emitter.events if e["event"] == "agent_turn"]
    assert [e["seq"] for e in agent_events] == list(range(1, 16))
    assert agent_events[0]["tokens"] == {"prompt": 100, "output": 10, "thinking": 5, "total": 115}
    search_result = agent_events[2]
    assert search_result["duration_ms"] is not None and "2 chunks of 2/2 RRF candidates" in search_result["tool_result_summary"]
    assert "governing: Amendment 0001 (effective 2026-03-15)" in agent_events[6]["tool_result_summary"]
    assert agent_events[7]["tool_result_summary"].endswith("= 465500.0") and agent_events[1]["tool_args"]["solicitation_id"] == "solicitation_01"
    assert llm.calls == 5


def test_agent_turn_and_synthesize_payload_contract_and_result() -> None:
    result, emitter, _ = _run_engine(StubLlm(script=_SCRIPT))
    done = {e["stage"]: e for e in emitter.events if e["event"] == "stage_completed"}
    agent = done["agent_turn"]["payload"]
    assert agent["turns"] == 5 and agent["tool_calls"] == 5
    assert agent["tool_calls_by_name"] == {"search_procurement_corpus": 1, "check_amendments": 1, "calculate_contract_pricing": 1, "verify_citations": 2}
    assert (agent["unsupported_claims_before"], agent["unsupported_claims_after"], agent["repair_turns"]) == (1, 0, 1)
    assert agent["unsupported_before_list"] == ["$999.00"] and agent["final_answer_check"]["unsupported_claims"] == 0
    assert agent["final_answer_source"] == "adk_final_response" and agent["rejected_citation_ids"] == ["bogus_chunk_id"]
    assert agent["citations"] == [_AMD_ID, _PRICE_ID] and agent["calculator_values"] == [465500.0]
    assert agent["retrieval_usage"]["search_calls"] == 1 and agent["retrieval_usage"]["bigquery_jobs"] == 2
    synth = done["synthesize"]["payload"]
    assert (synth["prompt_tokens"], synth["output_tokens"], synth["thinking_tokens"], synth["turns"]) == (500, 50, 25, 5)
    assert synth["answer"] == _DRAFT_OK and synth["final_answer_source"] == "adk_final_response" and synth["final_turn"]["turn_index"] == 5
    started = {e["stage"]: e for e in emitter.events if e["event"] == "stage_started"}
    assert started["agent_turn"]["request"]["runner"] == "InMemoryRunner" and started["agent_turn"]["request"]["max_turns"] == 8
    assert started["agent_turn"]["request"]["instruction"].endswith("Request at most 8 tool calls in a single turn.")
    assert result.answer == _DRAFT_OK and [c.chunk_id for c in result.citations] == [_AMD_ID, _PRICE_ID]
    assert {c.chunk_id for c in result.retrieved_chunks} == {_AMD_ID, _PRICE_ID}
    assert result.flags.adk_runner_used and result.flags.reranker_used and result.flags.layout_source == "documentai_layout"
    assert [s.tool_name for s in result.agent_steps][:2] == ["search_procurement_corpus", "check_amendments"]
    assert result.usage.embedding_tokens == 14 and result.usage.prompt_tokens == 500
    assert result.cost_usd == pytest.approx(cost_from_usage(result.usage, "gemini-4-argon") + rank_cost_usd(1))


# ---------------------------------------------------------------- bounds + fail loud (no fallback)


def _failed_agent_turn(emitter: RecordingStageEmitter) -> dict[str, Any]:
    failed = [e for e in emitter.events if e["event"] == "stage_failed"]
    assert [e["stage"] for e in failed] == ["agent_turn"], failed
    assert not any(e.get("stage") == "synthesize" for e in emitter.events)
    return failed[0]


def test_max_turns_is_enforced_and_fails_loud() -> None:
    llm = StubLlm(script=[[_call("calculate_contract_pricing", {"expression": "1 + 1"}, "x")]], loop_forever=True)
    exc, emitter, _ = _run_engine(llm, agent_max_turns=2)
    assert isinstance(exc, Exception) and "2" in str(exc) and llm.calls == 2
    assert _failed_agent_turn(emitter)["error_type"] in ("AgentLimitError", "LlmCallsLimitExceededError")


def test_timeout_is_enforced_and_fails_loud() -> None:
    llm = StubLlm(script=_SCRIPT, delay_s=5.0)
    exc, emitter, _ = _run_engine(llm, query_timeout_seconds=3)
    assert isinstance(exc, AgentTimeoutError), exc
    assert _failed_agent_turn(emitter)["error_type"] == "AgentTimeoutError"


def test_tool_infrastructure_error_fails_the_run_with_no_fallback_answer() -> None:
    exc, emitter, bq = _run_engine(StubLlm(script=_SCRIPT), bq=_RoutedBQ(fail_amendments=True))
    assert isinstance(exc, RuntimeError) and "Access Denied" in str(exc)
    assert "Access Denied" in _failed_agent_turn(emitter)["error"]
    assert not any(e["event"] == "agent_turn" and e["kind"] == "tool_result" and e["tool_name"] == "check_amendments" for e in emitter.events)


def test_final_answer_without_a_tool_returned_citation_fails_loud() -> None:
    script = [_SCRIPT[0], [types.Part(text="The rate is $245.00/hr.\nCITED_CHUNK_IDS: made_up_id")]]
    exc, emitter, _ = _run_engine(StubLlm(script=script))
    assert isinstance(exc, AgentProtocolError) and "made_up_id" in str(exc)
    _failed_agent_turn(emitter)


def _ev(parts: list[types.Part], *, usage: bool = True) -> Event:
    meta = types.GenerateContentResponseUsageMetadata(prompt_token_count=7, candidates_token_count=3, thoughts_token_count=1) if usage else None
    return Event(author=AGENT_NAME, invocation_id="inv", content=types.Content(role="model", parts=parts), usage_metadata=meta)


class _FakeRunner:
    """Fake ``InMemoryRunner``: a scripted ADK event stream (optionally slow or raising)."""

    def __init__(self, events: list[Any], *, delay_s: float = 0.0, raise_exc: Exception | None = None) -> None:
        self._events, self._delay, self._raise = events, delay_s, raise_exc
        self.session_service = SimpleNamespace(create_session=self._create)
        self.sessions_created = 0

    async def _create(self, **kw: Any) -> Any:
        self.sessions_created += 1
        return SimpleNamespace(id="sess-1")

    async def run_async(self, **kw: Any) -> AsyncGenerator[Any, None]:
        assert kw["session_id"] == "sess-1" and kw["run_config"].max_llm_calls >= 1
        for event in self._events:
            await asyncio.sleep(self._delay)
            yield event
        if self._raise:
            raise self._raise


def _run_fake(runner: _FakeRunner, limits: AgentLimits) -> tuple[Any, RecordingStageEmitter]:
    emitter = RecordingStageEmitter(run_id="run_fake", engine="level4_agentic")
    try:
        out = run(run_agent(object(), "q", limits=limits, ledger=AgentLedger("run_fake"), emitter=emitter, runner_factory=lambda **kw: runner))
    except Exception as exc:  # returned for assertions
        return exc, emitter
    return out, emitter


def test_fake_event_stream_is_translated_in_order_and_runner_uses_a_created_session() -> None:
    calc = _call("calculate_contract_pricing", {"expression": "2 * 3"}, "c1")
    resp = types.Part(function_response=types.FunctionResponse(name="calculate_contract_pricing", response={"status": "ok", "value": 6.0}, id="c1"))
    runner = _FakeRunner([_ev([calc]), Event(author=AGENT_NAME, invocation_id="inv", content=types.Content(role="user", parts=[resp])),
                          _ev([types.Part(text="Six.")])])
    out, emitter = _run_fake(runner, AgentLimits(max_turns=4, max_tool_calls_per_turn=2, budget_seconds=5))
    assert runner.sessions_created == 1 and out.final_text == "Six." and out.final_turn_index == 2
    assert [(e["kind"], e["turn_index"]) for e in emitter.events] == [("model", 1), ("tool_call", 1), ("tool_result", 1), ("model", 2)]
    assert '"value": 6.0' in emitter.events[2]["tool_result_summary"] and out.tokens().prompt == 14


def test_fake_stream_bounds_fail_loud() -> None:
    many = [_call("calculate_contract_pricing", {"expression": "1"}, f"c{i}") for i in range(3)]
    exc, _ = _run_fake(_FakeRunner([_ev(many)]), AgentLimits(max_turns=4, max_tool_calls_per_turn=2, budget_seconds=5))
    assert isinstance(exc, AgentLimitError) and "3 tool calls" in str(exc)
    texts = [_ev([_call("calculate_contract_pricing", {"expression": "1"}, f"t{i}")]) for i in range(3)]
    exc, _ = _run_fake(_FakeRunner(texts), AgentLimits(max_turns=2, max_tool_calls_per_turn=2, budget_seconds=5))
    assert isinstance(exc, AgentLimitError) and "AGENT_MAX_TURNS=2" in str(exc)
    exc, _ = _run_fake(_FakeRunner([_ev([types.Part(text="x")])], delay_s=2.0), AgentLimits(max_turns=2, max_tool_calls_per_turn=2, budget_seconds=0.3))
    assert isinstance(exc, AgentTimeoutError)
    exc, _ = _run_fake(_FakeRunner([], raise_exc=RuntimeError("vertex 503")), AgentLimits(max_turns=2, max_tool_calls_per_turn=2, budget_seconds=5))
    assert isinstance(exc, RuntimeError) and str(exc) == "vertex 503"
    exc, _ = _run_fake(_FakeRunner([_ev([calls]) for calls in many[:1]]), AgentLimits(max_turns=2, max_tool_calls_per_turn=2, budget_seconds=5))
    assert isinstance(exc, AgentProtocolError) and "without a final text response" in str(exc)
    exc, _ = _run_fake(_FakeRunner([_ev([types.Part(text="x")], usage=False)]), AgentLimits(max_turns=2, max_tool_calls_per_turn=2, budget_seconds=5))
    assert isinstance(exc, AgentProtocolError) and "usage_metadata" in str(exc)


# ---------------------------------------------------------------- tools: calculator, amendments SQL, verification


def test_ast_calculator_evaluates_contract_math_and_rejects_unsafe_input() -> None:
    res = evaluate_expression("hours * rate * (1 - discount)", {"hours": 2000, "rate": 245, "discount": 0.05})
    assert res.value == 465500.0 and res.normalized_expression == "hours * rate * (1 - discount)"
    assert res.substituted_expression == "2000.0 * 245.0 * (1 - 0.05)" and dict(res.variables) == {"discount": 0.05, "hours": 2000.0, "rate": 245.0}
    assert evaluate_expression("-(3 + 4) / 2").value == -3.5
    rejected = [
        ("unknown_var * 10", {"known": 5}), ("__import__('os').system('ls')", None), ("(1).__class__", None), ("abs(-3)", None),
        ("2 ** 100000", None), ("9 ** 9 ** 9", None), ("7 % 3", None), ("1 < 2", None), ("x[0]", {"x": 1}), ("'a' * 3", None),
        ("1" + " + 1" * 200, None), ("-" * 30 + "1", None), ("1 / 0", None), ("1e300 * 1e300", None), ("True + 1", None),
        ("a + 1", {"a": True}), ("a + 1", {"__a": 1}), ("a", {"a": float("inf")}), ("", None), ("lambda: 1", None),
    ]
    for expression, variables in rejected:
        try:
            evaluate_expression(expression, variables)
        except CalculatorError:
            continue
        raise AssertionError(f"calculator accepted {expression!r}")


def test_check_amendments_sql_is_parameterized_and_bad_ids_never_reach_bigquery() -> None:
    sql = build_amendment_lookup_sql("test-project", "rag_arena", "document_chunks_v3")
    assert "solicitation_id = @solicitation_id" in sql and "UPPER(@section_id)" in sql and "tier_profile = 'multimodal'" in sql
    assert "ORDER BY amendment_number DESC, effective_date DESC" in sql and "amendment_number > 0" in sql
    bq = FakeBigQueryClient(rows=[_AMD_ROW])
    rows, stats = run_amendment_query(bq, sql, "solicitation_01", "CLIN-0004-AB'; DELETE", 30)
    params = {p.name: p.value for p in bq.queries[0][1].query_parameters}
    assert params == {"solicitation_id": "solicitation_01", "section_id": "CLIN-0004-AB'; DELETE"} and "DELETE" not in bq.queries[0][0]
    assert len(rows) == 1 and stats["bytes_processed"] == 12_582_912
    for bad in (("solicitation_01; DROP", "CLIN-1"), ("solicitation_01", "x' OR '1'='1"), ("", "CLIN-1")):
        settings = live_settings(enabled_levels=("level4_agentic",))
        probe = FakeBigQueryClient(rows=[])
        deps = ToolDeps(settings=settings, retriever=Level3MultimodalEngine(settings=settings), genai_client=None, bq_client=probe, ledger=AgentLedger("r"))
        try:
            run(run_check_amendments(deps, *bad))
        except ValueError:
            assert probe.queries == []
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_claim_extraction_and_unsupported_detection() -> None:
    claims = {(c.kind, c.text) for c in extract_claims("Under CLIN-0004-AB and SEC-4.2.1 the rate is $245.00/hr, 5% off for 2,000 hours ($465,500.00); CTRL-SOC2-CC6.1 applies.")}
    assert {("clause", "CLIN-0004-AB"), ("clause", "SEC-4.2.1"), ("clause", "CTRL-SOC2-CC6.1"), ("money", "$245.00"),
            ("percent", "5%"), ("quantity", "2,000 hours"), ("money", "$465,500.00")} <= claims
    cited = ["Amendment 0001 supersedes CLIN-0004-AB ($210.00/hr) to $245.00/hr with a 5% discount; 2,000 hours estimated."]
    checks = {c.claim.text: c.supported_by for c in check_claims("CLIN-0004-AB: $245.00/hr, 5%, 2,000 hours, total $465,500.00, cap $999.00, CLIN-9999.", cited, [465500.0])}
    assert checks == {"CLIN-0004-AB": "chunk", "$245.00": "chunk", "5%": "chunk", "2,000 hours": "chunk", "$465,500.00": "calculator",
                      "$999.00": "", "CLIN-9999": ""}
    ledger = AgentLedger("r")
    from app.engines.agent_tools import amendment_row_to_chunk

    ledger.register_chunks([amendment_row_to_chunk(SimpleNamespace(**_AMD_ROW))])
    report = verify_draft(ledger, "CLIN-0004-AB is $245.00/hr, not $250.00/hr.", [_AMD_ID, "never_returned"])
    assert [c.text for c in report.unsupported] == ["$250.00"] and report.unknown_chunk_ids == ("never_returned",) and not report.verified


def test_citations_are_restricted_to_tool_returned_chunk_ids() -> None:
    from app.engines.agent_tools import amendment_row_to_chunk

    ledger = AgentLedger("r")
    ledger.register_chunks([amendment_row_to_chunk(SimpleNamespace(**_AMD_ROW)), amendment_row_to_chunk(SimpleNamespace(**_PRICE_ROW))])
    cites = resolve_citations(f"Answer text.\nCITED_CHUNK_IDS: `{_PRICE_ID}`, fabricated_1, {_PRICE_ID}", ledger)
    assert [c.chunk_id for c in cites.chunks] == [_PRICE_ID] and cites.rejected_ids == ("fabricated_1",) and cites.answer == "Answer text."
    inline = resolve_citations(f"Per {_AMD_ID} the rate is $245.00/hr.", ledger)
    assert [c.chunk_id for c in inline.chunks] == [_AMD_ID] and inline.source == "inline_chunk_ids"
    try:
        resolve_citations("No ids here.\nCITED_CHUNK_IDS: fabricated_2", ledger)
    except AgentProtocolError as exc:
        assert "fabricated_2" in str(exc)
    else:
        raise AssertionError("a final answer citing only unknown ids must fail loud")


# ---------------------------------------------------------------- model selection + architecture


def test_claude_agent_model_is_rejected_with_http_400() -> None:
    settings = live_settings(enabled_levels=("level4_agentic",))
    models = load_catalog(settings).select("claude-opus-5-5", "gemini-4-argon")
    try:
        build_engine("level4_agentic", settings, models)
    except AgentModelNotSupportedError as exc:
        assert "Level 4 agent model must be a Gemini model in this release" in str(exc)
    else:
        raise AssertionError("Claude must not drive the ADK agent in this release")
    body = {"query": "q", "engines": ["level4_agentic"], "synthesis_model": "claude-opus-5-5", "judge_model": "gemini-4-argon"}
    resp = TestClient(create_app(settings=settings)).post("/api/v1/battle/stream", json=body)
    assert resp.status_code == 400 and "must be a Gemini model" in resp.json()["error"]
    assert build_engine("level4_agentic", settings, load_catalog(settings).select("gemini-3.8-flash", "claude-opus-5-5")).synthesis_model.id == "gemini-3.8-flash"


def test_level4_architecture_maps_tools_to_agent_turn_and_every_stage_to_a_node() -> None:
    desc = get_architecture_descriptor("level4_agentic")
    by_id = {n.id: n for n in desc.nodes}
    mapped = {s for n in desc.nodes for s in n.stage_ids}
    assert set(Level4AgenticEngine.stages) <= mapped
    for node_id in ("agent", "tool_search", "tool_amend", "tool_calc", "tool_verify", "embed", "bq", "rank"):
        assert by_id[node_id].stage_ids == ("agent_turn",), node_id
    assert by_id["llm"].stage_ids == ("synthesize",) and by_id["judge"].stage_ids == ("evaluate",) and by_id["user"].stage_ids == ("query_prep",)
    assert {n.label for n in desc.nodes} >= {"search_procurement_corpus", "check_amendments", "calculate_contract_pricing", "verify_citations"}
    edges = {(e.source, e.target) for e in desc.edges}
    assert {("agent", "tool_search"), ("tool_amend", "bq"), ("rank", "tool_search"), ("agent", "llm"), ("llm", "judge")} <= edges
    assert "_AMENDMENT_LEDGER" not in json.dumps(desc.to_dict())


def test_v2_theater_is_gone() -> None:
    import app.engines.level4_agentic as l4

    source = open(l4.__file__, encoding="utf-8").read()
    for banned in ("_AMENDMENT_LEDGER", "CLIN-0004-AB", "_try_run_adk_in_memory", "run_legacy_body", "_build_live_pricing_step"):
        assert banned not in source, banned


def test_build_agent_constructs_a_real_adk_llm_agent_on_vertex_gemini_without_network() -> None:
    from google.adk.models.google_llm import Gemini

    from app.engines.agent_runtime import build_agent
    from app.engines.agent_tools import build_tools

    settings = live_settings(enabled_levels=("level4_agentic",))
    spec = load_catalog(settings).get("gemini-3.8-flash")
    deps = ToolDeps(settings=settings, retriever=Level3MultimodalEngine(settings=settings), genai_client=None, bq_client=None, ledger=AgentLedger("r"))
    agent = build_agent(spec, settings, build_tools(deps))
    assert isinstance(agent, LlmAgent) and isinstance(agent.model, Gemini) and agent.model.model == "gemini-3.8-flash"
    kw = dict(agent.model.client_kwargs)
    http = kw.pop("http_options")
    assert kw == {"vertexai": True, "project": "test-project", "location": spec.location}
    assert http.timeout == 45_000 and http.retry_options.attempts == 2  # per-call timeout + one bounded retry
    assert agent.generate_content_config.max_output_tokens == settings.synthesis_max_tokens
    assert [t.__name__ for t in agent.tools] == ["search_procurement_corpus", "check_amendments", "calculate_contract_pricing", "verify_citations"]
    # Thinking goes through the planner: newer google-adk rejects thinking_config in generate_content_config
    # ("Thinking config should be set via LlmAgent.planner") — the Cloud Run failure of 2026-10-23.
    assert agent.generate_content_config.thinking_config is None
    assert agent.planner.thinking_config.thinking_level.name == "LOW" == settings.agent_thinking_level.upper()
    claude = load_catalog(settings).get("claude-opus-5-5")
    try:
        build_agent(claude, settings, [])
    except AgentModelNotSupportedError:
        return
    raise AssertionError("build_agent must refuse Claude")
