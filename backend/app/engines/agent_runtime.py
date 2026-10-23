"""Level 4 ADK runtime: build the ``LlmAgent``, run it with ``InMemoryRunner`` (created session, ``run_async``) under
explicit bounds, and translate every ADK event into an ``agent_turn`` telemetry event the moment it arrives.

Bounds (fail loud — the error propagates and the engine's ``agent_turn`` stage emits ``stage_failed``):
* ``RunConfig.max_llm_calls = max_turns`` (ADK raises ``LlmCallsLimitExceededError`` on the next call) plus our own
  turn counter;
* ``max_tool_calls_per_turn`` — a model turn requesting more function calls raises ``AgentLimitError`` before ADK runs
  the tools (we stop consuming the generator);
* a wall-clock deadline (``asyncio.timeout``) → ``AgentTimeoutError``.
There is no fallback: if the runner errors or finishes without a final text response, the run fails.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
import json
import time
from typing import Any, Callable

from app.core.config import ArenaSettings
from app.core.models_catalog import ModelSpec, PROVIDER_VERTEX_GEMINI
from app.core.telemetry import BaseStageEmitter
from app.engines.agent_ledger import AgentLedger, bounded_summary

APP_NAME = "rag_arena_level4"
AGENT_NAME = "procurement_agent"
USER_ID = "arena_user"
MAX_ARGS_PREVIEW_CHARS = 1200
MAX_TEXT_PREVIEW_CHARS = 600
MAX_RUNNER_EVENTS = 400
MODEL_CALL_ATTEMPTS = 2  # one bounded retry of a timed-out / 408 / 429 / 5xx model call (google-genai tenacity)
AGENT_INSTRUCTION = (
    "You are an enterprise procurement analyst agent. Answer ONLY from tool results — never from memory.\n"
    "Workflow:\n"
    "1. Call search_procurement_corpus to find the relevant clauses, CLIN pricing tables, SLA matrices or diagrams. "
    "For questions spanning several solicitations, search each solicitation separately (solicitation_id filter).\n"
    "2. For every CLIN or clause whose value you report, call check_amendments with its solicitation_id; when an "
    "amendment supersedes it, the latest amendment governs — report the superseded and the governing value.\n"
    "3. Compute every total, discount, credit or sum with calculate_contract_pricing (pass every number as a variable); "
    "never do arithmetic yourself.\n"
    "4. Before answering, call verify_citations with your full draft answer and the chunk_ids that support it. If it "
    "reports unsupported claims or unknown chunk ids, repair the draft (correct or remove those claims, or retrieve "
    "support) and call verify_citations exactly once more.\n"
    "5. Final answer: concise, cite document names, state amendment supersession explicitly, and end with exactly one "
    "line `CITED_CHUNK_IDS: <comma-separated chunk_ids>` listing only chunk_ids returned by tools that support the answer.\n"
    "Be efficient: batch independent tool calls in one turn, avoid redundant calculations, keep the answer under 250 words."
)


def agent_instruction(max_tool_calls_per_turn: int) -> str:
    """The instruction sent to the model, including the per-turn tool-call bound the runtime enforces."""
    return f"{AGENT_INSTRUCTION}\nRequest at most {max_tool_calls_per_turn} tool calls in a single turn."


class AgentLimitError(RuntimeError):
    """The agent exceeded a configured bound (turns or tool calls per turn)."""


class AgentTimeoutError(RuntimeError):
    """The agent loop exceeded its wall-clock budget."""


class AgentProtocolError(RuntimeError):
    """The ADK run ended in a state that cannot produce a measured, cited answer (no final text, no usage, …)."""


class AgentModelNotSupportedError(ValueError):
    """The selected model cannot drive the ADK agent in this release (HTTP 400 at request validation)."""


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int
    max_tool_calls_per_turn: int
    budget_seconds: float

    def __post_init__(self) -> None:
        if not (1 <= self.max_turns <= 16):
            raise ValueError("max_turns must be between 1 and 16")
        if not (1 <= self.max_tool_calls_per_turn <= 8):
            raise ValueError("max_tool_calls_per_turn must be between 1 and 8")
        if not (0 < self.budget_seconds <= 600):
            raise ValueError("budget_seconds must be in (0, 600]")


@dataclass(frozen=True)
class TurnTokens:
    prompt: int = 0
    output: int = 0
    thinking: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt, "output": self.output, "thinking": self.thinking, "total": self.prompt + self.output + self.thinking}


@dataclass(frozen=True)
class ModelTurn:
    turn_index: int
    duration_ms: int
    tokens: TurnTokens
    function_calls: tuple[str, ...]
    text: str


@dataclass
class AgentOutcome:
    """What the runner produced (filled while consuming events)."""

    turns: list[ModelTurn] = field(default_factory=list)
    final_text: str = ""
    final_turn_index: int = 0
    events_seen: int = 0
    agent_turn_events: int = 0

    def tokens(self) -> TurnTokens:
        return TurnTokens(
            prompt=sum(t.tokens.prompt for t in self.turns),
            output=sum(t.tokens.output for t in self.turns),
            thinking=sum(t.tokens.thinking for t in self.turns),
        )


def ensure_agent_model(spec: ModelSpec) -> None:
    """Level 4 agent models must be Gemini (ADK's Claude adapter needs the undeclared ``anthropic`` package)."""
    if spec.provider != PROVIDER_VERTEX_GEMINI:
        raise AgentModelNotSupportedError(
            f"Level 4 agent model must be a Gemini model in this release (got {spec.id}, provider {spec.provider})"
        )


def agent_client_kwargs(spec: ModelSpec, settings: ArenaSettings) -> dict[str, Any]:
    """google-genai Client options for the agent model: Vertex routing + per-call timeout + one bounded retry."""
    from google.genai import types

    return {
        "vertexai": True,
        "project": settings.gcp_project_id,
        "location": spec.location or settings.vertex_genai_location,
        "http_options": types.HttpOptions(
            timeout=settings.agent_model_call_timeout_seconds * 1000,
            retry_options=types.HttpRetryOptions(attempts=MODEL_CALL_ATTEMPTS, initial_delay=1.0, max_delay=4.0),
        ),
    }


def build_agent(spec: ModelSpec, settings: ArenaSettings, tools: list[Callable[..., Any]]) -> Any:
    """``LlmAgent`` on Vertex AI Gemini (explicit per-agent Vertex routing via ``client_kwargs``)."""
    ensure_agent_model(spec)
    from google.adk.agents import LlmAgent
    from google.adk.models.google_llm import Gemini
    from google.adk.planners import BuiltInPlanner
    from google.genai import types

    model = Gemini(model=spec.id, client_kwargs=agent_client_kwargs(spec, settings))
    return LlmAgent(
        name=AGENT_NAME,
        model=model,
        description="Multi-hop procurement RAG agent: search, amendment supersession, exact pricing math, citation check.",
        instruction=agent_instruction(settings.agent_max_tool_calls_per_turn),
        tools=tools,
        # ADK rejects thinking_config inside generate_content_config ("Thinking config should be set via
        # LlmAgent.planner"); the built-in planner forwards it to Gemini unchanged.
        planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_level=settings.agent_thinking_level.upper())),
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=settings.synthesis_max_tokens,  # bounded output per model turn (SYNTHESIS_MAX_TOKENS)
        ),
    )


