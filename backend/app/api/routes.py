"""Starlette routes for The RAG Arena v3 (spec §6.3).

``POST /api/v1/battle/stream`` is a pure forwarder: engines emit stage events through a ``QueueStageEmitter``;
this module forwards them and appends ``run_started``, ``answer_complete``, ``eval_complete``, ``engine_error``,
``stream_warning`` and ``battle_complete``. It owns no stage logic and no timers (spec P1).
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
import tempfile
from typing import Any, AsyncIterator
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api.query_runs import QueryRunWriter, build_query_run_row, run_cost_usd, write_query_run_or_warn
from app.core.config import ArenaSettings, get_settings
from app.core.models import EngineResult, EvalOutcome
from app.core.telemetry import QueueStageEmitter, utc_now_iso
from app.engines import BaseRAGEngine, get_engine_registry, list_levels
from app.engines.replay_engine import ReplayUnavailableError, recording_store_for
from app.evaluation.evaluator import run_evaluation
from app.evaluation.scoring import leaderboard_from_recordings
from app.ingestion.jobs import IngestJobRunner
from app.ingestion.parsers import parse_pdf_Fallback_or_fitz
from app.ingestion.session_store import store_uploaded_session_chunks
from app.ingestion.upload_guard import UploadGuardError, validate_pdf_upload
from app.recording.codec import eval_outcome_to_dict
from app.recording.recorder import build_recording, record_run

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BENCHMARK_PATH = _PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
_MAX_ENGINES_PER_BATTLE = 5
_MAX_QUERY_CHARS = 4000
_MAX_MULTIPART_PARTS = 16
_MAX_CITATIONS_IN_FRAME = 10
_QUEUE_IDLE_GRACE_SECONDS = 10.0
_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename="([^"]+)"', re.IGNORECASE)


def _format_sse_event(payload: dict[str, Any]) -> str:
    """Format a dictionary payload as a standard Server-Sent Events (SSE) data frame."""
    if not isinstance(payload, dict) or "event" not in payload:
        raise ValueError("SSE payload must be a dictionary containing an 'event' key")
    return f"data: {json.dumps(payload)}\n\n"


def _settings_of(request: Request) -> ArenaSettings:
    """Settings bound to the app at creation (tests inject them); falls back to environment settings."""
    bound = getattr(request.app.state, "settings", None)
    return bound if isinstance(bound, ArenaSettings) else get_settings()


def _validate_battle_payload(
    body: dict[str, Any],
    registry: dict[str, BaseRAGEngine],
) -> tuple[str, str | None, list[str], str | None]:
    """Validate incoming battle request JSON and return (query, question_id, engines, solicitation_filter)."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    if not isinstance(registry, dict) or not registry:
        raise ValueError("No engines are enabled (check ENABLED_LEVELS)")

    raw_query = body.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise ValueError("Field 'query' must be a non-empty string")
    query = raw_query.strip()
    if len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"Field 'query' exceeds maximum length of {_MAX_QUERY_CHARS} characters")

    raw_engines = body.get("engines")
    if raw_engines is None:
        selected = list(registry.keys())
    elif isinstance(raw_engines, list) and len(raw_engines) > 0:
        selected = [str(e).strip() for e in raw_engines[:_MAX_ENGINES_PER_BATTLE]]
    else:
        raise ValueError("Field 'engines' must be a non-empty list of engine IDs when provided")

    unknown = [eng for eng in selected if eng not in registry]
    if unknown:
        raise ValueError(f"Unknown or disabled engine ID(s): {unknown}. Enabled engines: {list(registry.keys())}")

    q_id = str(body["question_id"]).strip() if body.get("question_id") else None
    sol_filter = str(body["solicitation_filter"]).strip() if body.get("solicitation_filter") else None
    return query, q_id, selected, sol_filter


def _answer_complete_event(run_id: str, result: EngineResult) -> dict[str, Any]:
    return {
        "event": "answer_complete",
        "run_id": run_id,
        "engine": result.engine_id,
        "engine_label": result.engine_label,
        "answer": result.answer,
        "citations": [c.to_dict() for c in result.citations[:_MAX_CITATIONS_IN_FRAME]],
        "retrieved_chunks": [c.to_dict() for c in result.retrieved_chunks[:_MAX_CITATIONS_IN_FRAME]],
        "agent_steps": [s.to_dict() for s in result.agent_steps[:15]],
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "usage": result.usage.to_dict(),
        "flags": result.flags.to_dict(),
        "ts": utc_now_iso(),
    }


