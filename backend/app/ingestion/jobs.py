"""Visible ingestion jobs per level (spec §6.3): ``discover_docs → parse → chunk → embed → load_bigquery →
build_indexes → verify_counts`` streamed as stage events.

Phase 0: ``discover_docs``/``parse``/``chunk`` run for real against the local corpus; ``embed``/``load_bigquery``/
``build_indexes``/``verify_counts`` run behind the injectable ``Embedder`` / ``BigQueryLoader`` interfaces
(mocked in tests). The default factory raises until the live wiring lands in Phase 1, which surfaces as
``stage_failed`` + ``job_failed`` — never as a silent skip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, AsyncIterator, Callable, Protocol
import uuid

from app.core.config import ALL_LEVEL_IDS, ArenaSettings
from app.core.telemetry import BaseStageEmitter, stage_scope, utc_now_iso
from app.ingestion.parsers import chunk_pages, extract_pdf_pages, is_genuinely_blank_pdf, parser_backend_name
from app.ingestion.pipeline import chunk_to_bigquery_row
from app.recording.recorder import read_git_sha

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_DIR = _PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"
INGEST_STAGES: tuple[str, ...] = (
    "discover_docs",
    "parse",
    "chunk",
    "embed",
    "load_bigquery",
    "build_indexes",
    "verify_counts",
)
LEVEL_TIER_PROFILES: dict[str, str] = {
    "level1_naive": "naive",
    "level2_hybrid": "hybrid",
    "level3_multimodal": "multimodal",
    "level4_agentic": "multimodal",
    "managed_vertex": "hybrid",
}
_MAX_SOLICITATION_DIRS = 20
_MAX_PDFS_PER_SOLICITATION = 50
_MAX_JOBS = 20
_MAX_JOB_EVENTS = 5000
_EMBED_BATCH_SIZE = 32
_MAX_EMBED_BATCHES = 10_000
_STREAM_POLL_SECONDS = 1.0


class IngestBackendUnavailable(RuntimeError):
    """Raised when no live embedder/loader is wired for the current mode."""


class ParserUnavailableError(RuntimeError):
    """Live/record ingestion requires PyMuPDF (Level 1 spec); the regex fallback is for offline tests only."""


class EmptyDocumentsError(ValueError):
    """One or more non-blank PDFs yielded zero text pages (parse defect; never silently skipped)."""


class Embedder(Protocol):
    """Batch text embedder (live: gemini-embedding-2 via google-genai; tests: fake)."""

    model: str

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class BigQueryLoader(Protocol):
    """BigQuery side of ingestion (live: google-cloud-bigquery; tests: fake)."""

    table_fqn: str

    def ensure_schema(self) -> None: ...

    def load_rows(self, rows: list[dict[str, Any]]) -> int: ...

    def build_indexes(self) -> dict[str, str]: ...

    def count_by_tier(self) -> dict[str, int]: ...

    def index_states(self) -> dict[str, str]: ...

    def write_ingest_run(self, row: dict[str, Any]) -> None: ...

    def recent_ingest_runs(self, limit: int = 20) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class IngestBackends:
    embedder: Embedder
    loader: BigQueryLoader


BackendFactory = Callable[[ArenaSettings], IngestBackends]


def unavailable_backends(settings: ArenaSettings) -> IngestBackends:
    """Default factory: live BigQuery/embedding wiring is Phase 1; fail loud with the mode in the message."""
    raise IngestBackendUnavailable(
        f"No embedder/BigQuery loader is wired for DEMO_MODE={settings.demo_mode} yet (live wiring lands in Phase 1)"
    )


@dataclass
class IngestJob:
    """Mutable state of one ingestion job (bounded event buffer for late subscribers)."""

    job_id: str
    level: str
    tier_profile: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    counts_by_tier: dict[str, int] = field(default_factory=dict)
    chunks: int = 0
    error: str | None = None
    failed_stage: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    notify: asyncio.Event = field(default_factory=asyncio.Event)

    def append_event(self, event: dict[str, Any]) -> None:
        """Append to the bounded buffer (single source of truth for subscribers) and wake streamers."""
        if len(self.events) < _MAX_JOB_EVENTS:
            self.events.append(event)
        self.notify.set()

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "level": self.level,
            "tier_profile": self.tier_profile,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "counts_by_tier": dict(self.counts_by_tier),
            "chunks": self.chunks,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "events": len(self.events),
        }


@dataclass(frozen=True)
class _DiscoveredDoc:
    path: Path
    solicitation_id: str


def discover_corpus_docs(corpus_dir: Path) -> list[_DiscoveredDoc]:
    """List corpus PDFs deterministically (bounded dirs/files)."""
    if not corpus_dir.is_dir():
        raise FileNotFoundError(f"Corpus directory not found: {corpus_dir}")
    sol_dirs = sorted(p for p in corpus_dir.iterdir() if p.is_dir() and p.name.startswith("solicitation_"))
    docs: list[_DiscoveredDoc] = []
    for sol_dir in sol_dirs[:_MAX_SOLICITATION_DIRS]:
        sol_id = "_".join(sol_dir.name.split("_")[:2])
        for pdf in sorted(sol_dir.glob("*.pdf"))[:_MAX_PDFS_PER_SOLICITATION]:
            docs.append(_DiscoveredDoc(path=pdf, solicitation_id=sol_id))
    return docs


class IngestJobRunner:
    """Owns ingestion jobs, runs them as bounded asyncio tasks, and exposes their event streams."""

    def __init__(
        self,
        settings: ArenaSettings,
        *,
        backend_factory: BackendFactory = unavailable_backends,
        corpus_dir: Path | None = None,
    ) -> None:
        if not isinstance(settings, ArenaSettings):
            raise TypeError("settings must be an ArenaSettings instance")
        if not callable(backend_factory):
            raise TypeError("backend_factory must be callable")
        self.settings = settings
        self.backend_factory = backend_factory
        self.corpus_dir = DEFAULT_CORPUS_DIR if corpus_dir is None else corpus_dir
        self._jobs: dict[str, IngestJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, job_id: str) -> IngestJob:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise KeyError(f"Unknown ingestion job '{job_id}'")
        return self._jobs[job_id]

    def list_jobs(self) -> list[IngestJob]:
        return list(self._jobs.values())

    def start(self, level: str) -> IngestJob:
        """Create and schedule a job for ``level`` (must be a known level id)."""
        if level not in ALL_LEVEL_IDS:
            raise ValueError(f"Unknown level '{level}'; valid levels: {ALL_LEVEL_IDS}")
        if len(self._jobs) >= _MAX_JOBS:
            oldest_done = next((jid for jid, j in self._jobs.items() if j.done), None)
            if oldest_done is None:
                raise RuntimeError(f"Too many concurrent ingestion jobs (> {_MAX_JOBS})")
            self._jobs.pop(oldest_done)
            self._tasks.pop(oldest_done, None)
        job = IngestJob(job_id=f"ing_{uuid.uuid4().hex[:12]}", level=level, tier_profile=LEVEL_TIER_PROFILES[level])
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = asyncio.create_task(self._run_bounded(job))
        return job

    async def _put(self, job: IngestJob, event: dict[str, Any]) -> None:
        stamped = {"run_id": job.job_id, "job_id": job.job_id, "engine": job.level, **event, "ts": event.get("ts", utc_now_iso())}
        job.append_event(stamped)

    async def _run_bounded(self, job: IngestJob) -> None:
        try:
            await asyncio.wait_for(self._run(job), timeout=float(self.settings.ingest_job_timeout_seconds))
        except BaseException as exc:  # boundary: every failure becomes a visible job_failed event
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = utc_now_iso()
            await self._put(
                job,
                {"event": "job_failed", "error_type": type(exc).__name__, "error": str(exc), "failed_stage": job.failed_stage},
            )
        finally:
            job.notify.set()

    async def _run(self, job: IngestJob) -> None:
        job.status = "running"
        emitter = _JobEmitter(self, job)
        await self._put(job, {"event": "job_started", "level": job.level, "tier_profile": job.tier_profile})
        tracker = _StageTracker(job)
        async with tracker.scope(emitter, "discover_docs", "Discover corpus documents", "Local corpus", {"corpus_dir": str(self.corpus_dir)}) as sc:
            docs = await asyncio.to_thread(discover_corpus_docs, self.corpus_dir)
            sc.payload = {"docs": len(docs), "solicitations": len({d.solicitation_id for d in docs}), "doc_names": [d.path.name for d in docs[:50]]}
        pages_by_doc = await self._parse(job, emitter, tracker, docs)
        chunks = await self._chunk(job, emitter, tracker, docs, pages_by_doc)
        backends, vectors = await self._embed(emitter, tracker, chunks)
        await self._load(emitter, tracker, job, backends, chunks, vectors)
        async with tracker.scope(emitter, "build_indexes", "Build vector/search indexes", "BigQuery", {"table": backends.loader.table_fqn}) as sc:
            sc.payload = {"index_states": await asyncio.to_thread(backends.loader.build_indexes)}
        async with tracker.scope(emitter, "verify_counts", "Verify row counts", "BigQuery", {"table": backends.loader.table_fqn}) as sc:
            counts = await asyncio.to_thread(backends.loader.count_by_tier)
            sc.payload = {"counts_by_tier": counts}
        job.counts_by_tier = dict(counts)
        job.chunks = len(chunks)
        await asyncio.to_thread(
            backends.loader.write_ingest_run,
            {
                "run_id": job.job_id, "level": job.level, "tier_profile": job.tier_profile,
                "started_at": job.created_at, "finished_at": utc_now_iso(), "status": "completed",
                "docs": len(docs), "pages": sum(len(p) for p in pages_by_doc.values()), "chunks": len(chunks),
                "embedded": len(vectors), "loaded": len(chunks), "embedding_model": backends.embedder.model,
                "layout_source": parser_backend_name(), "git_sha": read_git_sha(),
                "stage_log": [r.to_dict() for r in emitter.records],
            },
        )
        job.status = "completed"
        job.finished_at = utc_now_iso()
        await self._put(job, {"event": "job_complete", "counts_by_tier": dict(counts), "chunks": len(chunks)})

    async def _parse(self, job: IngestJob, emitter: "_JobEmitter", tracker: "_StageTracker", docs: list[_DiscoveredDoc]) -> dict[str, list[list[str]]]:
        pages_by_doc: dict[str, list[list[str]]] = {}
        backend = parser_backend_name()
        async with tracker.scope(emitter, "parse", "Parse PDF pages", backend, {"docs": len(docs), "parser": backend}) as sc:
            if self.settings.is_live and backend != "pymupdf":
                raise ParserUnavailableError(
                    f"{job.level} ingestion requires PyMuPDF (pymupdf) in DEMO_MODE={self.settings.demo_mode}; "
                    f"active parser is '{backend}'. Install the declared dependency 'pymupdf>=1.24.0'."
                )
            for doc in docs:
                pages = await asyncio.to_thread(extract_pdf_pages, doc.path)
                pages_by_doc[doc.path.name] = pages
                await self._put(job, {"event": "doc_progress", "stage": "parse", "doc_name": doc.path.name, "pages": len(pages), "chunks": 0})
            empty_docs, blank_docs = await asyncio.to_thread(_classify_empty_docs, docs, pages_by_doc, backend)
            if empty_docs:
                raise EmptyDocumentsError(f"{len(empty_docs)} non-blank document(s) yielded 0 text pages; empty_docs={empty_docs}")
            sc.payload = {
                "docs": len(docs), "pages": sum(len(p) for p in pages_by_doc.values()), "parser": backend,
                "empty_docs": empty_docs, "blank_docs": blank_docs,
            }
        return pages_by_doc

    async def _chunk(self, job: IngestJob, emitter: "_JobEmitter", tracker: "_StageTracker", docs: list[_DiscoveredDoc], pages_by_doc: dict[str, list[list[str]]]) -> list[Any]:
        chunks: list[Any] = []
        async with tracker.scope(emitter, "chunk", f"Chunk ({job.tier_profile} profile)", "Ingestion job", {"tier_profile": job.tier_profile}) as sc:
            for doc in docs:
                doc_chunks = await asyncio.to_thread(
                    chunk_pages, pages_by_doc[doc.path.name], pdf_name=doc.path.name,
                    solicitation_id=doc.solicitation_id, tier_profile=job.tier_profile,
                )
                chunks.extend(doc_chunks)
                await self._put(job, {"event": "doc_progress", "stage": "chunk", "doc_name": doc.path.name, "pages": len(pages_by_doc[doc.path.name]), "chunks": len(doc_chunks)})
            sc.payload = {"chunks": len(chunks), "tier_profile": job.tier_profile, "element_types": _count_by(chunks, "element_type")}
        return chunks

    async def _embed(self, emitter: "_JobEmitter", tracker: "_StageTracker", chunks: list[Any]) -> tuple[IngestBackends, list[list[float]]]:
        vectors: list[list[float]] = []
        async with tracker.scope(emitter, "embed", "Embed chunks", "Vertex AI · gemini-embedding-2", {"chunks": len(chunks), "batch_size": _EMBED_BATCH_SIZE}) as sc:
            backends = self.backend_factory(self.settings)
            total = len(chunks)
            batches = 0
            for start in range(0, total, _EMBED_BATCH_SIZE):
                if batches >= _MAX_EMBED_BATCHES:
                    raise RuntimeError(f"embed exceeded {_MAX_EMBED_BATCHES} batches")
                texts = [c.content for c in chunks[start : start + _EMBED_BATCH_SIZE]]
                t0 = time.perf_counter()
                batch_vectors = await asyncio.to_thread(backends.embedder.embed_batch, texts)
                _check_batch_vectors(batch_vectors, texts, len(vectors[0]) if vectors else None)
                vectors.extend(batch_vectors)
                batches += 1
                await self._put(tracker.job, {"event": "embed_progress", "done": len(vectors), "total": total, "batch_ms": int((time.perf_counter() - t0) * 1000)})
            sc.payload = {
                "embedded": len(vectors), "model": backends.embedder.model, "dimensions": len(vectors[0]) if vectors else 0,
                "batches": batches, "task_type": getattr(backends.embedder, "task_type", None),
                "input_format": getattr(backends.embedder, "input_format", None),
            }
        return backends, vectors

    async def _load(self, emitter: "_JobEmitter", tracker: "_StageTracker", job: IngestJob, backends: IngestBackends, chunks: list[Any], vectors: list[list[float]]) -> None:
        async with tracker.scope(emitter, "load_bigquery", "Load rows", "BigQuery", {"table": backends.loader.table_fqn, "rows": len(chunks)}) as sc:
            rows = [
                chunk_to_bigquery_row(c, v, ingest_run_id=job.job_id, layout_source=parser_backend_name())
                for c, v in zip(chunks, vectors)
            ]
            await asyncio.to_thread(backends.loader.ensure_schema)
            loaded = await asyncio.to_thread(backends.loader.load_rows, rows)
            if loaded != len(rows):
                raise RuntimeError(f"BigQuery loader reported {loaded} rows loaded for {len(rows)} submitted")
            sc.payload = {"loaded": loaded, "table": backends.loader.table_fqn}

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered events, then live events until the terminal sentinel (bounded by frames + timeout)."""
        job = self.get(job_id)
        max_frames = self.settings.max_sse_frames
        deadline = time.monotonic() + float(self.settings.ingest_job_timeout_seconds) + 5.0
        idx = 0
        while idx < max_frames and time.monotonic() < deadline:
            if idx < len(job.events):
                yield job.events[idx]
                idx += 1
                continue
            if job.done:
                return
            job.notify.clear()
            waiter = asyncio.ensure_future(job.notify.wait())
            done, _ = await asyncio.wait({waiter}, timeout=_STREAM_POLL_SECONDS)
            if not done:
                waiter.cancel()
        yield {"event": "stream_warning", "run_id": job.job_id, "job_id": job.job_id, "engine": job.level, "message": f"Ingest stream bound reached ({max_frames} frames or timeout)", "ts": utc_now_iso()}

    async def status(self) -> dict[str, Any]:
        """``GET /api/v1/ingest/status``: live counts by tier + index states + ingest_runs provenance, or a reason."""
        base: dict[str, Any] = {"demo_mode": self.settings.demo_mode, **self._memory_runs()}
        try:
            backends = self.backend_factory(self.settings)
            counts = await asyncio.to_thread(backends.loader.count_by_tier)
            states = await asyncio.to_thread(backends.loader.index_states)
            if self.settings.is_live:
                runs = await asyncio.to_thread(backends.loader.recent_ingest_runs, 20)
                base.update({"last_runs": runs, "last_runs_source": "bigquery:ingest_runs"})
        except IngestBackendUnavailable as exc:
            return {**base, "available": False, "reason": str(exc), "counts_by_tier": {}, "index_states": {}}
        return {**base, "available": True, "counts_by_tier": counts, "index_states": states, "table": backends.loader.table_fqn}

    def _memory_runs(self) -> dict[str, Any]:
        runs = [
            {"run_id": j.job_id, "level": j.level, "tier_profile": j.tier_profile, "finished_at": j.finished_at, "status": j.status, "chunks": j.chunks}
            for j in list(self._jobs.values())[-20:]
        ]
        return {"last_runs": runs, "last_runs_source": f"in-memory jobs (DEMO_MODE={self.settings.demo_mode})"}


