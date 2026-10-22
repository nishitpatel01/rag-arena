"""Answer-synthesis provider adapters behind one interface (selected per run from the model catalog).

* ``GeminiSynthesizer`` — google-genai ``models.generate_content`` on Vertex AI with ``thinking_level`` from
  ``GEMINI_THINKING_LEVEL`` (the Level 1 prompt is sent unchanged as a single user turn, as before).
* ``AnthropicVertexSynthesizer`` — Claude via Vertex ``:rawPredict``: ``system`` = the grounding instruction,
  user message = retrieved context + question, ``max_tokens`` = ``SYNTHESIS_MAX_TOKENS``, **no temperature**.

Both return a ``SynthesisOutput`` whose token counts come from the provider's usage fields (never estimated); a
missing usage block, an empty answer or a non-2xx status raises (spec P2). There is no fallback model.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Protocol

from app.core.anthropic_vertex import (
    ANTHROPIC_VERTEX_VERSION,
    AnthropicVertexClient,
    RawPredictHTTPError,
    message_text,
    message_usage,
)
from app.core.config import ArenaSettings
from app.core.gcp_clients import get_access_token, get_genai_client
from app.core.models_catalog import PROVIDER_ANTHROPIC_VERTEX, PROVIDER_VERTEX_GEMINI, ModelSpec

SYNTHESIS_INSTRUCTION = "Answer the procurement question using ONLY the retrieved context chunks below."


class SynthesisHTTPError(RawPredictHTTPError):
    """Claude synthesis endpoint answered with a non-2xx status (after bounded retries if retryable)."""


@dataclass(frozen=True)
class SynthesisPrompt:
    """The grounded prompt in both shapes: ``system`` + ``user`` (Claude) and ``combined`` (Gemini, unchanged)."""

    system: str
    user: str

    @property
    def combined(self) -> str:
        return f"{self.system}\n\n{self.user}"


@dataclass(frozen=True)
class SynthesisOutput:
    """One synthesis call: answer + measured usage + provenance."""

    answer: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    response_id: str
    stop_reason: str
    attempts: int


class Synthesizer(Protocol):
    spec: ModelSpec

    def request_summary(self, prompt: SynthesisPrompt, context_chunks: int) -> dict[str, Any]: ...

    def generate(self, prompt: SynthesisPrompt) -> SynthesisOutput: ...


def build_prompt(context_block: str, query: str, instruction: str = SYNTHESIS_INSTRUCTION) -> SynthesisPrompt:
    """Grounded prompt: instruction (system) + retrieved context and question (user). Levels 1–2 use the default
    instruction unchanged; Level 3 appends its authority/element rules."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")
    return SynthesisPrompt(system=instruction, user=f"Context:\n{context_block}\n\nQuestion: {query}")