def _eval_complete_event(run_id: str, result: EngineResult, outcome: EvalOutcome) -> dict[str, Any]:
    """``eval_complete`` with the judge outcome; ``run_cost_usd`` = engine cost + judge cost (both measured)."""
    return {
        "event": "eval_complete",
        "run_id": run_id,
        "engine": result.engine_id,
        **eval_outcome_to_dict(outcome),
        "run_cost_usd": run_cost_usd(result, outcome),
        "ts": utc_now_iso(),
    }


def _engine_error_event(run_id: str, engine_id: str, exc: BaseException, failed_stage: str | None) -> dict[str, Any]:
    message = str(exc).strip() or type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError):
        message = "Engine exceeded QUERY_TIMEOUT_SECONDS"
    return {
        "event": "engine_error",
        "run_id": run_id,
        "engine": engine_id,
        "error_type": type(exc).__name__,
        "error": message,
        "failed_stage": failed_stage,
        "ts": utc_now_iso(),
    }


def _last_failed_stage(emitter: QueueStageEmitter) -> str | None:
    failed = [r.stage for r in emitter.records if r.status == "failed"]
    return failed[-1] if failed else emitter.open_stage


async def _maybe_record(
    settings: ArenaSettings,
    *,
    run_id: str,
    engine_id: str,
    question_id: str | None,
    query: str,
    emitter: QueueStageEmitter,
    result: EngineResult,
    outcome: EvalOutcome,
    queue: asyncio.Queue,
) -> None:
    """DEMO_MODE=record: persist the run under data/recordings/<engine>/<question_id>.json."""
    if settings.demo_mode != "record":
        return
    if not question_id:
        await queue.put({"event": "stream_warning", "run_id": run_id, "message": f"record mode: no question_id, {engine_id} run not recorded", "ts": utc_now_iso()})
        return
    recording = build_recording(
        settings=settings, engine_id=engine_id, question_id=question_id, query=query, run_id=run_id,
        events=emitter.events, result=result, evaluation=outcome,
    )
    path = await asyncio.to_thread(record_run, recording_store_for(settings), recording)
    await queue.put({"event": "recording_saved", "run_id": run_id, "engine": engine_id, "path": str(path), "ts": utc_now_iso()})


async def _run_single_engine_worker(
    run_id: str,
    engine: BaseRAGEngine,
    query: str,
    question_id: str | None,
    solicitation_filter: str | None,
    settings: ArenaSettings,
    queue: asyncio.Queue,
    query_run_writer: QueryRunWriter | None = None,
) -> bool:
    """Run one engine + judge with a bounded timeout; forward emitter events; return True on success."""
    engine_id = engine.engine_id
    emitter = QueueStageEmitter(queue, run_id=run_id, engine=engine_id)
    timeout = float(settings.query_timeout_seconds)
    started_at = utc_now_iso()
    try:
        result = await asyncio.wait_for(
            engine.run_query(query, question_id=question_id, solicitation_filter=solicitation_filter, emitter=emitter),
            timeout=timeout,
        )
        await queue.put(_answer_complete_event(run_id, result))
        outcome = await asyncio.wait_for(
            run_evaluation(query, result, question_id=question_id, settings=settings, emitter=emitter),
            timeout=timeout,
        )
        await queue.put(_eval_complete_event(run_id, result, outcome))
        await _maybe_record(
            settings, run_id=run_id, engine_id=engine_id, question_id=question_id, query=query,
            emitter=emitter, result=result, outcome=outcome, queue=queue,
        )
    except ReplayUnavailableError as exc:
        await queue.put(_engine_error_event(run_id, engine_id, exc, _last_failed_stage(emitter)))
        return False
    except Exception as exc:  # boundary: every failure is surfaced as engine_error (spec P2), never swallowed
        await queue.put(_engine_error_event(run_id, engine_id, exc, _last_failed_stage(emitter)))
        return False
    if settings.is_live and query_run_writer is not None:
        row = build_query_run_row(
            settings=settings, run_id=run_id, question_id=question_id, query=query, started_at=started_at,
            emitter=emitter, result=result, outcome=outcome,
        )
        await write_query_run_or_warn(query_run_writer, row, queue=queue, run_id=run_id, engine_id=engine_id)
    return True


