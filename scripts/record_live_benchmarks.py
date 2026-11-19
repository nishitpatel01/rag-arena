"""Live standing-engine golden benchmark recorder (Release 1.6.2).

Records measured golden benchmark runs across standing live engines (`managed_vertex`, `rag_engine`,
`level1_naive`, `level2_hybrid`, `level3_multimodal`, `level4_agentic`) into `data/recordings/<engine>/`
with strict `assert_redacted` verification, `0644` file permissions, `--resume` idempotency, bounded
`--max-spend-usd` budget enforcement, and optional BigQuery `query_runs` persistence.

Run: `PYTHONPATH=backend .venv/bin/python scripts/record_live_benchmarks.py [options]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable, Sequence
import uuid

from app.api.query_runs import build_query_run_row
from app.core.config import _PROJECT_ROOT, DEFAULT_LIVE_LEVELS, ArenaSettings, _load_dotenv_file, get_settings
from app.core.models import EngineResult, EvalOutcome
from app.core.models_catalog import ModelCatalog, RunModels, load_catalog
from app.core.telemetry import QueueStageEmitter, utc_now_iso
from app.engines import build_engine
from app.engines.base import BaseRAGEngine
from app.engines.replay_engine import recording_store_for
from app.evaluation.evaluator import run_evaluation
from app.ingestion.live_backends import LiveQueryRunWriter
from app.recording.recorder import build_recording, record_run
from app.recording.redaction import assert_redacted, redact_text, redaction_tokens

DEFAULT_BENCHMARK_PATH: Path = _PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
DEFAULT_SYNTHESIS_MODEL: str = "gemini-4-hydrogen"
DEFAULT_JUDGE_MODEL: str = "claude-opus-5-5"
DEFAULT_MAX_SPEND_USD: float = 2.50
_MAX_BENCHMARK_QUESTIONS: int = 50
_MAX_ATTEMPTS_PER_QUESTION: int = 2
_RETRY_DELAY_SECONDS: float = 5.0


def load_benchmark_questions(
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    *,
    only_ids: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Load golden benchmark questions deterministically with optional question_id filter."""
    payload = json.loads(benchmark_path.read_text(encoding="utf-8"))
    raw_questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError(f"Benchmark file {benchmark_path} contains no questions list")
    allow = {q.strip() for q in only_ids if q and q.strip()} if only_ids else None
    out: list[dict[str, str]] = []
    for item in raw_questions[:_MAX_BENCHMARK_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("question_id") or "").strip()
        qtext = str(item.get("question") or "").strip()
        if not qid or not qtext:
            continue
        if allow is not None and qid not in allow:
            continue
        out.append({"question_id": qid, "question": qtext})
    if not out:
        raise ValueError(f"No benchmark questions matched filter {sorted(allow or ())}")
    return out


