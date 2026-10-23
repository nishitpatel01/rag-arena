"""Per-run state of one Level 4 agent run: every chunk a tool returned, every tool call (measured), retrieval usage,
calculator outputs and citation verifications. One ``AgentLedger`` is created per run and handed to the tool closures;
nothing here is global. All collections are bounded (overflow raises — fail loud, never silently drops)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.core.models import DocumentChunk
from app.engines.agent_verify import VerificationReport

MAX_LEDGER_CHUNKS = 200
MAX_TOOL_RECORDS = 64
MAX_SUMMARY_CHARS = 600


class AgentLedgerError(RuntimeError):
    """A per-run bound was exceeded (too many chunks / tool calls)."""


@dataclass(frozen=True)
class ToolRecord:
    """One executed tool call, measured inside the tool (``duration_ms``) and keyed by ADK's function_call_id."""

    call_id: str
    tool_name: str
    args: dict[str, Any]
    summary: str
    duration_ms: int
    status: str  # "ok" | "error" (argument error reported back to the model)
    chunk_ids: tuple[str, ...] = ()
    seq: int = 0  # call order (parallel calls of one turn finish in any order; steps are reported in call order)


@dataclass(frozen=True)
class RetrievalUsage:
    """Billable side effects of the tools: embedding tokens, Ranking API counts, BigQuery bytes."""

    embedding_tokens: int = 0
    rank_billed_counts: int = 0
    bigquery_bytes: int = 0
    bigquery_jobs: int = 0
    search_calls: int = 0


class AgentLedger:
    """Mutable, bounded accumulator for one run (tools write, the engine reads after the runner finishes)."""

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        self.run_id = run_id
        self._chunks: dict[str, DocumentChunk] = {}
        self._records: dict[str, ToolRecord] = {}
        self._usage = RetrievalUsage()
        self._calc_values: list[float] = []
        self._verifications: list[VerificationReport] = []

    # ---------------------------------------------------------------- chunks
    def register_chunks(self, chunks: Iterable[DocumentChunk]) -> tuple[str, ...]:
        ids: list[str] = []
        for chunk in chunks:
            if not isinstance(chunk, DocumentChunk):
                raise TypeError("register_chunks expects DocumentChunk instances")
            if chunk.chunk_id not in self._chunks and len(self._chunks) >= MAX_LEDGER_CHUNKS:
                raise AgentLedgerError(f"more than {MAX_LEDGER_CHUNKS} distinct chunks returned by tools in one run")
            self._chunks.setdefault(chunk.chunk_id, chunk)
            ids.append(chunk.chunk_id)
        return tuple(ids)

    def chunk(self, chunk_id: str) -> DocumentChunk | None:
        return self._chunks.get(chunk_id)

    @property
    def chunks(self) -> tuple[DocumentChunk, ...]:
        """Every distinct chunk returned by any tool, in first-returned order."""
        return tuple(self._chunks.values())

    # ---------------------------------------------------------------- tool calls
    def record_tool(self, record: ToolRecord) -> None:
        if not isinstance(record, ToolRecord):
            raise TypeError("record must be a ToolRecord")
        if len(self._records) >= MAX_TOOL_RECORDS:
            raise AgentLedgerError(f"more than {MAX_TOOL_RECORDS} tool calls in one run")
        self._records[record.call_id] = record

    def tool_record(self, call_id: str) -> ToolRecord | None:
        return self._records.get(call_id)

    @property
    def tool_records(self) -> tuple[ToolRecord, ...]:
        """Executed tool calls in call (start) order."""
        return tuple(sorted(self._records.values(), key=lambda r: r.seq))

    # ---------------------------------------------------------------- usage / calculator / verification
    def add_usage(self, *, embedding_tokens: int = 0, rank_counts: int = 0, bq_bytes: int = 0, bq_jobs: int = 0, search: bool = False) -> None:
        u = self._usage
        self._usage = RetrievalUsage(
            embedding_tokens=u.embedding_tokens + int(embedding_tokens),
            rank_billed_counts=u.rank_billed_counts + int(rank_counts),
            bigquery_bytes=u.bigquery_bytes + int(bq_bytes),
            bigquery_jobs=u.bigquery_jobs + int(bq_jobs),
            search_calls=u.search_calls + (1 if search else 0),
        )

    @property
    def usage(self) -> RetrievalUsage:
        return self._usage

    def add_calc_value(self, value: float) -> None:
        if len(self._calc_values) >= MAX_TOOL_RECORDS:
            raise AgentLedgerError("too many calculator results in one run")
        self._calc_values.append(float(value))

    @property
    def calc_values(self) -> tuple[float, ...]:
        return tuple(self._calc_values)

    def add_verification(self, report: VerificationReport) -> None:
        if len(self._verifications) >= MAX_TOOL_RECORDS:
            raise AgentLedgerError("too many verify_citations calls in one run")
        self._verifications.append(report)

    @property
    def verifications(self) -> tuple[VerificationReport, ...]:
        return tuple(self._verifications)


def bounded_summary(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 1] + "…"


__all__ = ["AgentLedger", "AgentLedgerError", "RetrievalUsage", "ToolRecord", "bounded_summary"]
