"""Level 3 (tier ``multimodal``) ingestion stage bodies: ``parse`` (Document AI Layout Parser + Gemini vision),
``chunk`` (element-aware) and ``embed`` (gemini-embedding-2 multimodal). The shared ``IngestJobRunner._finish`` tail
then loads (tier-scoped transactional replace), builds indexes (incl. ``embedding_multimodal``) and verifies counts.

Fail loud (spec P2): there is no PyMuPDF text fallback when Document AI is not configured or fails — the ``parse``
stage fails with the error. PyMuPDF is used only for (a) splitting PDFs into ≤15-page ranges, (b) rendering diagram
pages to PNG, (c) the vector-drawing diagram heuristic, (d) reading the corpus metadata line, (e) locating element
bboxes on the text layer — each reported in the payloads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any

from app.core.pricing import PRICE_TABLE
from app.ingestion.jobs import (
    IngestBackendUnavailable,
    IngestJob,
    IngestJobRunner,
    TierOutcome,
    _check_batch_vectors,
    _count_by,
    _DiscoveredDoc,
)
from app.ingestion.layout_documentai import LAYOUT_SOURCE, DocLayout
from app.ingestion.multimodal import (
    METADATA_SOURCE,
    VISION_LAYOUT_SOURCE,
    ChunkBuilder,
    MultimodalChunk,
    PageMeta,
    page_metadata,
)
from app.ingestion.vision_transcribe import (
    DIAGRAM_HEURISTIC,
    MAX_VISION_PAGES,
    VisionResult,
    VisionTranscriptionError,
    detect_diagram_pages,
    labels_in_text_layer,
    render_page_png,
)

DOCAI_CONCURRENCY = 4
VISION_CONCURRENCY = 4
EMBED_BATCH = 32
DOCAI_USD_PER_PAGE = 10.0 / 1000.0  # Layout Parser list price (cloud.google.com/document-ai/pricing, read 2026-10-22)
RELEASE_CANDIDATE_VERSIONS = ("pretrained-layout-parser-v1.5-2025-08-25", "pretrained-layout-parser-v1.5-pro-2025-08-25", "pretrained-layout-parser-v1.6-pro-2025-12-01")


@dataclass(frozen=True)
class ParsedDoc:
    doc: _DiscoveredDoc
    layout: DocLayout
    metas: dict[int, PageMeta]
    diagram_pages: dict[int, str]
    vision: dict[int, VisionResult]


def _require_backends(backends: Any, mode: str) -> None:
    missing = [n for n in ("layout_parser", "vision", "mm_embedder") if getattr(backends, n, None) is None]
    if missing:
        raise IngestBackendUnavailable(
            f"multimodal ingestion needs {missing} (set DOCAI_PROCESSOR_ID / DOCAI_LOCATION in .env; run "
            f"scripts/provision_documentai.py) — DEMO_MODE={mode}; no PyMuPDF fallback is used"
        )


def _version_note(version: str) -> str:
    if not version:
        return "processor default version"
    return "release candidate (preview)" if version in RELEASE_CANDIDATE_VERSIONS else "stable"


async def _layout_all(runner: IngestJobRunner, job: IngestJob, docs: list[_DiscoveredDoc], parser: Any) -> list[DocLayout]:
    sem = asyncio.Semaphore(DOCAI_CONCURRENCY)

    async def one(doc: _DiscoveredDoc) -> DocLayout:
        async with sem:
            data = await asyncio.to_thread(doc.path.read_bytes)
            layout = await asyncio.to_thread(parser.parse, doc.path.name, data)
        await runner._put(job, {
            "event": "doc_progress", "stage": "parse", "step": "documentai", "doc_name": doc.path.name,
            "pages": layout.pages, "chunks": 0, "blocks": len(layout.elements),
            "tables": sum(s.tables for s in layout.page_stats), "figures": sum(s.figures for s in layout.page_stats),
            "docai_ms": layout.docai_ms, "requests": layout.requests,
            "page_counts": [{"page": s.page_number, "blocks": s.blocks, "tables": s.tables, "figures": s.figures} for s in layout.page_stats],
        })
        return layout

    return list(await asyncio.gather(*(one(d) for d in docs)))


async def _vision_all(
    runner: IngestJobRunner, job: IngestJob, targets: list[tuple[_DiscoveredDoc, int, str]], vision: Any
) -> tuple[dict[tuple[str, int], VisionResult], list[dict[str, Any]]]:
    """Transcribe every target page. An UNUSABLE answer (``VisionTranscriptionError``: empty / not JSON / no labelled
    components) fails THAT page loudly — reported in ``vision_failed_pages`` and a ``doc_progress`` event with the reason,
    and no diagram chunk is created for it (nothing invented). API/transport errors still fail the whole stage."""
    sem = asyncio.Semaphore(VISION_CONCURRENCY)

    async def one(doc: _DiscoveredDoc, page: int, reason: str) -> tuple[tuple[str, int], VisionResult | dict[str, Any]]:
        base = {"event": "doc_progress", "stage": "parse", "step": "gemini_vision", "doc_name": doc.path.name, "pages": 0,
                "chunks": 0, "vision_page": page, "reason": reason}
        async with sem:
            png = await asyncio.to_thread(render_page_png, doc.path, page)
            try:
                result = await asyncio.to_thread(vision.transcribe, png, page, f"{doc.path.name} p.{page}")
            except VisionTranscriptionError as exc:
                failure = {"doc_name": doc.path.name, "page": page, "reason": reason, "status": "failed",
                           "error_type": type(exc).__name__, "error": str(exc)}
                await runner._put(job, {**base, "status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
                return (doc.path.name, page), failure
        await runner._put(job, {**base, "status": "ok", "components": len(result.spec.components),
                                "connections": len(result.spec.connections), "vision_ms": result.duration_ms})
        return (doc.path.name, page), result

    outcomes = await asyncio.gather(*(one(d, p, r) for d, p, r in targets))
    results = {key: value for key, value in outcomes if isinstance(value, VisionResult)}
    failures = [value for _, value in outcomes if isinstance(value, dict)]
    return results, failures


def _vision_cost(results: list[VisionResult], model: str) -> float:
    price = PRICE_TABLE.get(model)
    if price is None:
        return 0.0
    tokens_in = sum(r.prompt_tokens for r in results)
    tokens_out = sum(r.output_tokens + r.thinking_tokens for r in results)
    return round(tokens_in / 1e6 * price.input_usd_per_1m + tokens_out / 1e6 * price.output_usd_per_1m, 6)


def _page_text(pdf_path: Any, page: int) -> str:
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        return doc[page - 1].get_text("text", clip=pymupdf.INFINITE_RECT())


async def _parse(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, docs: list[_DiscoveredDoc]) -> tuple[Any, list[ParsedDoc], dict[str, Any]]:
    backends = runner.backend_factory(runner.settings)
    parser, vision = getattr(backends, "layout_parser", None), getattr(backends, "vision", None)
    request: dict[str, Any] = {"docs": len(docs), "diagram_heuristic": DIAGRAM_HEURISTIC, "max_vision_pages": MAX_VISION_PAGES}
    if parser is not None and vision is not None:
        request.update(layout_parser=parser.request_summary(), vision=vision.request_summary())
    service = f"Document AI Layout Parser · Vertex AI {getattr(vision, 'model', runner.settings.vision_model)}"
    async with tracker.scope(emitter, "parse", "Layout Parser + Gemini vision", service, request) as sc:
        _require_backends(backends, runner.settings.demo_mode)  # inside the stage → stage_failed + job_failed
        t0 = time.perf_counter()
        layouts = await _layout_all(runner, job, docs, parser)
        docai_wall_ms = int((time.perf_counter() - t0) * 1000)
        diagram_pages = [await asyncio.to_thread(detect_diagram_pages, d.path, list(range(1, lay.pages + 1)), lay.figure_pages) for d, lay in zip(docs, layouts)]
        targets = [(d, p, r) for d, pages in zip(docs, diagram_pages) for p, r in sorted(pages.items())]
        if len(targets) > MAX_VISION_PAGES:
            raise RuntimeError(f"{len(targets)} diagram pages exceed MAX_VISION_PAGES={MAX_VISION_PAGES}: {[(d.path.name, p) for d, p, _ in targets[:10]]}…")
        t1 = time.perf_counter()
        results, failures = await _vision_all(runner, job, targets, vision)
        vision_wall_ms = int((time.perf_counter() - t1) * 1000)
        parsed = [
            ParsedDoc(d, lay, await asyncio.to_thread(lambda d=d, lay=lay: page_metadata(d.path, lay.pages, upload=d.solicitation_id.startswith("upl_"))), dp,
                      {p: results[(d.path.name, p)] for p in dp if (d.path.name, p) in results})
            for d, lay, dp in zip(docs, layouts, diagram_pages)
        ]
        sc.payload = await _parse_payload(parsed, parser, vision, docai_wall_ms, vision_wall_ms)
        sc.payload["vision_failed_pages"] = failures
        sc.payload["vision_attempted_pages"] = len(targets)
    return backends, parsed, sc.payload


async def _parse_payload(parsed: list[ParsedDoc], parser: Any, vision: Any, docai_wall_ms: int, vision_wall_ms: int) -> dict[str, Any]:
    elements = [e for p in parsed for e in p.layout.elements]
    results = [r for p in parsed for r in p.vision.values()]
    pages = sum(p.layout.pages for p in parsed)
    vision_pages = []
    for p in parsed:
        for page, result in sorted(p.vision.items()):
            grounding = labels_in_text_layer(result.spec, await asyncio.to_thread(_page_text, p.doc.path, page))
            vision_pages.append({
                "doc_name": p.doc.path.name, "page": page, "reason": p.diagram_pages[page], "title": result.spec.title,
                "components": len(result.spec.components), "connections": len(result.spec.connections),
                "regions": len(result.spec.regions), "labels_in_text_layer": grounding, "vision_ms": result.duration_ms,
                "prompt_tokens": result.prompt_tokens, "output_tokens": result.output_tokens, "thinking_tokens": result.thinking_tokens,
                "attempts": result.attempts, "image_bytes": result.image_bytes, "transcription": result.spec.to_dict(),
            })
    skipped: dict[str, int] = {}
    for p in parsed:
        for key, value in p.layout.skipped_blocks.items():
            skipped[key] = skipped.get(key, 0) + value
    return {
        "docs": len(parsed), "pages": pages, "tables": sum(1 for e in elements if e.kind == "table"),
        "diagrams": len(results), "figures": sum(1 for e in elements if e.kind == "figure"),
        "vision_pages": vision_pages,
        "layout_source_counts": {LAYOUT_SOURCE: len(elements), VISION_LAYOUT_SOURCE: len(results)},
        "elements_by_kind": _count_by(elements, "kind"), "skipped_blocks": skipped,
        "docai_ms": sum(p.layout.docai_ms for p in parsed), "docai_wall_ms": docai_wall_ms,
        "vision_ms": sum(r.duration_ms for r in results), "vision_wall_ms": vision_wall_ms,
        "docai_requests": sum(p.layout.requests for p in parsed), "docai_attempts": sum(p.layout.attempts for p in parsed),
        "processor_id": parser.processor_id, "processor_version": parser.processor_version or "(processor default)",
        "processor_version_channel": _version_note(parser.processor_version), "vision_model": vision.model,
        "bbox_from_documentai": sum(1 for e in elements if e.bbox is not None),
        "cost_usd": {"documentai": round(pages * DOCAI_USD_PER_PAGE, 4), "vision": _vision_cost(results, vision.model)},
        "metadata_source": METADATA_SOURCE, "diagram_heuristic": DIAGRAM_HEURISTIC,
        "pymupdf_uses": ["split PDF into <=15-page ranges", "render diagram pages to PNG", "vector-drawing diagram heuristic",
                         "corpus metadata line", "locate element bbox on the text layer"],
    }


async def _chunk(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, parsed: list[ParsedDoc]) -> list[MultimodalChunk]:
    out: list[MultimodalChunk] = []
    bbox_sources: dict[str, int] = {}
    async with tracker.scope(emitter, "chunk", "Element-aware chunker (tables · diagrams · heading groups)", "Ingestion job", {"tier_profile": "multimodal"}) as sc:
        for p in parsed:
            builder = ChunkBuilder(pdf_path=p.doc.path, solicitation_id=p.doc.solicitation_id, metas=p.metas)
            doc_chunks = await asyncio.to_thread(builder.build, p.layout, p.vision, lambda n, _p=p.doc.path: render_page_png(_p, n))
            out.extend(doc_chunks)
            for key, value in builder.bbox_sources.items():
                bbox_sources[key] = bbox_sources.get(key, 0) + value
            await runner._put(job, {"event": "doc_progress", "stage": "chunk", "doc_name": p.doc.path.name, "pages": p.layout.pages, "chunks": len(doc_chunks)})
        chunks = [m.chunk for m in out]
        sc.payload = {
            "chunks": len(chunks), "tier_profile": "multimodal", "element_types": _count_by(chunks, "element_type"),
            "layout_sources": _count_by(chunks, "layout_source"), "bbox_sources": bbox_sources,
            "amendment_chunks": sum(1 for c in chunks if c.amendment_number > 0), "metadata_source": METADATA_SOURCE,
            "max_narrative_chars": 1800, "image_items": sum(1 for m in out if m.item.image_png is not None),
        }
    return out


async def _embed(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, embedder: Any, items: list[MultimodalChunk]) -> list[list[float]]:
    vectors: list[list[float]] = []
    request = {"chunks": len(items), "batch_size": EMBED_BATCH, "model": embedder.model, "task_type": embedder.task_type, "column": "embedding_multimodal"}
    async with tracker.scope(emitter, "embed", "Embed chunks (text · text+image)", f"Vertex AI · {embedder.model}", request) as sc:
        batches = 0
        for start in range(0, len(items), EMBED_BATCH):
            batch = [m.item for m in items[start : start + EMBED_BATCH]]
            t0 = time.perf_counter()
            got = await asyncio.to_thread(embedder.embed_items, batch)
            _check_batch_vectors(got, batch, len(vectors[0]) if vectors else None)  # type: ignore[arg-type]
            vectors.extend(got)
            batches += 1
            await runner._put(job, {"event": "embed_progress", "done": len(vectors), "total": len(items), "batch_ms": int((time.perf_counter() - t0) * 1000)})
        sc.payload = {
            "embedded": len(vectors), "model": embedder.model, "dimensions": len(vectors[0]) if vectors else 0, "batches": batches,
            "task_type": embedder.task_type, "input_format": embedder.input_format, "column": "embedding_multimodal",
            "image_items": sum(1 for m in items if m.item.image_png is not None), "calls": len(items),
        }
    return vectors


async def run_multimodal_stages(runner: IngestJobRunner, job: IngestJob, emitter: Any, tracker: Any, docs: list[_DiscoveredDoc]) -> TierOutcome:
    """parse → chunk → embed for tier 'multimodal'; returns what the shared tail loads and indexes."""
    backends, parsed, parse_payload = await _parse(runner, job, emitter, tracker, docs)
    items = await _chunk(runner, job, emitter, tracker, parsed)
    vectors = await _embed(runner, job, emitter, tracker, backends.mm_embedder, items)
    version = parse_payload["processor_version"]
    return TierOutcome(
        backends=backends, docs=len(docs), pages=parse_payload["pages"], chunks=[m.chunk for m in items], vectors=vectors,
        layout_source=f"{LAYOUT_SOURCE}({version})+{VISION_LAYOUT_SOURCE}({parse_payload['vision_model']})",
        index_columns=("embedding", "embedding_multimodal"), embedding_model=backends.mm_embedder.model,
    )


__all__ = ["ParsedDoc", "run_multimodal_stages"]