async def _stream_battle_events(
    query: str,
    question_id: str | None,
    selected_engines: list[str],
    solicitation_filter: str | None,
    registry: dict[str, BaseRAGEngine],
    settings: ArenaSettings,
    query_run_writer: QueryRunWriter | None = None,
) -> AsyncIterator[str]:
    """Run selected engines concurrently and forward their events as SSE frames (bounded by MAX_SSE_FRAMES)."""
    if not query.strip() or not selected_engines:
        raise ValueError("query and selected_engines must be non-empty")
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    queue: asyncio.Queue = asyncio.Queue()
    yield _format_sse_event(
        {"event": "run_started", "run_id": run_id, "engines": list(selected_engines), "demo_mode": settings.demo_mode, "question_id": question_id, "ts": utc_now_iso()}
    )
    tasks = [
        asyncio.create_task(
            _run_single_engine_worker(
                run_id, registry[eng_id], query, question_id, solicitation_filter, settings, queue, query_run_writer
            )
        )
        for eng_id in selected_engines[:_MAX_ENGINES_PER_BATTLE]
    ]

    async def _join_workers() -> list[bool]:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)
        return [item is True for item in outcomes]

    join_task = asyncio.create_task(_join_workers())
    frame_budget = settings.max_sse_frames - 3  # run_started + optional stream_warning + battle_complete
    frames = 0
    reached_sentinel = False
    idle_timeout = 2.0 * float(settings.query_timeout_seconds) + _QUEUE_IDLE_GRACE_SECONDS
    while frames < frame_budget:
        item = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
        if item is None:
            reached_sentinel = True
            break
        yield _format_sse_event(item)
        frames += 1
    if not reached_sentinel:
        yield _format_sse_event(
            {"event": "stream_warning", "run_id": run_id, "message": f"MAX_SSE_FRAMES ({settings.max_sse_frames}) reached; remaining events truncated.", "ts": utc_now_iso()}
        )
    successes = await join_task
    completed = [eng for eng, ok in zip(selected_engines, successes) if ok]
    failed = [eng for eng, ok in zip(selected_engines, successes) if not ok]
    yield _format_sse_event(
        {"event": "battle_complete", "run_id": run_id, "engines_completed": completed, "engines_failed": failed, "ts": utc_now_iso()}
    )


async def health_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/health: runtime health, mode, project/bucket (frontend never hardcodes them), models, levels."""
    settings = _settings_of(request)
    return JSONResponse(
        {
            "status": "ok",
            "app_version": settings.app_version,
            "demo_mode": settings.demo_mode,
            "project_id": settings.gcp_project_id,
            "gcs_bucket": settings.gcs_corpus_bucket,
            "enabled_levels": list(settings.enabled_levels),
            "replay_speed": settings.replay_speed,
            "gemini_fast_model": settings.gemini_fast_model,
            "gemini_pro_model": settings.gemini_pro_model,
            "gemini_thinking_level": settings.gemini_thinking_level,
            "text_embedding_model": settings.text_embedding_model,
            "judge_provider": settings.judge_provider,
            "judge_model": settings.judge_model,
            "judge_location": settings.judge_location,
        }
    )


async def benchmarks_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/benchmarks: Return the golden procurement benchmark catalog."""
    if not _BENCHMARK_PATH.exists():
        return JSONResponse({"error": "Benchmark catalog file not found"}, status_code=500)
    data = json.loads(_BENCHMARK_PATH.read_text(encoding="utf-8"))
    return JSONResponse(
        {
            "benchmark_version": data.get("benchmark_version", "2.0.0"),
            "domain": data.get("domain", "Enterprise IT & Cloud Procurement"),
            "failure_mode_categories": data.get("failure_mode_categories", []),
            "questions": data.get("questions", []),
            "question_count": len(data.get("questions", [])),
        }
    )


async def levels_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/levels: all five levels with ``enabled`` flag, stages and ArchitectureDescriptor."""
    settings = _settings_of(request)
    return JSONResponse({"enabled_levels": list(settings.enabled_levels), "demo_mode": settings.demo_mode, "levels": list_levels(settings)})


async def leaderboard_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/leaderboard: aggregates of recorded live runs with provenance (empty until recordings exist)."""
    settings = _settings_of(request)
    try:
        summary = await asyncio.to_thread(leaderboard_from_recordings, recording_store_for(settings))
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": f"Leaderboard unavailable: {exc}"}, status_code=500)
    return JSONResponse(summary)


