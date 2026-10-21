"""Stage bodies shared by the BigQuery levels (Level 1 naive, Level 2 hybrid): query embedding and synthesis.

Each helper opens its stage through ``engine.stage`` (so the stage id must be declared by the engine), runs the
blocking SDK call in a worker thread, and fills the measured payload. Behaviour is identical for every caller;
only the context block handed to ``synthesize_stage`` differs per level.
"""

from __future__ import annotations

from typing import Any

from app.core.embeddings import QUERY_TASK_TYPE, embed_config, format_query_for_embedding, single_vector
from app.core.models import TokenUsage
from app.core.models_catalog import ModelSpec
from app.core.telemetry import StageEmitter
from app.engines.synthesis import GeminiSynthesizer, build_prompt, build_synthesizer


def embedding_token_count(embed_response: Any) -> int | None:
    """Read ``embeddings[0].statistics.token_count`` when the SDK returns it (Vertex AI does)."""
    embeddings = getattr(embed_response, "embeddings", None) or []
    stats = getattr(embeddings[0], "statistics", None) if embeddings else None
    count = getattr(stats, "token_count", None) if stats is not None else None
    return int(count) if isinstance(count, (int, float)) else None


async def embed_query_stage(engine: Any, prepared: str, genai_client: Any, emitter: StageEmitter) -> tuple[list[float], int]:
    """Embed the query in the same gemini-embedding-2 space as the ingested documents (RETRIEVAL_QUERY side)."""
    model = engine.settings.text_embedding_model
    contents = format_query_for_embedding(prepared)
    async with engine.stage(
        emitter,
        "embed_query",
        label="Query embedding",
        service=f"Vertex AI · {model}",
        request={"model": model, "contents": contents, "task_type": QUERY_TASK_TYPE, "method": "models.embed_content"},
    ) as ctx:
        resp = await engine.blocking(
            genai_client.models.embed_content, model=model, contents=contents, config=embed_config(QUERY_TASK_TYPE)
        )
        vector = single_vector(resp)
        token_count = embedding_token_count(resp)
        ctx.payload = {"model": model, "dimensions": len(vector), "token_count": token_count, "task_type": QUERY_TASK_TYPE}
    return vector, token_count or 0


async def synthesize_stage(
    engine: Any,
    spec: ModelSpec,
    query: str,
    context_block: str,
    context_chunks: int,
    emitter: StageEmitter,
    embedding_tokens: int,
) -> tuple[str, TokenUsage]:
    """Answer with the run's selected synthesis model (Gemini or Claude) through its provider adapter."""
    synthesizer = build_synthesizer(spec, engine.settings)
    prompt = build_prompt(context_block, query)
    async with engine.stage(
        emitter,
        "synthesize",
        label="Answer synthesis",
        service=f"Vertex AI · {spec.label}",
        request=synthesizer.request_summary(prompt, context_chunks),
    ) as ctx:
        out = await engine.blocking(synthesizer.generate, prompt)
        usage = TokenUsage(
            prompt_tokens=out.prompt_tokens,
            output_tokens=out.output_tokens,
            thinking_tokens=out.thinking_tokens,
            embedding_tokens=embedding_tokens,
            model=spec.id,
            embedding_model=engine.settings.text_embedding_model,
        )
        ctx.payload = {
            "model": spec.id,
            "provider": spec.provider,
            "prompt_tokens": usage.prompt_tokens,
            "output_tokens": usage.output_tokens,
            "thinking_tokens": usage.thinking_tokens,
            "stop_reason": out.stop_reason,
            "response_id": out.response_id,
            "attempts": out.attempts,
            "answer": out.answer,
        }
        if isinstance(synthesizer, GeminiSynthesizer):
            ctx.payload["thinking_level"] = synthesizer.thinking_level
    return out.answer, usage


__all__ = ["embed_query_stage", "embedding_token_count", "synthesize_stage"]
