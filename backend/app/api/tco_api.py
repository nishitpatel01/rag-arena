"""Starlette REST endpoints for the Enterprise TCO & Architecture Wizard (Slice #1, v1.6.1).

Imperative Shell around ``app.core.tco``:
- ``GET /api/v1/tco/defaults``: bounds, enum options, curated enterprise scenario presets,
  published GCP pricing basis, and initial TCO + workload-fit estimate joined with real recordings.
- ``POST /api/v1/tco/estimate``: strict bounded input validation (HTTP 400 on out-of-range,
  unknown fields, or malformed JSON) + deterministic TCO estimate joined with real recorded cohorts.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import ArenaSettings, get_settings
from app.core.tco import (
    COMPLIANCE_POSTURE_OPTIONS,
    CORPUS_PAGES_BOUNDS,
    DATA_GRAVITY_OPTIONS,
    DEFAULT_WORKLOAD,
    DOC_COMPLEXITY_OPTIONS,
    ENTERPRISE_SCENARIO_PRESETS,
    LATENCY_SLA_OPTIONS,
    MONTHLY_QUERIES_BOUNDS,
    TCO_SYNTHESIS_MODELS,
    RecordedCohortSnapshot,
    TcoValidationError,
    estimate_enterprise_tco,
    parse_tco_workload,
    pricing_basis_metadata,
)
from app.engines import engine_labels
from app.engines.replay_engine import recording_store_for
from app.evaluation.leaderboard import leaderboard_from_recordings

_MAX_TCO_BODY_BYTES = 16_384
_MAX_CACHE_ENTRIES = 8
_CACHE_LOCK = threading.Lock()
_COHORT_CACHE: dict[
    tuple[str, bool, int],
    tuple[dict[tuple[str, str], RecordedCohortSnapshot], dict[str, RecordedCohortSnapshot]],
] = {}


def clear_tco_cohort_cache() -> None:
    """Clear cached recorded-cohort snapshots (used by tests or after recording updates)."""
    with _CACHE_LOCK:
        _COHORT_CACHE.clear()


def _settings_of(request: Request) -> ArenaSettings:
    bound = getattr(request.app.state, "settings", None)
    return bound if isinstance(bound, ArenaSettings) else get_settings()


def _fallback_priority(snapshot: RecordedCohortSnapshot) -> tuple[int, int]:
    model_rank = (
        3
        if snapshot.synthesis_model == "gemini-4-neon"
        else (2 if snapshot.synthesis_model == "gemini-4-argon" else (1 if snapshot.synthesis_model == "gemini-3.8-flash" else 0))
    )
    return (model_rank, snapshot.runs)


def _extract_cohort_maps(
    leaderboard_payload: dict[str, Any],
) -> tuple[dict[tuple[str, str], RecordedCohortSnapshot], dict[str, RecordedCohortSnapshot]]:
    exact: dict[tuple[str, str], RecordedCohortSnapshot] = {}
    fallback: dict[str, RecordedCohortSnapshot] = {}
    groups = leaderboard_payload.get("groups")
    if not isinstance(groups, list):
        return exact, fallback

    for group in groups:
        if not isinstance(group, dict) or not group.get("pair_known", True):
            continue
        synth = str(group.get("synthesis_model") or "").strip()
        judge = str(group.get("judge_model") or "").strip()
        rows = group.get("rows")
        if not synth or not judge or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            engine = str(row.get("engine") or "").strip()
            runs = int(row.get("runs") or 0)
            if not engine or runs <= 0:
                continue
            snap = RecordedCohortSnapshot(
                synthesis_model=synth,
                judge_model=judge,
                runs=runs,
                questions_covered=int(row.get("questions_covered") or 0),
                avg_qa_relevance=row.get("avg_qa_relevance"),
                avg_groundedness=row.get("avg_groundedness"),
                avg_context_precision=row.get("avg_context_precision"),
                avg_battle_score=row.get("avg_battle_score"),
                avg_latency_ms=row.get("avg_latency_ms"),
                avg_cost_usd=row.get("avg_cost_usd"),
            )
            prev = exact.get((engine, synth))
            if prev is None or (judge == "claude-opus-5-5" and prev.judge_model != "claude-opus-5-5") or snap.runs > prev.runs:
                exact[(engine, synth)] = snap
            prev_fb = fallback.get(engine)
            if prev_fb is None or _fallback_priority(snap) > _fallback_priority(prev_fb):
                fallback[engine] = snap
    return exact, fallback


def _load_cohort_maps_sync(
    settings: ArenaSettings,
) -> tuple[dict[tuple[str, str], RecordedCohortSnapshot], dict[str, RecordedCohortSnapshot]]:
    store = recording_store_for(settings)
    try:
        mtime_ns = store.root.stat().st_mtime_ns if store.root.exists() else 0
    except OSError:
        mtime_ns = 0
    cache_key = (str(store.root), bool(store.allow_dev_fixtures), mtime_ns)
    with _CACHE_LOCK:
        cached = _COHORT_CACHE.get(cache_key)
        if cached is not None:
            return cached

    payload = leaderboard_from_recordings(store, level_labels=engine_labels())
    extracted = _extract_cohort_maps(payload)
    with _CACHE_LOCK:
        if len(_COHORT_CACHE) >= _MAX_CACHE_ENTRIES:
            _COHORT_CACHE.clear()
        _COHORT_CACHE[cache_key] = extracted
    return extracted


async def tco_defaults_endpoint(request: Request) -> JSONResponse:
    """GET /api/v1/tco/defaults: schema bounds, enums, scenario presets, pricing basis, and default estimate."""
    settings = _settings_of(request)
    labels = engine_labels()
    exact_cohorts, fallback_cohorts = await run_in_threadpool(_load_cohort_maps_sync, settings)
    estimate = estimate_enterprise_tco(
        DEFAULT_WORKLOAD,
        level_labels=labels,
        recorded_cohorts=exact_cohorts,
        fallback_cohorts=fallback_cohorts,
    )
    return JSONResponse(
        {
            "defaults": DEFAULT_WORKLOAD.to_dict(),
            "bounds": {
                "monthly_queries": list(MONTHLY_QUERIES_BOUNDS),
                "corpus_pages": list(CORPUS_PAGES_BOUNDS),
            },
            "enums": {
                "data_gravity": list(DATA_GRAVITY_OPTIONS),
                "latency_sla": list(LATENCY_SLA_OPTIONS),
                "compliance_posture": list(COMPLIANCE_POSTURE_OPTIONS),
                "doc_complexity": list(DOC_COMPLEXITY_OPTIONS),
                "synthesis_model": list(TCO_SYNTHESIS_MODELS),
            },
            "presets": [p.to_dict() for p in ENTERPRISE_SCENARIO_PRESETS],
            "pricing_metadata": pricing_basis_metadata(),
            "enabled_levels": list(settings.enabled_levels),
            "estimate": estimate.to_dict(),
        }
    )


async def tco_estimate_endpoint(request: Request) -> JSONResponse:
    """POST /api/v1/tco/estimate: validate bounded workload inputs and return TCO + architecture fit."""
    raw_bytes = await request.body()
    if len(raw_bytes) > _MAX_TCO_BODY_BYTES:
        return JSONResponse(
            {"error": f"Request body exceeds {_MAX_TCO_BODY_BYTES} byte bound"},
            status_code=400,
        )
    if not raw_bytes.strip():
        parsed_body: Any = {}
    else:
        try:
            parsed_body = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": f"Invalid JSON request body: {exc}"}, status_code=400)

    try:
        workload = parse_tco_workload(parsed_body)
    except TcoValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    settings = _settings_of(request)
    labels = engine_labels()
    exact_cohorts, fallback_cohorts = await run_in_threadpool(_load_cohort_maps_sync, settings)
    estimate = estimate_enterprise_tco(
        workload,
        level_labels=labels,
        recorded_cohorts=exact_cohorts,
        fallback_cohorts=fallback_cohorts,
    )
    return JSONResponse(estimate.to_dict())


def get_tco_routes() -> list[Route]:
    """Return Starlette routes for ``/api/v1/tco/*`` (registered by ``create_app`` / ``get_api_routes``)."""
    return [
        Route("/api/v1/tco/defaults", endpoint=tco_defaults_endpoint, methods=["GET"]),
        Route("/api/v1/tco/estimate", endpoint=tco_estimate_endpoint, methods=["POST"]),
    ]


__all__ = [
    "clear_tco_cohort_cache",
    "get_tco_routes",
    "tco_defaults_endpoint",
    "tco_estimate_endpoint",
]