def _classify_empty_docs(
    docs: list[_DiscoveredDoc], pages_by_doc: dict[str, list[list[str]]], backend: str
) -> tuple[list[str], list[str]]:
    """Split zero-page docs into (empty = parse defect, blank = genuinely blank PDF; PyMuPDF decides blankness)."""
    empty: list[str] = []
    blank: list[str] = []
    for doc in docs:
        if pages_by_doc.get(doc.path.name):
            continue
        if backend == "pymupdf" and is_genuinely_blank_pdf(doc.path):
            blank.append(doc.path.name)
        else:
            empty.append(doc.path.name)
    return empty, blank


def _check_batch_vectors(batch_vectors: list[list[float]], texts: list[str], expected_dims: int | None) -> None:
    """One vector per text, all with the dimensionality of the first batch (fail loud on drift)."""
    if len(batch_vectors) != len(texts):
        raise ValueError(f"embedder returned {len(batch_vectors)} vectors for {len(texts)} texts")
    dims = {len(v) for v in batch_vectors}
    if expected_dims is not None:
        dims.add(expected_dims)
    if len(dims) != 1 or 0 in dims:
        raise ValueError(f"embedding dimension drift across batches: {sorted(dims)}")


class _JobEmitter(BaseStageEmitter):
    """Stage emitter writing into the job's bounded event buffer (subscribers stream from it)."""

    def __init__(self, runner: IngestJobRunner, job: IngestJob) -> None:
        super().__init__(run_id=job.job_id, engine=job.level)
        self._job = job

    async def _emit(self, event: dict[str, Any]) -> None:
        self._job.append_event({**event, "job_id": self._job.job_id})


class _StageTracker:
    """Remembers which stage is open so a failure can be attributed in ``job_failed.failed_stage``."""

    def __init__(self, job: IngestJob) -> None:
        self.job = job

    def scope(self, emitter: _JobEmitter, stage_id: str, label: str, service: str, request: dict[str, Any]):
        if stage_id not in INGEST_STAGES:
            raise ValueError(f"Unknown ingest stage '{stage_id}'")
        self.job.failed_stage = stage_id
        return stage_scope(emitter, stage_id, label=label, service=service, request=request)


def _count_by(items: list[Any], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items[:100_000]:
        key = str(getattr(item, attr, ""))
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "BackendFactory",
    "BigQueryLoader",
    "DEFAULT_CORPUS_DIR",
    "Embedder",
    "INGEST_STAGES",
    "IngestBackendUnavailable",
    "IngestBackends",
    "IngestJob",
    "IngestJobRunner",
    "LEVEL_TIER_PROFILES",
    "discover_corpus_docs",
    "unavailable_backends",
]
