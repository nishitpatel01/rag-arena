"""Leaderboard aggregation shared by both sources (spec §7.4): recordings (replay) and ``query_runs`` (live).

Both sources reduce to ``RunSample`` values and go through ONE aggregator, so the response shape is identical:
``{source, generated_at, filters, notes, stats, groups: [{synthesis_model, judge_model, pair_known, note, rows}]}``.
Rows are per engine inside a (synthesis, judge) pair and never averaged across pairs or judges. Each run's
Battle Score comes from ``scoring.battle_score`` (the single formula), then is averaged per row. Runs without a
known pair go to an ``unknown`` group with a note — never merged into a real pair.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from app.core.models import EvalMetrics
from app.core.telemetry import utc_now_iso
from app.evaluation.scoring import battle_score
from app.recording.codec import eval_outcome_from_dict
from app.recording.store import RecordingInvalid, RecordingStore

SOURCES = ("recordings", "query_runs")
SCOPES = ("golden", "uploads")
UNKNOWN_MODEL = "unknown"
UNKNOWN_PAIR_NOTE = (
    "These runs have no recorded synthesis/judge model (written before the columns existed); "
    "they are kept apart because scores from different judges are not comparable."
)
RECORDINGS_UPLOADS_NOTE = (
    "Recordings capture the fixed golden benchmark corpus only; switch source to Live runs to view upload runs."
)
MAX_GIT_SHAS = 20
MAX_SAMPLES = 10_000
_ENGINE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_QUESTION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


@dataclass(frozen=True)
class LeaderboardFilters:
    """Optional exact-match filters (validated; passed to BigQuery only as query parameters)."""

    question_id: str | None = None
    engine: str | None = None
    scope: str = "golden"

    def to_dict(self) -> dict[str, str | None]:
        return {"question_id": self.question_id, "engine": self.engine, "scope": self.scope}


def parse_filters(
    question_id: str | None, engine: str | None, scope: str | None = None
) -> LeaderboardFilters:
    """Empty → None (or ``'golden'`` for ``scope``); anything outside the alphabets raises ValueError (HTTP 400)."""
    qid = (question_id or "").strip() or None
    eng = (engine or "").strip() or None
    scp = (scope or "").strip() or "golden"
    if qid is not None and not _QUESTION_RE.match(qid):
        raise ValueError(f"Invalid question_id {qid[:140]!r}: allowed letters, digits, '_', '-', '.' (max 128)")
    if eng is not None and not _ENGINE_RE.match(eng):
        raise ValueError(f"Invalid engine {eng[:80]!r}: allowed lowercase letters, digits, '_' (max 64)")
    if scp not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}, got {scp[:40]!r}")
    return LeaderboardFilters(question_id=qid, engine=eng, scope=scp)


@dataclass(frozen=True)
class RunSample:
    """One measured run from either source; ``metrics is None`` marks a run that produced no judge scores."""

    engine: str
    synthesis_model: str | None
    judge_model: str | None
    question_id: str | None
    run_at: str | None
    git_sha: str | None
    demo_mode: str | None
    latency_ms: int | None
    cost_usd: float | None
    metrics: EvalMetrics | None

    @property
    def scored(self) -> bool:
        return self.metrics is not None and self.latency_ms is not None and self.cost_usd is not None


def _pair_of(sample: RunSample) -> tuple[str, str, bool]:
    synthesis, judge = sample.synthesis_model, sample.judge_model
    if synthesis and judge:
        return synthesis, judge, True
    return UNKNOWN_MODEL, UNKNOWN_MODEL, False


def _row_provenance(source: str, samples: list[RunSample], table: str | None) -> dict[str, Any]:
    shas = sorted({s.git_sha for s in samples if s.git_sha})[:MAX_GIT_SHAS]
    if source == "recordings":
        stamps = sorted(s.run_at for s in samples if s.run_at)
        recorded_at = {"first": stamps[0], "last": stamps[-1]} if stamps else None
        return {"kind": "recording", "files": len(samples), "recorded_at": recorded_at, "git_shas": shas}
    modes: dict[str, int] = {}
    for s in samples:
        key = s.demo_mode or "unknown"
        modes[key] = modes.get(key, 0) + 1
    return {"kind": "query_runs", "table": table, "demo_modes": dict(sorted(modes.items())), "git_shas": shas}


def _avg(values: list[float], digits: int) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _aggregate_row(engine: str, samples: list[RunSample], *, source: str, label: str, table: str | None) -> dict[str, Any]:
    scored = [s for s in samples if s.scored]
    scores = [battle_score(s.metrics, int(s.latency_ms), float(s.cost_usd)) for s in scored]  # type: ignore[arg-type]
    stamps = sorted(s.run_at for s in samples if s.run_at)
    return {
        "engine": engine,
        "level_label": label,
        "runs": len(scored),
        "avg_battle_score": _avg([b.battle_score for b in scores], 1),
        "avg_efficiency": _avg([b.efficiency for b in scores], 3),
        "avg_groundedness": _avg([b.groundedness for b in scores], 3),
        "avg_qa_relevance": _avg([b.qa_relevance for b in scores], 3),
        "avg_context_precision": _avg([b.context_precision for b in scores], 3),
        "avg_latency_ms": _avg([float(b.latency_ms) for b in scores], 1),
        "avg_cost_usd": _avg([b.cost_usd for b in scores], 6),
        "questions_covered": len({s.question_id for s in scored if s.question_id}),
        "failures": len(samples) - len(scored),
        "first_run_at": stamps[0] if stamps else None,
        "last_run_at": stamps[-1] if stamps else None,
        "provenance": _row_provenance(source, samples, table),
    }


def _row_sort_key(row: dict[str, Any]) -> tuple[bool, float, str]:
    score = row["avg_battle_score"]
    return (score is None, -(score or 0.0), row["engine"])


def aggregate_leaderboard(
    samples: Iterable[RunSample],
    *,
    source: str,
    filters: LeaderboardFilters,
    level_labels: Mapping[str, str],
    table: str | None = None,
    notes: Iterable[str] = (),
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Group by (synthesis, judge) then engine; rows sorted by avg Battle Score desc (unscored rows last)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    buckets: dict[tuple[str, str, bool], dict[str, list[RunSample]]] = {}
    for idx, sample in enumerate(samples):
        if idx >= MAX_SAMPLES:
            raise ValueError(f"leaderboard refuses more than {MAX_SAMPLES} runs in one aggregation")
        buckets.setdefault(_pair_of(sample), {}).setdefault(sample.engine, []).append(sample)
    groups = []
    for (synthesis, judge, known) in sorted(buckets, key=lambda k: (not k[2], k[0], k[1])):
        rows = [
            _aggregate_row(engine, runs, source=source, label=level_labels.get(engine, engine), table=table)
            for engine, runs in buckets[(synthesis, judge, known)].items()
        ]
        rows.sort(key=_row_sort_key)
        groups.append({"synthesis_model": synthesis, "judge_model": judge, "pair_known": known,
                       "note": None if known else UNKNOWN_PAIR_NOTE, "rows": rows})
    return {
        "source": source,
        "generated_at": utc_now_iso(),
        "filters": filters.to_dict(),
        "notes": list(notes),
        "stats": dict(stats or {}),
        "groups": groups,
    }


def _recording_sample(rec: Any) -> RunSample:
    try:
        synthesis, judge = rec.pair
    except RecordingInvalid:
        synthesis, judge = None, None
    metrics, latency, cost = None, None, None
    if rec.evaluation is not None:
        outcome = eval_outcome_from_dict(rec.evaluation)
        metrics = outcome.metrics
        latency = int(rec.result.get("latency_ms", 0))
        cost = float(rec.result.get("cost_usd", 0.0)) + outcome.judge_cost_usd  # run cost includes the judge
    return RunSample(
        engine=rec.engine, synthesis_model=synthesis, judge_model=judge, question_id=rec.question_id,
        run_at=rec.header.get("recorded_at"), git_sha=rec.header.get("git_sha"), demo_mode=rec.header.get("demo_mode"),
        latency_ms=latency, cost_usd=cost, metrics=metrics,
    )


def leaderboard_from_recordings(
    store: RecordingStore,
    filters: LeaderboardFilters | None = None,
    level_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Aggregate recorded live runs. Synthetic dev fixtures are never aggregated, in any mode."""
    if not isinstance(store, RecordingStore):
        raise TypeError("store must be a RecordingStore")
    flt = filters or LeaderboardFilters()
    if flt.scope == "uploads":
        stats = {"rows_read": 0, "rows_matching": 0, "max_rows": None, "bytes_processed": None}
        return aggregate_leaderboard(
            [], source="recordings", filters=flt, level_labels=level_labels or {},
            notes=[RECORDINGS_UPLOADS_NOTE], stats=stats,
        )
    samples = []
    for rec in store.list_recordings(include_dev=store.allow_dev_fixtures):
        if rec.is_dev_fixture:
            continue
        if (flt.engine and rec.engine != flt.engine) or (flt.question_id and rec.question_id != flt.question_id):
            continue
        samples.append(_recording_sample(rec))
    stats = {"rows_read": len(samples), "rows_matching": len(samples), "max_rows": None, "bytes_processed": None}
    notes = ["Recordings are live runs captured with DEMO_MODE=record; synthetic dev fixtures are never aggregated."]
    return aggregate_leaderboard(samples, source="recordings", filters=flt, level_labels=level_labels or {},
                                 notes=notes, stats=stats)


__all__ = [
    "LeaderboardFilters",
    "RECORDINGS_UPLOADS_NOTE",
    "RunSample",
    "SCOPES",
    "SOURCES",
    "UNKNOWN_MODEL",
    "UNKNOWN_PAIR_NOTE",
    "aggregate_leaderboard",
    "leaderboard_from_recordings",
    "parse_filters",
]
