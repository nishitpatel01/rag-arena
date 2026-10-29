"""Tests for per-model pricing (spec §6.1: cost = usage × published per-model prices)."""

from __future__ import annotations

import pytest

from app.core.models import TokenUsage
from app.core.pricing import (
    PRICE_TABLE,
    PricingError,
    cost_from_usage,
    price_for_model,
    rank_cost_usd,
)


def test_price_table_covers_required_models_with_positive_rates() -> None:
    for model in ("gemini-4-argon", "gemini-4-carbon", "gemini-embedding-2", "discovery-engine-rank"):
        price = price_for_model(model)
        assert price.model == model
        assert price.source.startswith("https://")
        assert price.price_as_of
    assert PRICE_TABLE["gemini-4-argon"].input_usd_per_1m > 0
    assert PRICE_TABLE["gemini-4-argon"].output_usd_per_1m > 0
    assert (PRICE_TABLE["gemini-4-carbon"].input_usd_per_1m, PRICE_TABLE["gemini-4-carbon"].output_usd_per_1m) == (2.00, 10.00)
    assert PRICE_TABLE["gemini-embedding-2"].input_usd_per_1m > 0
    assert PRICE_TABLE["discovery-engine-rank"].usd_per_1k_requests > 0


def test_cost_from_usage_is_linear_in_tokens_and_zero_for_zero_usage() -> None:
    usage = TokenUsage(prompt_tokens=1_000_000, output_tokens=1_000_000, model="gemini-4-argon")
    gen = PRICE_TABLE["gemini-4-argon"]
    assert cost_from_usage(usage, "gemini-4-argon") == pytest.approx(gen.input_usd_per_1m + gen.output_usd_per_1m)
    assert cost_from_usage(TokenUsage(), "gemini-4-argon") == 0.0
    with_embed = TokenUsage(
        prompt_tokens=0, output_tokens=0, embedding_tokens=2_000_000, embedding_model="gemini-embedding-2"
    )
    emb = PRICE_TABLE["gemini-embedding-2"]
    assert cost_from_usage(with_embed, "gemini-4-argon") == pytest.approx(2 * emb.input_usd_per_1m)


def test_unknown_model_raises_and_no_tier_multipliers_exist() -> None:
    with pytest.raises(PricingError):
        cost_from_usage(TokenUsage(prompt_tokens=10), "gemini-1.5-flash")
    with pytest.raises(PricingError):
        price_for_model("")
    assert rank_cost_usd(1000) == pytest.approx(PRICE_TABLE["discovery-engine-rank"].usd_per_1k_requests)
    with pytest.raises(ValueError):
        rank_cost_usd(-1)
    import app.engines.base as base_module

    assert not hasattr(base_module.BaseRAGEngine, "estimate_cost_usd")
    assert "tier_multiplier" not in open(base_module.__file__, encoding="utf-8").read()


def test_claude_opus_judge_price_is_published_with_source_and_date() -> None:
    from app.core.pricing import judge_cost_usd

    price = price_for_model("claude-opus-5-5")
    assert price.input_usd_per_1m == 4.00 and price.output_usd_per_1m == 20.00
    assert price.source == "https://cloud.google.com/vertex-ai/generative-ai/pricing"
    assert price.price_as_of == "2026-10-21" and "global" in price.note.lower()
    assert judge_cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=0) == pytest.approx(4.0)
    assert judge_cost_usd("claude-opus-5-5", input_tokens=2000, output_tokens=100) == pytest.approx(0.01)
    with pytest.raises(PricingError):
        judge_cost_usd("claude-unknown", input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError):
        judge_cost_usd("claude-opus-5-5", input_tokens=-1, output_tokens=0)