async def battle_stream_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """POST /api/v1/battle/stream: validate the request and stream v3 SSE events."""
    try:
        body = await request.json()
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid JSON request body: {exc}"}, status_code=400)

    settings = _settings_of(request)
    registry = get_engine_registry(settings)
    try:
        query, q_id, selected_engines, sol_filter = _validate_battle_payload(body, registry)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    event_generator = _stream_battle_events(
        query=query, question_id=q_id, selected_engines=selected_engines,
        solicitation_filter=sol_filter, registry=registry, settings=settings,
        query_run_writer=getattr(request.app.state, "query_run_writer", None),
    )
    return StreamingResponse(
        event_generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _ingest_runner(request: Request) -> IngestJobRunner:
    runner = getattr(request.app.state, "ingest_runner", None)
    if not isinstance(runner, IngestJobRunner):
        raise RuntimeError("IngestJobRunner is not attached to app.state (create the app via create_app)")
    return runner


async def ingest_jobs_create_endpoint(request: Request) -> JSONResponse:
    """POST /api/v1/ingest/jobs {level} → {job_id}."""
    try:
        body = await request.json()
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid JSON request body: {exc}"}, status_code=400)
    level = str(body.get("level", "")).strip() if isinstance(body, dict) else ""
    try:
        job = _ingest_runner(request).start(level)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"job_id": job.job_id, "level": job.level, "tier_profile": job.tier_profile, "status": job.status}, status_code=202)