async def record_live_cohort(
    *,
    settings: ArenaSettings,
    engines: Sequence[str] = DEFAULT_LIVE_LEVELS,
    synthesis_id: str = DEFAULT_SYNTHESIS_MODEL,
    judge_id: str = DEFAULT_JUDGE_MODEL,
    question_ids: Sequence[str] | None = None,
    benchmark_path: Path = DEFAULT_BENCHMARK_PATH,
    max_spend_usd: float = DEFAULT_MAX_SPEND_USD,
    resume: bool = True,
    write_query_runs: bool = True,
    catalog: ModelCatalog | None = None,
    engine_builder: Callable[[str, ArenaSettings, RunModels], BaseRAGEngine] = build_engine,
    evaluator: Callable[..., Awaitable[EvalOutcome]] = run_evaluation,
) -> dict[str, Any]:
    """Record golden runs across `engines` for `(synthesis_id, judge_id)` under `max_spend_usd`."""
    if settings.demo_mode != "record":
        raise ValueError(f"record_live_cohort requires DEMO_MODE=record, got {settings.demo_mode!r}")
    if not (0.0 < max_spend_usd <= 25.0):
        raise ValueError(f"max_spend_usd must be in (0, 25], got {max_spend_usd}")

    questions = load_benchmark_questions(benchmark_path, only_ids=question_ids)
    resolved_catalog = catalog or load_catalog(settings)
    run_models = resolved_catalog.select(synthesis_id, judge_id)
    store = recording_store_for(settings)
    tokens = redaction_tokens(settings)
    writer = LiveQueryRunWriter(settings) if write_query_runs else None
    timeout = float(settings.query_timeout_seconds)

    recorded = 0
    skipped_existing = 0
    total_spend_usd = 0.0
    saved_paths: list[str] = []

    for engine_id in engines:
        run_models.synthesis.check_level(engine_id)
        run_models.judge.check_level(engine_id)
        engine = engine_builder(engine_id, settings, run_models)
        pair_slug = f"{synthesis_id}__{judge_id}"

        for q in questions:
            qid = q["question_id"]
            qtext = q["question"]
            target_path = settings.recordings_path / engine_id / pair_slug / f"{qid}.json"
            if resume and target_path.is_file():
                skipped_existing += 1
                continue

            if total_spend_usd >= max_spend_usd:
                raise RuntimeError(
                    f"Recording spend ${total_spend_usd:.4f} reached max_spend_usd=${max_spend_usd:.2f} before {engine_id}/{qid}"
                )

            last_exc: Exception | None = None
            result: EngineResult | None = None
            outcome: EvalOutcome | None = None
            emitter: QueueStageEmitter | None = None
            run_id = ""
            started_at = ""

            for attempt in range(1, _MAX_ATTEMPTS_PER_QUESTION + 1):
                run_id = f"run_{uuid.uuid4().hex[:12]}"
                queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                emitter = QueueStageEmitter(queue, run_id=run_id, engine=engine_id)
                started_at = utc_now_iso()
                try:
                    result = await asyncio.wait_for(
                        engine.run_query(qtext, question_id=qid, solicitation_filter=None, emitter=emitter),
                        timeout=timeout,
                    )
                    outcome = await asyncio.wait_for(
                        evaluator(
                            qtext,
                            result,
                            question_id=qid,
                            settings=settings,
                            emitter=emitter,
                            models=run_models,
                            solicitation_filter=None,
                        ),
                        timeout=timeout,
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    safe_msg = redact_text(str(exc), tokens)
                    print(
                        f"WARN [{engine_id}] {pair_slug}/{qid} attempt {attempt}/{_MAX_ATTEMPTS_PER_QUESTION} failed: {type(exc).__name__}: {safe_msg}",
                        file=sys.stderr,
                    )
                    if attempt < _MAX_ATTEMPTS_PER_QUESTION:
                        await asyncio.sleep(_RETRY_DELAY_SECONDS)

            if last_exc is not None or result is None or outcome is None or emitter is None:
                raise RuntimeError(f"Failed recording {engine_id}/{pair_slug}/{qid}") from last_exc

            run_spend = float(result.cost_usd) + float(outcome.judge_cost_usd)
            total_spend_usd += run_spend
            if total_spend_usd > max_spend_usd:
                raise RuntimeError(
                    f"Recording spend ${total_spend_usd:.4f} exceeded max_spend_usd=${max_spend_usd:.2f} on {engine_id}/{qid}"
                )

            recording = build_recording(
                settings=settings,
                engine_id=engine_id,
                question_id=qid,
                query=qtext,
                run_id=run_id,
                events=emitter.events,
                result=result,
                evaluation=outcome,
                models=run_models,
            )
            path = record_run(store, recording)
            os.chmod(path, 0o644)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            assert_redacted(persisted, tokens)
            recorded += 1
            saved_paths.append(str(path.relative_to(_PROJECT_ROOT)) if path.is_relative_to(_PROJECT_ROOT) else path.name)

            if writer is not None:
                row = build_query_run_row(
                    settings=settings,
                    run_id=run_id,
                    question_id=qid,
                    query=qtext,
                    started_at=started_at,
                    emitter=emitter,
                    result=result,
                    outcome=outcome,
                    solicitation_filter=None,
                )
                writer.write_query_run(row)

            print(
                f"[{engine_id}] {pair_slug}/{qid}: latency={result.latency_ms}ms "
                f"G={outcome.metrics.groundedness:.2f} QA={outcome.metrics.qa_relevance:.2f} "
                f"CP={outcome.metrics.context_precision:.2f} engine=${result.cost_usd:.4f} "
                f"judge=${outcome.judge_cost_usd:.4f} cum=${total_spend_usd:.4f}",
                file=sys.stderr,
                flush=True,
            )

    return {
        "synthesis_model": synthesis_id,
        "judge_model": judge_id,
        "engines": list(engines),
        "recorded": recorded,
        "skipped_existing": skipped_existing,
        "total_spend_usd": round(total_spend_usd, 6),
        "saved_paths": saved_paths,
    }


def _merged_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    base = {**_load_dotenv_file(_PROJECT_ROOT / ".env"), **os.environ}
    if overrides:
        base.update(overrides)
    return base


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Record standing live golden benchmark runs into data/recordings/.")
    p.add_argument("--engines", default=",".join(DEFAULT_LIVE_LEVELS), help="Comma-separated engine ids.")
    p.add_argument("--synthesis-model", default=DEFAULT_SYNTHESIS_MODEL)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    p.add_argument("--questions", default="", help="Optional comma-separated question_ids.")
    p.add_argument("--max-spend-usd", type=float, default=DEFAULT_MAX_SPEND_USD)
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing recording files instead of resuming.")
    p.add_argument("--no-query-runs", action="store_true", help="Skip BigQuery query_runs persistence.")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    engines = tuple(e.strip() for e in args.engines.split(",") if e.strip())
    q_ids = tuple(q.strip() for q in args.questions.split(",") if q.strip()) or None
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "engines": list(engines),
                    "synthesis_model": args.synthesis_model,
                    "judge_model": args.judge_model,
                    "questions": list(q_ids or ()),
                    "max_spend_usd": args.max_spend_usd,
                }
            )
        )
        return 0

    raw_env = _merged_env()
    settings = get_settings({**raw_env, "DEMO_MODE": "record", "ENABLED_LEVELS": ",".join(dict.fromkeys([*DEFAULT_LIVE_LEVELS, *engines]))})
    tokens = redaction_tokens(settings)
    try:
        summary = asyncio.run(
            record_live_cohort(
                settings=settings,
                engines=engines,
                synthesis_id=args.synthesis_model.strip(),
                judge_id=args.judge_model.strip(),
                question_ids=q_ids,
                max_spend_usd=float(args.max_spend_usd),
                resume=not bool(args.overwrite),
                write_query_runs=not bool(args.no_query_runs),
            )
        )
        print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {redact_text(str(exc), tokens)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
