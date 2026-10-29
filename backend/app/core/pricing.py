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
    "gemini-4-carbon": ModelPrice(
        model="gemini-4-carbon",
        input_usd_per_1m=2.00,
        output_usd_per_1m=10.00,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-28",
        note=(
            "Introductory global-endpoint rate ($2.00 / $10.00 per 1M in/out; standard $4.00 / $20.00 "
            "post-introductory); output rate includes thinking tokens."
        ),
    ),
    "gemini-embedding-2": ModelPrice(
        model="gemini-embedding-2",
        input_usd_per_1m=0.20,
        output_usd_per_1m=0.0,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-20",
        note="Text input rate; multimodal (image/file) inputs are billed at a separate rate.",
    ),
    "claude-opus-5-5": ModelPrice(
        model="claude-opus-5-5",
        input_usd_per_1m=4.00,
        output_usd_per_1m=20.00,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-21",
        note=(
            "Anthropic Claude Opus 5.5 partner model (LLM-as-a-judge), Global endpoint, <= 200K input tokens; "
            "regional/multi-region endpoints are priced separately."
        ),
    ),
    "gemini-3.8-flash": ModelPrice(
        model="gemini-3.8-flash",
        input_usd_per_1m=0.75,
        output_usd_per_1m=3.75,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-21",
        note=(
            "Introductory global-endpoint rate through 2026-12-31 (standard $1.50 / $7.50 from 2027-01-01); "
            "output rate includes reasoning tokens."
        ),
    ),
    "claude-fable-5-1": ModelPrice(
        model="claude-fable-5-1",
        input_usd_per_1m=10.00,
        output_usd_per_1m=50.00,
        source=_VERTEX_PRICING_URL,
        price_as_of="2026-10-21",
        note="Anthropic Claude Fable 5.1 partner model, Global endpoint; regional/multi-region endpoints are $11 / $55.",
    ),
    "discovery-engine-rank": ModelPrice(
        model="discovery-engine-rank",
        usd_per_1k_requests=1.00,
        source=_DISCOVERY_PRICING_URL,
        price_as_of="2026-10-21",
        note=(
            "Ranking API 'Ranking' SKU: $1.00 per 1,000 count (pay-as-you-go list price). One count = one rank query "
            "of up to 100 documents; each further started block of 100 documents is another count (132 docs = 2)."
        ),
    ),
    "vertex-ai-search-enterprise": ModelPrice(
        model="vertex-ai-search-enterprise",
        usd_per_1k_requests=4.00,
        source=_DISCOVERY_PRICING_URL,
        price_as_of="2026-10-23",
        note=(
            "Agent Search General Pricing: 'Search Enterprise Edition includes Core Generative Answers (AI Mode)' "
            "$4.00 / 1,000 query (pay-as-you-go list price; the 10,000 queries/account/month free trial is NOT deducted)."
        ),
    ),
    "vertex-ai-search-summary": ModelPrice(
        model="vertex-ai-search-summary",
        usd_per_1k_requests=4.00,
        source=_DISCOVERY_PRICING_URL,
        price_as_of="2026-10-23",
        note=(
            "Pseudo synthesis model (search summary, engine with SEARCH_ADD_ON_LLM): 'Advanced Generative Answers' "
            "+$4.00 / 1,000 user input query, charged here on every summary-requesting query (conservative: the page "
            "lists summaries under Core Generative Answers included in Enterprise). No per-token charges."
        ),
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


def judge_cost_usd(model: str, *, input_tokens: int, output_tokens: int) -> float:
    """USD cost of one LLM-as-a-judge call from its measured input/output token counts."""
    for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be an integer >= 0, got {value!r}")
    price = price_for_model(model)
    total = (input_tokens / 1_000_000.0) * price.input_usd_per_1m + (output_tokens / 1_000_000.0) * price.output_usd_per_1m
    return round(total, 8)


RANK_DOCUMENTS_PER_COUNT = 100


def vais_query_cost_usd(*, searches: int, summary_queries: int) -> float:
    """USD of Vertex AI Search calls: Enterprise per search + the generative add-on per summary-requesting search."""
    for name, value in (("searches", searches), ("summary_queries", summary_queries)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be an integer >= 0, got {value!r}")
    if summary_queries > searches:
        raise ValueError("summary_queries cannot exceed searches")
    total = searches * PRICE_TABLE["vertex-ai-search-enterprise"].usd_per_1k_requests
    total += summary_queries * PRICE_TABLE["vertex-ai-search-summary"].usd_per_1k_requests
    return round(total / 1000.0, 8)


def rank_cost_usd(requests: int) -> float:
    """USD cost of ``requests`` billable Ranking API counts (see ``rank_billed_counts``)."""
    if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
        raise ValueError("requests must be an integer >= 0")
    return round((requests / 1000.0) * PRICE_TABLE["discovery-engine-rank"].usd_per_1k_requests, 8)


def rank_billed_counts(records: int) -> int:
    """Billable counts for ONE rank call with ``records`` documents: ceil(records / 100) (pricing page example)."""
    if not isinstance(records, int) or isinstance(records, bool) or records < 1:
        raise ValueError("records must be an integer >= 1")
    return -(-records // RANK_DOCUMENTS_PER_COUNT)


__all__ = [
    "ModelPrice",
    "PRICE_TABLE",
    "PricingError",
    "cost_from_usage",
    "judge_cost_usd",
    "price_for_model",
    "rank_billed_counts",
    "rank_cost_usd",
    "vais_query_cost_usd",
]
