"""Published per-model prices and cost derivation from *measured* token usage (spec §6.1, P3).

Prices are design metadata: USD per 1M tokens (models) or USD per 1k requests (Ranking API). They
are never multiplied by tier "complexity" factors. Each entry carries the page it was read from and
the date it was read so a reviewer can re-verify it; update ``price_as_of`` when re-verifying.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import TokenUsage


class PricingError(ValueError):
    """Raised for unknown models or invalid usage inputs."""


@dataclass(frozen=True)
class ModelPrice:
    """Price card for one billable model or API."""

    model: str
    input_usd_per_1m: float = 0.0
    output_usd_per_1m: float = 0.0
    usd_per_1k_requests: float = 0.0
    source: str = ""
    price_as_of: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.model:
            raise PricingError("ModelPrice.model must be non-empty")
        for name in ("input_usd_per_1m", "output_usd_per_1m", "usd_per_1k_requests"):
            if float(getattr(self, name)) < 0.0:
                raise PricingError(f"{name} must be >= 0")


_VERTEX_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
_DISCOVERY_PRICING_URL = "https://cloud.google.com/generative-ai-app-builder/pricing"

PRICE_TABLE: dict[str, ModelPrice] = {
    "gemini-4-argon": ModelPrice(
        model="gemini-4-argon",
        input_usd_per_1m=2.00,
        output_usd_per_1m=10.00,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-20",
        note="Introductory global-endpoint rate through 2026-11-30; output rate includes thinking tokens.",
    ),
    "gemini-embedding-2": ModelPrice(
        model="gemini-embedding-2",
        input_usd_per_1m=0.20,
        output_usd_per_1m=0.0,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-20",
        note="Text input rate; multimodal (image/file) inputs are billed at a separate rate.",
    ),
    "discovery-engine-rank": ModelPrice(
        model="discovery-engine-rank",
        usd_per_1k_requests=1.00,
        source=_DISCOVERY_PRICING_URL,
        price_as_of="2026-10-20",
        note="Ranking API (semantic-ranker-default), billed per 1,000 rank requests.",
    ),
}


def price_for_model(model: str) -> ModelPrice:
    """Return the price card for ``model`` or raise ``PricingError`` for unknown/empty names."""
    if not isinstance(model, str) or not model.strip():
        raise PricingError("model must be a non-empty string")
    key = model.strip()
    if key not in PRICE_TABLE:
        raise PricingError(f"No published price configured for model '{key}'; add it to pricing.PRICE_TABLE")
    return PRICE_TABLE[key]


def cost_from_usage(usage: TokenUsage, model: str) -> float:
    """USD cost of a run: generation tokens priced at ``model``, embedding tokens at ``usage.embedding_model``."""
    if not isinstance(usage, TokenUsage):
        raise PricingError("usage must be a TokenUsage instance")
    gen = price_for_model(model)
    total = (usage.prompt_tokens / 1_000_000.0) * gen.input_usd_per_1m
    total += ((usage.output_tokens + usage.thinking_tokens) / 1_000_000.0) * gen.output_usd_per_1m
    if usage.embedding_tokens > 0:
        emb = price_for_model(usage.embedding_model)
        total += (usage.embedding_tokens / 1_000_000.0) * emb.input_usd_per_1m
    return round(total, 8)


def rank_cost_usd(requests: int) -> float:
    """USD cost of ``requests`` Discovery Engine Ranking API calls."""
    if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
        raise ValueError("requests must be an integer >= 0")
    return round((requests / 1000.0) * PRICE_TABLE["discovery-engine-rank"].usd_per_1k_requests, 8)


__all__ = ["ModelPrice", "PRICE_TABLE", "PricingError", "cost_from_usage", "price_for_model", "rank_cost_usd"]