def _preview(value: Any, limit: int = MAX_ARGS_PREVIEW_CHARS) -> Any:
    text = json.dumps(value, default=str, ensure_ascii=False)
    return value if len(text) <= limit else text[: limit - 1] + "…"


def _usage_tokens(event: Any) -> TurnTokens:
    meta = getattr(event, "usage_metadata", None)
    if meta is None:
        raise AgentProtocolError("ADK model event carries no usage_metadata; cannot compute measured cost")
    return TurnTokens(
        prompt=int(getattr(meta, "prompt_token_count", 0) or 0),
        output=int(getattr(meta, "candidates_token_count", 0) or 0),
        thinking=int(getattr(meta, "thoughts_token_count", 0) or 0),
    )


def _parts(event: Any) -> list[Any]:
    content = getattr(event, "content", None)
    return list(getattr(content, "parts", None) or [])


def _event_text(event: Any) -> str:
    return "".join(str(p.text) for p in _parts(event) if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()


class TurnTranslator:
    """Turns ADK events into ``agent_turn`` field dicts (model / tool_call / tool_result) with measured durations.

    ``repair`` is true on every frame of a model turn that follows a ``verify_citations`` result reporting unsupported
    claims or unknown chunk ids (the repair turn the instruction requires), and on that turn's tool calls/results."""

    def __init__(self, ledger: AgentLedger, limits: AgentLimits, clock: Callable[[], float] = time.perf_counter) -> None:
        self._ledger = ledger
        self._limits = limits
        self._clock = clock
        self._last = clock()
        self._turn = 0
        self._call_turn: dict[str, int] = {}
        self._repair_pending = False
        self._repair_turns: set[int] = set()
        self.outcome = AgentOutcome()

    def _since_last(self) -> int:
        now = self._clock()
        elapsed, self._last = max(0, int((now - self._last) * 1000)), now
        return elapsed

    def _model_fields(self, event: Any, calls: list[Any]) -> list[dict[str, Any]]:
        self._turn += 1
        if self._turn > self._limits.max_turns:
            raise AgentLimitError(f"agent exceeded AGENT_MAX_TURNS={self._limits.max_turns}")
        if len(calls) > self._limits.max_tool_calls_per_turn:
            raise AgentLimitError(
                f"turn {self._turn} requested {len(calls)} tool calls (> AGENT_MAX_TOOL_CALLS_PER_TURN={self._limits.max_tool_calls_per_turn})"
            )
        if self._repair_pending:
            self._repair_turns.add(self._turn)
            self._repair_pending = False
        repair = self._turn in self._repair_turns
        tokens, text, duration = _usage_tokens(event), _event_text(event), self._since_last()
        self.outcome.turns.append(ModelTurn(self._turn, duration, tokens, tuple(c.name for c in calls), text))
        fields = [{
            "turn_index": self._turn, "kind": "model", "tool_name": None, "tool_args": None, "tool_result_summary": None,
            "duration_ms": duration, "tokens": tokens.to_dict(), "function_calls": [c.name for c in calls],
            "text_preview": text[:MAX_TEXT_PREVIEW_CHARS] if text else None, "call_id": None, "repair": repair,
        }]
        for call in calls:
            self._call_turn[str(call.id or "")] = self._turn
            fields.append({
                "turn_index": self._turn, "kind": "tool_call", "tool_name": call.name, "tool_args": _preview(dict(call.args or {})),
                "tool_result_summary": None, "duration_ms": None, "tokens": None, "call_id": call.id, "repair": repair,
            })
        return fields

    def _needs_repair(self, resp: Any) -> bool:
        body = resp.response if isinstance(resp.response, dict) else {}
        return resp.name == "verify_citations" and bool(body.get("unsupported_claims") or body.get("unknown_chunk_ids"))

    def _result_fields(self, responses: list[Any]) -> list[dict[str, Any]]:
        self._since_last()
        fields = []
        for resp in responses:
            record = self._ledger.tool_record(str(resp.id or ""))
            summary = record.summary if record else bounded_summary(json.dumps(resp.response, default=str))
            turn = self._call_turn.get(str(resp.id or ""), self._turn)
            fields.append({
                "turn_index": turn, "kind": "tool_result", "tool_name": resp.name,
                "tool_args": None, "tool_result_summary": summary, "duration_ms": record.duration_ms if record else None,
                "status": record.status if record else None, "tokens": None, "call_id": resp.id, "repair": turn in self._repair_turns,
            })
            self._repair_pending = self._repair_pending or self._needs_repair(resp)
        return fields

    def translate(self, event: Any) -> list[dict[str, Any]]:
        """Field dicts for one ADK event (empty for events that carry nothing to show, e.g. partial chunks)."""
        self.outcome.events_seen += 1
        if getattr(event, "partial", None):
            return []
        responses = list(event.get_function_responses() or [])
        if responses:
            return self._result_fields(responses)
        calls = list(event.get_function_calls() or [])
        if getattr(event, "author", "") != AGENT_NAME or not (calls or _event_text(event)):
            return []
        fields = self._model_fields(event, calls)
        if not calls and event.is_final_response():
            self.outcome.final_text = _event_text(event)
            self.outcome.final_turn_index = self._turn
        return fields


async def run_agent(
    agent: Any,
    user_message: str,
    *,
    limits: AgentLimits,
    ledger: AgentLedger,
    emitter: BaseStageEmitter,
    runner_factory: Callable[..., Any] | None = None,
) -> AgentOutcome:
    """Run ``agent`` once in a fresh in-memory session; emit ``agent_turn`` per ADK event as it arrives."""
    if not isinstance(emitter, BaseStageEmitter):
        raise TypeError("emitter must be a BaseStageEmitter (agent_turn events are required)")
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("user_message must be a non-empty string")
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = (runner_factory or InMemoryRunner)(agent=agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    message = types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    translator = TurnTranslator(ledger, limits)
    stream = runner.run_async(
        user_id=USER_ID, session_id=session.id, new_message=message, run_config=RunConfig(max_llm_calls=limits.max_turns)
    )
    try:
        async with asyncio.timeout(limits.budget_seconds):
            await _consume(stream, translator, emitter)
    except TimeoutError as exc:
        raise AgentTimeoutError(f"agent loop exceeded its {limits.budget_seconds:.0f} s budget (QUERY_TIMEOUT_SECONDS)") from exc
    outcome = translator.outcome
    if not outcome.final_text:
        raise AgentProtocolError(f"ADK runner finished after {len(outcome.turns)} turn(s) without a final text response")
    return outcome


async def _consume(stream: Any, translator: TurnTranslator, emitter: BaseStageEmitter) -> None:
    async with aclosing(stream) as events:
        async for event in events:
            if translator.outcome.events_seen >= MAX_RUNNER_EVENTS:
                raise AgentLimitError(f"ADK runner produced more than {MAX_RUNNER_EVENTS} events")
            for fields in translator.translate(event):
                translator.outcome.agent_turn_events += 1
                await emitter.agent_turn(seq=translator.outcome.agent_turn_events, **fields)


__all__ = [
    "AGENT_INSTRUCTION", "AGENT_NAME", "APP_NAME", "AgentLimitError", "AgentLimits", "AgentModelNotSupportedError",
    "AgentOutcome", "AgentProtocolError", "AgentTimeoutError", "ModelTurn", "TurnTokens", "TurnTranslator", "agent_instruction", "build_agent",
    "ensure_agent_model", "run_agent",
]
