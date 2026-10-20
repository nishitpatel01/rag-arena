"""Shared offline fakes: mocked google-genai / BigQuery clients that return SDK-shaped objects (no GCP calls)."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import time
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

from app.core import gcp_clients
from app.core.config import ArenaSettings

EMBED_DIMS = 3072
# Simulated SDK latencies (seconds) so dev fixtures carry visible t_rel_ms pacing; clearly synthetic.
FAKE_LATENCY = {"embed_content": 0.06, "query": 0.18, "generate_content": 0.25, "judge": 0.12}
_FAKE_ROWS: list[dict[str, Any]] = [
    {
        "chunk_id": "solicitation_01_04_Pricing_Schedule_CLINs_p2_w3",
        "solicitation_id": "solicitation_01",
        "doc_name": "04_Pricing_Schedule_CLINs.pdf",
        "doc_type": "PRICING_SCHEDULE",
        "element_type": "narrative_text",
        "section_id": "CLIN-0004-AB",
        "amendment_number": 0,
        "effective_date": "2026-01-15",
        "amends_section": None,
        "page_number": 2,
        "bbox": {"page": 2, "x0": 0.08, "y0": 0.31, "x1": 0.92, "y1": 0.46},
        "content": "CLIN-0004-AB Senior Cloud Architect labor rate $210.00/hr base period, 2,000 hours estimated.",
        "similarity_score": 0.8123,
    },
    {
        "chunk_id": "solicitation_01_06_Amendment_0001_Scope_Update_p1_w1",
        "solicitation_id": "solicitation_01",
        "doc_name": "06_Amendment_0001_Scope_Update.pdf",
        "doc_type": "AMENDMENT",
        "element_type": "narrative_text",
        "section_id": "AMD-0001-A",
        "amendment_number": 1,
        "effective_date": "2026-02-10",
        "amends_section": "CLIN-0004-AB",
        "page_number": 1,
        "bbox": {"page": 1, "x0": 0.08, "y0": 0.22, "x1": 0.92, "y1": 0.40},
        "content": "Amendment 0001 revises CLIN-0004-AB to $245.00/hr with a 5% volume discount above 1,500 hours.",
        "similarity_score": 0.7791,
    },
]


class FakeEmbeddings:
    def __init__(self) -> None:
        self.embeddings = [
            SimpleNamespace(values=[0.001 * (i % 7) for i in range(EMBED_DIMS)], statistics=SimpleNamespace(token_count=14))
        ]


class FakeGenerateResponse:
    def __init__(self, text: str, prompt_tokens: int = 1842, output_tokens: int = 96, thinking: int = 230) -> None:
        self.text = text
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=prompt_tokens, candidates_token_count=output_tokens, thoughts_token_count=thinking
        )


class FakeGenaiClient:
    """Minimal google-genai shape: ``models.embed_content`` / ``models.generate_content``."""

    def __init__(self, answer: str = "The base rate for CLIN-0004-AB is $210.00/hr (amended to $245.00/hr).") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answer = answer
        self.models = SimpleNamespace(embed_content=self._embed, generate_content=self._generate)

    def _embed(self, **kwargs: Any) -> FakeEmbeddings:
        self.calls.append(("embed_content", kwargs))
        time.sleep(FAKE_LATENCY["embed_content"])
        return FakeEmbeddings()

    def _generate(self, **kwargs: Any) -> FakeGenerateResponse:
        self.calls.append(("generate_content", kwargs))
        if kwargs.get("config", {}).get("response_mime_type") == "application/json":
            time.sleep(FAKE_LATENCY["judge"])
            return FakeGenerateResponse('{"groundedness": 0.81, "qa_relevance": 0.64, "context_precision": 0.58}', 900, 24, 0)
        time.sleep(FAKE_LATENCY["generate_content"])
        return FakeGenerateResponse(self._answer)


class FakeQueryJob:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.job_id = "bqjob_fake_0001"
        self.total_bytes_processed = 12_582_912
        self.slot_millis = 1840
        self.cache_hit = False

    def result(self, timeout: int | None = None) -> list[SimpleNamespace]:
        time.sleep(FAKE_LATENCY["query"])
        return [SimpleNamespace(**r) for r in self._rows]


class FakeBigQueryClient:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = _FAKE_ROWS if rows is None else rows
        self.queries: list[tuple[str, Any]] = []

    def query(self, sql: str, job_config: Any = None) -> FakeQueryJob:
        self.queries.append((sql, job_config))
        return FakeQueryJob(self.rows)


@contextmanager
def fake_clients(
    genai: FakeGenaiClient | None = None, bq: FakeBigQueryClient | None = None
) -> Iterator[tuple[FakeGenaiClient, FakeBigQueryClient]]:
    """Patch the SDK client factories used by engines/evaluator so no GCP call can happen."""
    genai_client = genai or FakeGenaiClient()
    bq_client = bq or FakeBigQueryClient()
    gcp_clients.clear_client_cache()
    targets = (
        "app.core.gcp_clients.get_genai_client",
        "app.engines.level1_naive.get_genai_client",
        "app.evaluation.evaluator.get_genai_client",
    )
    bq_targets = ("app.core.gcp_clients.get_bigquery_client", "app.engines.level1_naive.get_bigquery_client")
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, lambda settings, _c=genai_client: _c))
        for target in bq_targets:
            stack.enter_context(patch(target, lambda settings, _c=bq_client: _c))
        yield genai_client, bq_client


def live_settings(**overrides: Any) -> ArenaSettings:
    base: dict[str, Any] = {"demo_mode": "live", "gcp_project_id": "test-project", "enabled_levels": ("level1_naive",)}
    base.update(overrides)
    return ArenaSettings(**base)


def run(coro):  # noqa: ANN001 - test helper
    return asyncio.run(coro)