class GeminiSynthesizer:
    """Gemini on Vertex AI via google-genai (thinking config from settings)."""

    def __init__(self, spec: ModelSpec, settings: ArenaSettings) -> None:
        if spec.provider != PROVIDER_VERTEX_GEMINI:
            raise ValueError(f"GeminiSynthesizer needs a {PROVIDER_VERTEX_GEMINI} model, got {spec.provider}")
        self.spec = spec
        self._settings = settings
        self.thinking_level = settings.gemini_thinking_level.upper()

    def request_summary(self, prompt: SynthesisPrompt, context_chunks: int) -> dict[str, Any]:
        return {
            "model": self.spec.id,
            "provider": self.spec.provider,
            "location": self.spec.location,
            "thinking_level": self.thinking_level,
            "prompt": prompt.combined,
            "context_chunks": context_chunks,
            "method": "models.generate_content",
        }

    def generate(self, prompt: SynthesisPrompt) -> SynthesisOutput:
        client = get_genai_client(self._settings, location=self.spec.location)
        resp = client.models.generate_content(
            model=self.spec.id, contents=prompt.combined, config={"thinking_config": {"thinking_level": self.thinking_level}}
        )
        answer = str(getattr(resp, "text", "") or "").strip()
        if not answer:
            raise ValueError(f"generate_content ({self.spec.id}) returned an empty answer")
        meta = getattr(resp, "usage_metadata", None)
        if meta is None:
            raise ValueError("generate_content response carries no usage_metadata; cannot compute measured cost")
        candidates = getattr(resp, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", "") if candidates else ""
        return SynthesisOutput(
            answer=answer,
            prompt_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
            response_id=str(getattr(resp, "response_id", "") or ""),
            stop_reason=str(getattr(finish, "name", finish) or ""),
            attempts=1,
        )


class AnthropicVertexSynthesizer:
    """Claude on Vertex AI via ``:rawPredict`` (bounded retries inside QUERY_TIMEOUT_SECONDS, no temperature)."""

    def __init__(
        self,
        spec: ModelSpec,
        settings: ArenaSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if spec.provider != PROVIDER_ANTHROPIC_VERTEX:
            raise ValueError(f"AnthropicVertexSynthesizer needs an {PROVIDER_ANTHROPIC_VERTEX} model, got {spec.provider}")
        self.spec = spec
        self._settings = settings
        self._client = AnthropicVertexClient(
            project_id=settings.gcp_project_id,
            model=spec.id,
            location=spec.location,
            attempt_timeout_seconds=float(settings.synthesis_timeout_seconds),
            budget_seconds=float(settings.query_timeout_seconds),
            token_provider=token_provider or (lambda: get_access_token(settings)),
            sleep=sleep,
            clock=clock,
            error_cls=SynthesisHTTPError,
        )

    def body(self, prompt: SynthesisPrompt) -> dict[str, Any]:
        return {
            "anthropic_version": ANTHROPIC_VERTEX_VERSION,
            "max_tokens": self._settings.synthesis_max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }

    def request_summary(self, prompt: SynthesisPrompt, context_chunks: int) -> dict[str, Any]:
        return {
            "model": self.spec.id,
            "provider": self.spec.provider,
            "location": self.spec.location,
            "anthropic_version": ANTHROPIC_VERTEX_VERSION,
            "max_tokens": self._settings.synthesis_max_tokens,
            "system": prompt.system,
            "prompt": prompt.user,
            "context_chunks": context_chunks,
            "method": "rawPredict",
        }

    def generate(self, prompt: SynthesisPrompt) -> SynthesisOutput:
        doc, attempts = self._client.post_messages(self.body(prompt))
        answer = message_text(doc).strip()
        if not answer:
            raise ValueError(f"rawPredict ({self.spec.id}) returned an empty answer")
        input_tokens, output_tokens = message_usage(doc, required=True)
        return SynthesisOutput(
            answer=answer,
            prompt_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=0,
            response_id=str(doc.get("id", "")),
            stop_reason=str(doc.get("stop_reason", "") or ""),
            attempts=attempts,
        )


def build_synthesizer(spec: ModelSpec, settings: ArenaSettings) -> Synthesizer:
    """Adapter for the selected catalog model's provider; unknown providers raise (no fallback)."""
    if not isinstance(spec, ModelSpec) or not isinstance(settings, ArenaSettings):
        raise TypeError("spec must be a ModelSpec and settings an ArenaSettings")
    if spec.provider == PROVIDER_VERTEX_GEMINI:
        return GeminiSynthesizer(spec, settings)
    if spec.provider == PROVIDER_ANTHROPIC_VERTEX:
        return AnthropicVertexSynthesizer(spec, settings)
    raise ValueError(f"Unsupported synthesis provider {spec.provider!r}")


__all__ = [
    "AnthropicVertexSynthesizer",
    "GeminiSynthesizer",
    "SYNTHESIS_INSTRUCTION",
    "SynthesisHTTPError",
    "SynthesisOutput",
    "SynthesisPrompt",
    "Synthesizer",
    "build_prompt",
    "build_synthesizer",
]