async def ingest_job_stream_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """GET /api/v1/ingest/jobs/{job_id}/stream: SSE of job_started, stage_*, doc_progress, embed_progress, job_complete."""
    runner = _ingest_runner(request)
    job_id = str(request.path_params.get("job_id", ""))
    try:
        runner.get(job_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    async def _frames() -> AsyncIterator[str]:
        async for event in runner.stream(job_id):
            yield _format_sse_event(event)

    return StreamingResponse(_frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def ingest_job_get_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/ingest/jobs/{job_id}: job status snapshot."""
    try:
        job = _ingest_runner(request).get(str(request.path_params.get("job_id", "")))
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(job.to_dict())


async def ingest_status_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/ingest/status: counts by tier + index states (or an explicit unavailability reason)."""
    return JSONResponse(await _ingest_runner(request).status())


async def _read_bounded_request_body(request: Request, max_bytes: int) -> bytes:
    """Pre-check Content-Length and read request stream with a strict byte ceiling."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    wire_ceiling = int(max_bytes * 1.4) + 65536
    content_len_hdr = request.headers.get("content-length")
    if content_len_hdr is not None:
        try:
            declared_len = int(content_len_hdr.strip())
        except ValueError as exc:
            raise UploadGuardError("Invalid Content-Length header") from exc
        if declared_len > wire_ceiling:
            raise UploadGuardError(
                f"Upload Content-Length ({declared_len} bytes) exceeds maximum allowed limit ({max_bytes} bytes)"
            )

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > wire_ceiling:
            raise UploadGuardError(f"Upload payload stream exceeds maximum allowed limit ({max_bytes} bytes)")
    return bytes(buf)


def _extract_multipart_pdf_part(raw_body: bytes, content_type: str, default_filename: str) -> tuple[str, bytes]:
    """Parse MIME multipart/form-data boundaries to isolate the uploaded PDF file part."""
    boundary_match = _BOUNDARY_RE.search(content_type)
    boundary_bytes: bytes | None = None
    if boundary_match:
        b_str = boundary_match.group(1) or boundary_match.group(2)
        boundary_bytes = f"--{b_str}".encode("latin-1")
    elif raw_body.startswith(b"--"):
        first_line = raw_body.split(b"\r\n", 1)[0].strip()
        if first_line:
            boundary_bytes = first_line

    if boundary_bytes:
        parts = raw_body.split(boundary_bytes)[:_MAX_MULTIPART_PARTS]
        for part in parts:
            if not part or part.startswith(b"--"):
                continue
            sep = b"\r\n\r\n" if b"\r\n\r\n" in part else (b"\n\n" if b"\n\n" in part else None)
            if sep is None:
                continue
            hdr_bytes, part_body = part.split(sep, 1)
            hdr_text = hdr_bytes.decode("latin-1", errors="ignore")
            fn_match = _FILENAME_RE.search(hdr_text)
            if fn_match or "application/pdf" in hdr_text.lower():
                filename = (fn_match.group(1).strip() if fn_match else default_filename) or default_filename
                if part_body.endswith(b"\r\n"):
                    part_body = part_body[:-2]
                elif part_body.endswith(b"\n"):
                    part_body = part_body[:-1]
                pdf_start = part_body.find(b"%PDF-")
                pdf_eof = part_body.rfind(b"%%EOF")
                if pdf_start != -1 and pdf_eof != -1 and pdf_eof > pdf_start:
                    return filename, part_body[pdf_start : pdf_eof + 5]
                return filename, part_body

    pdf_start = raw_body.find(b"%PDF-")
    pdf_eof = raw_body.rfind(b"%%EOF")
    if pdf_start != -1 and pdf_eof != -1 and pdf_eof > pdf_start:
        return default_filename, raw_body[pdf_start : pdf_eof + 5]
    raise UploadGuardError("Multipart upload does not contain a valid %PDF-...%%EOF stream")


async def _extract_upload_payload(request: Request, max_bytes: int) -> tuple[str, bytes]:
    """Extract filename and raw PDF bytes from JSON base64, multipart/form-data, or raw binary body."""
    content_type = request.headers.get("content-type", "")
    raw_body = await _read_bounded_request_body(request, max_bytes=max_bytes)
    if not raw_body:
        raise UploadGuardError("Request body is empty")

    lower_ct = content_type.lower()
    if "application/json" in lower_ct:
        try:
            data = json.loads(raw_body.decode("utf-8"))
            filename = str(data.get("filename", "uploaded_document.pdf")).strip()
            b64_str = str(data.get("content_base64", "")).strip()
            return filename, base64.b64decode(b64_str, validate=True)
        except (ValueError, AttributeError, UnicodeDecodeError) as exc:
            raise UploadGuardError(f"Invalid base64 JSON upload payload: {exc}") from exc

    default_filename = request.headers.get("x-filename", "uploaded_document.pdf").strip() or "uploaded_document.pdf"
    if "multipart/form-data" in lower_ct:
        return _extract_multipart_pdf_part(raw_body, content_type, default_filename)
    return default_filename, raw_body


async def ingest_upload_endpoint(request: Request) -> JSONResponse:
    """POST /api/v1/ingest/upload: guard + parse an uploaded PDF across tiers (parsed only — NOT embedded/indexed)."""
    settings = _settings_of(request)
    try:
        filename, raw_bytes = await _extract_upload_payload(request, max_bytes=settings.max_upload_bytes)
        meta = validate_pdf_upload(
            raw_bytes=raw_bytes, filename=filename, max_bytes=settings.max_upload_bytes, max_pages=settings.max_upload_pages
        )
        safe_name = Path(meta.filename).name
        sol_id = f"upload_{meta.session_id[:8]}"
        with tempfile.TemporaryDirectory(prefix="rag_arena_upload_") as tmp_dir:
            tmp_pdf = Path(tmp_dir) / safe_name
            tmp_pdf.write_bytes(raw_bytes)
            chunks_by_tier = {
                tier: parse_pdf_Fallback_or_fitz(pdf_path=tmp_pdf, solicitation_id=sol_id, tier_profile=tier)
                for tier in ("naive", "hybrid", "multimodal")
            }
        persisted_sol_id = store_uploaded_session_chunks(meta.session_id, chunks_by_tier)
    except (UploadGuardError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(
        {
            "status": "parsed",
            "indexed": False,
            "note": "Uploaded PDFs are parsed and kept in a session store; BigQuery indexing of uploads is not available yet.",
            "solicitation_id": persisted_sol_id,
            "upload": meta.to_dict(),
            "chunks_parsed": {tier: len(chunks) for tier, chunks in chunks_by_tier.items()},
            "sample_chunks": [c.to_dict() for c in chunks_by_tier["multimodal"][:3]],
        }
    )


def get_api_routes() -> list[Route]:
    """Return the Starlette Route table for The RAG Arena v1 API."""
    routes = [
        Route("/api/v1/health", endpoint=health_endpoint, methods=["GET"]),
        Route("/api/v1/benchmarks", endpoint=benchmarks_endpoint, methods=["GET"]),
        Route("/api/v1/levels", endpoint=levels_endpoint, methods=["GET"]),
        Route("/api/v1/leaderboard", endpoint=leaderboard_endpoint, methods=["GET"]),
        Route("/api/v1/battle/stream", endpoint=battle_stream_endpoint, methods=["POST"]),
        Route("/api/v1/ingest/jobs", endpoint=ingest_jobs_create_endpoint, methods=["POST"]),
        Route("/api/v1/ingest/jobs/{job_id}", endpoint=ingest_job_get_endpoint, methods=["GET"]),
        Route("/api/v1/ingest/jobs/{job_id}/stream", endpoint=ingest_job_stream_endpoint, methods=["GET"]),
        Route("/api/v1/ingest/status", endpoint=ingest_status_endpoint, methods=["GET"]),
        Route("/api/v1/ingest/upload", endpoint=ingest_upload_endpoint, methods=["POST"]),
    ]
    if len(routes) < 10:
        raise RuntimeError("Incomplete route registration")
    return routes
