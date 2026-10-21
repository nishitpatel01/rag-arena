"""Model selection (owner request 2026-10-21): catalog, /models, request validation, provider adapters, pair-keyed
recordings, leaderboard grouping and query_runs provenance. All GCP calls are mocked."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

from app.core.config import ArenaSettings, get_settings
from app.core.models_catalog import (
    NOT_VERIFIED,
    PRICE_NOT_VERIFIED,
    ModelCatalog,
    ModelSelectionError,
    RunModels,
    load_catalog,
    location_env_suffix,
)
from app.core.pricing import PRICE_TABLE
from app.core.telemetry import RecordingStageEmitter
from app.engines import build_engine
from app.engines.level1_naive import Level1NaiveEngine
from app.engines.synthesis import AnthropicVertexSynthesizer, build_prompt
from app.evaluation.evaluator import run_evaluation
from app.evaluation.scoring import leaderboard_from_recordings
from app.ingestion import live_backends as lb
from app.main import create_app
from app.recording.recorder import build_recording, record_run
from app.recording.redaction import redact_value, redaction_tokens
from app.recording.store import RecordingStore
from fakes import (
    FABLE_403,
    FAKE_JUDGE_TEXT,
    FakeGenaiClient,
    anthropic_message,
    fake_clients,
    live_settings,
    run,
    write_verification_file,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_QID = "q_exact_clin_lookup"
_QUERY = "What is the base hourly rate for CLIN-0004-AB?"
_CLAUDE_ANSWER = "CLIN-0004-AB is billed at $210.00/hr in the base period [04_Pricing_Schedule_CLINs.pdf p.2]."
_FABLE_DOWN = {"gemini-4-argon": True, "gemini-3.8-flash": True, "claude-opus-5-5": True, "claude-fable-5-1": False}


def _events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


class _RoutedRawPredict:
    """``httpx.post`` stand-in: answers synthesis calls with prose and judge calls with the judge JSON."""

    def __init__(self, synthesis_model: str) -> None:
        self.synthesis_model = synthesis_model
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        text = _CLAUDE_ANSWER if f"/models/{self.synthesis_model}:rawPredict" in url else FAKE_JUDGE_TEXT
        return httpx.Response(200, json=anthropic_message(text, 2100, 140), request=httpx.Request("POST", url))


@contextmanager
def _clients(synthesis_model: str) -> Iterator[tuple[FakeGenaiClient, _RoutedRawPredict]]:
    raw = _RoutedRawPredict(synthesis_model)
    genai = FakeGenaiClient()
    with fake_clients(genai=genai), patch("app.evaluation.judges.httpx.post", raw):
        yield genai, raw


def _catalog(ok: dict[str, bool] | None = None, **overrides: Any) -> ModelCatalog:
    return load_catalog(live_settings(model_verification_file=str(write_verification_file(ok)), **overrides))


def _battle(settings: ArenaSettings, body: dict[str, Any], writer: Any = None) -> tuple[int, list[dict[str, Any]], Any]:
    app = create_app(settings=settings, query_run_writer=writer)
    with _clients(body.get("synthesis_model", "gemini-4-argon")) as (genai, raw), TestClient(app) as client:
        resp = client.post("/api/v1/battle/stream", json={"query": _QUERY, "question_id": _QID, **body})
    payload = _events(resp.text) if resp.status_code == 200 else resp.json()
    return resp.status_code, payload, (genai, raw)


# ---------------------------------------------------------------- catalog


def test_catalog_has_the_four_owner_models_with_prices_sources_and_both_roles() -> None:
    catalog = _catalog()
    assert [m.id for m in catalog.models] == ["gemini-4-argon", "gemini-3.8-flash", "claude-opus-5-5", "claude-fable-5-1"]
    assert [m.label for m in catalog.models] == ["Gemini 4 Argon", "Gemini 3.8 Flash", "Claude Opus 5.5", "Claude Fable 5.1"]
    assert [m.provider for m in catalog.models] == ["vertex_gemini", "vertex_gemini", "anthropic_vertex", "anthropic_vertex"]
    for spec in catalog.models:
        assert spec.roles == ("synthesis", "judge") and spec.location == "global" and spec.available
        assert spec.price_source.startswith("https://cloud.google.com/vertex-ai/") and spec.price_as_of
        assert spec.input_usd_per_1m == PRICE_TABLE[spec.id].input_usd_per_1m > 0
    assert (catalog.get("gemini-3.8-flash").input_usd_per_1m, catalog.get("gemini-3.8-flash").output_usd_per_1m) == (0.75, 3.75)
    assert (catalog.get("claude-fable-5-1").input_usd_per_1m, catalog.get("claude-fable-5-1").output_usd_per_1m) == (10.0, 50.0)


def test_failed_verification_marks_fable_unavailable_with_the_recorded_reason() -> None:
    fable = _catalog(_FABLE_DOWN).get("claude-fable-5-1")
    assert fable.available is False and fable.unavailable_reason == f"HTTP 403: {FABLE_403}"
    assert fable.verified_at == "2026-10-21T18:00:00Z"


def test_unverified_price_or_missing_verification_makes_a_model_unavailable() -> None:
    table = {k: v for k, v in PRICE_TABLE.items() if k != "gemini-3.8-flash"}
    with patch.dict("app.core.models_catalog.PRICE_TABLE", table, clear=True):
        flash = _catalog().get("gemini-3.8-flash")
    assert flash.available is False and flash.unavailable_reason == PRICE_NOT_VERIFIED
    missing = load_catalog(live_settings(model_verification_file=str(Path(tempfile.mkdtemp()) / "absent.json")))
    assert all(not m.available and m.unavailable_reason == NOT_VERIFIED for m in missing.models)


def test_model_location_override_comes_from_the_environment() -> None:
    assert location_env_suffix("gemini-3.8-flash") == "GEMINI_3_8_FLASH"
    env = {"MODEL_LOCATION_CLAUDE_OPUS_5_5": "us-east5", "MODEL_VERIFICATION_FILE": str(write_verification_file())}
    catalog = load_catalog(get_settings(env))
    assert catalog.get("claude-opus-5-5").location == "us-east5" and catalog.get("gemini-4-argon").location == "global"


def test_committed_verification_file_is_identity_free_and_well_formed() -> None:
    doc = json.loads((_PROJECT_ROOT / "data" / "model_verification.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in doc["models"]} == {"gemini-4-argon", "gemini-3.8-flash", "claude-opus-5-5", "claude-fable-5-1"}
    for entry in doc["models"]:
        assert set(entry) == {"id", "ok", "http_status", "message", "verified_at"}
    blob = json.dumps(doc)
    assert "projects/" not in blob or "${GCP_PROJECT" in blob


def test_select_rejects_unknown_unavailable_role_and_same_model() -> None:
    catalog = _catalog(_FABLE_DOWN)
    for args, needle in (
        (("gemini-9-ultra", "claude-opus-5-5"), "Unknown synthesis_model"),
        (("gemini-4-argon", "claude-fable-5-1"), "unavailable"),
        (("claude-opus-5-5", "claude-opus-5-5"), "must differ"),
    ):
        try:
            catalog.select(*args)
        except ModelSelectionError as exc:
            assert needle in str(exc), (args, str(exc))
        else:
            raise AssertionError(f"select{args} did not raise")
    synthesis_only = replace(catalog.get("gemini-3.8-flash"), roles=("synthesis",))
    restricted = ModelCatalog(models=(catalog.get("gemini-4-argon"), synthesis_only))
    with pytest.raises(ModelSelectionError):
        restricted.require("gemini-3.8-flash", "judge")
    with pytest.raises(ModelSelectionError):
        RunModels(synthesis=synthesis_only, judge=synthesis_only)


# ---------------------------------------------------------------- API: /models and 400s


def test_models_endpoint_shape_defaults_and_recorded_pairs() -> None:
    settings = ArenaSettings(demo_mode="replay", model_verification_file=str(write_verification_file(_FABLE_DOWN)))
    body = TestClient(create_app(settings=settings)).get("/api/v1/models").json()
    assert set(body) == {"models", "defaults", "demo_mode", "recorded_pairs"}
    assert body["defaults"] == {"synthesis_model": "gemini-4-argon", "judge_model": "claude-opus-5-5"}
    assert body["demo_mode"] == "replay" and len(body["models"]) == 4
    keys = {"id", "label", "provider", "location", "roles", "input_usd_per_1m", "output_usd_per_1m", "price_source",
            "price_as_of", "available", "unavailable_reason", "verified_at"}
    assert all(set(m) == keys for m in body["models"])
    fable = next(m for m in body["models"] if m["id"] == "claude-fable-5-1")
    assert fable["available"] is False and "data sharing" in fable["unavailable_reason"]
    argon_opus = next(
        p for p in body["recorded_pairs"]
        if (p["engine"], p["synthesis_model"], p["judge_model"]) == ("level1_naive", "gemini-4-argon", "claude-opus-5-5")
    )
    assert len(argon_opus["question_ids"]) == 8 and _QID in argon_opus["question_ids"]
    assert "dev_q01" not in argon_opus["question_ids"]  # dev fixtures only when ALLOW_DEV_FIXTURES=1


def test_battle_stream_returns_400_before_streaming_for_invalid_model_selection() -> None:
    settings = live_settings(model_verification_file=str(write_verification_file(_FABLE_DOWN)))
    for body, needle in (
        ({"synthesis_model": "gpt-x"}, "Unknown synthesis_model"),
        ({"judge_model": "claude-fable-5-1"}, "unavailable"),
        ({"synthesis_model": "claude-opus-5-5", "judge_model": "claude-opus-5-5"}, "must differ"),
        ({"judge_model": "gemini-4-argon"}, "must differ"),  # default synthesis is gemini-4-argon
        ({"synthesis_model": 7}, "non-empty model id"),
    ):
        status, payload, (genai, _raw) = _battle(settings, body)
        assert status == 400 and needle in payload["error"], (body, payload)
        assert genai.calls == []  # nothing ran
    synthesis_only = ModelCatalog(models=tuple(
        replace(m, roles=("synthesis",)) if m.id == "claude-opus-5-5" else m for m in load_catalog(settings).models
    ))
    with patch("app.api.routes.load_catalog", lambda s: synthesis_only):
        status, payload, _ = _battle(settings, {})
    assert status == 400 and "not allowed as judge" in payload["error"]


# ---------------------------------------------------------------- provider adapters


def test_claude_synthesizer_sends_no_temperature_and_maps_usage() -> None:
    settings = live_settings(synthesis_max_tokens=2048)
    spec = load_catalog(settings).get("claude-opus-5-5")
    synth = AnthropicVertexSynthesizer(spec, settings, token_provider=lambda: "fake-token", sleep=lambda s: None)
    prompt = build_prompt("[doc.pdf p.1]: CLIN-0004-AB $210.00/hr", _QUERY)
    reply = httpx.Response(200, json=anthropic_message(_CLAUDE_ANSWER, 1900, 120), request=httpx.Request("POST", "u"))
    with patch("app.core.anthropic_vertex.httpx.post", return_value=reply) as post:
        out = synth.generate(prompt)
    url, body = post.call_args.args[0], post.call_args.kwargs["json"]
    assert url.endswith("/locations/global/publishers/anthropic/models/claude-opus-5-5:rawPredict")
    assert "temperature" not in body and body["max_tokens"] == 2048 and body["anthropic_version"] == "vertex-2023-10-16"
    assert body["system"] == prompt.system and body["messages"] == [{"role": "user", "content": prompt.user}]
    assert (out.answer, out.prompt_tokens, out.output_tokens, out.thinking_tokens) == (_CLAUDE_ANSWER, 1900, 120, 0)
    assert out.response_id == "msg_vrtx_fake01" and out.stop_reason == "end_turn"
    no_usage = httpx.Response(200, json={"content": [{"type": "text", "text": "x"}]}, request=httpx.Request("POST", "u"))
    with patch("app.core.anthropic_vertex.httpx.post", return_value=no_usage):
        with pytest.raises(ValueError):
            synth.generate(prompt)


def test_level1_with_claude_synthesis_emits_selected_model_service_usage_and_cost() -> None:
    settings = live_settings()
    spec = load_catalog(settings).get("claude-opus-5-5")
    emitter = RecordingStageEmitter(run_id="run_x", engine="level1_naive")
    with _clients("claude-opus-5-5") as (genai, raw):
        result = run(Level1NaiveEngine(settings, synthesis_model=spec).run_query(
            _QUERY, question_id=_QID, solicitation_filter=None, emitter=emitter))
    started = next(e for e in emitter.events if e["event"] == "stage_started" and e["stage"] == "synthesize")
    assert started["service"] == "Vertex AI · Claude Opus 5.5"
    assert started["request"]["model"] == "claude-opus-5-5" and started["request"]["provider"] == "anthropic_vertex"
    assert "temperature" not in started["request"] and "temperature" not in raw.calls[0][1]["json"]
    assert [name for name, _ in genai.calls] == ["embed_content"]  # Gemini only embedded the query
    assert result.answer == _CLAUDE_ANSWER and result.usage.model == "claude-opus-5-5"
    assert (result.usage.prompt_tokens, result.usage.output_tokens, result.usage.thinking_tokens) == (2100, 140, 0)
    expected = 2100 / 1e6 * 4.0 + 140 / 1e6 * 20.0 + 14 / 1e6 * 0.20
    assert result.cost_usd == pytest.approx(expected)


def test_gemini_judge_grades_a_claude_answer_end_to_end() -> None:
    writer = _CapturingWriter()
    body = {"synthesis_model": "claude-opus-5-5", "judge_model": "gemini-3.8-flash", "engines": ["level1_naive"]}
    status, events, (genai, raw) = _battle(live_settings(), body, writer)
    assert status == 200 and events[-1]["engines_completed"] == ["level1_naive"], events
    started = events[0]
    assert (started["synthesis_model"], started["judge_model"]) == ("claude-opus-5-5", "gemini-3.8-flash")
    stages = {e["stage"]: e for e in events if e["event"] == "stage_started"}
    assert stages["synthesize"]["service"] == "Vertex AI · Claude Opus 5.5"
    assert stages["evaluate"]["service"] == "Vertex AI · Gemini 3.8 Flash (judge)"
    assert stages["evaluate"]["request"]["model"] == "gemini-3.8-flash" and stages["evaluate"]["request"]["provider"] == "vertex_gemini"
    assert stages["evaluate"]["request"]["temperature"] == 0.0
    answer = next(e for e in events if e["event"] == "answer_complete")
    evaluation = next(e for e in events if e["event"] == "eval_complete")
    assert answer["usage"]["model"] == "claude-opus-5-5" and answer["answer"] == _CLAUDE_ANSWER
    assert evaluation["judge_model"] == "gemini-3.8-flash" and evaluation["judge_provider"] == "vertex_gemini"
    assert evaluation["judge_cost_usd"] == pytest.approx(900 / 1e6 * 0.75 + 24 / 1e6 * 3.75)
    judge_call = next(kw for name, kw in genai.calls if name == "generate_content")
    assert judge_call["model"] == "gemini-3.8-flash" and judge_call["config"]["response_mime_type"] == "application/json"
    assert len(raw.calls) == 1 and "/models/claude-opus-5-5:rawPredict" in raw.calls[0][0]
    row = writer.rows[0]
    assert (row["synthesis_model"], row["judge_model"]) == ("claude-opus-5-5", "gemini-3.8-flash")


class _CapturingWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_query_run(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


def test_query_runs_row_carries_both_models_for_the_default_pair() -> None:
    writer = _CapturingWriter()
    status, events, _ = _battle(live_settings(), {"engines": ["level1_naive"]}, writer)
    assert status == 200 and len(writer.rows) == 1
    assert (writer.rows[0]["synthesis_model"], writer.rows[0]["judge_model"]) == ("gemini-4-argon", "claude-opus-5-5")
    synth = next(e for e in events if e["event"] == "stage_started" and e["stage"] == "synthesize")
    assert synth["service"] == "Vertex AI · Gemini 4 Argon" and synth["request"]["thinking_level"] == "HIGH"


def test_query_runs_schema_gains_model_columns_in_place() -> None:
    names = [name for name, _, _ in lb._QUERY_RUN_SCHEMA]
    assert names[-2:] == ["synthesis_model", "judge_model"]

    class _BQ:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str, job_config: Any = None) -> Any:
            self.queries.append(sql)
            return type("J", (), {"result": lambda self, timeout=None: []})()

    bq = _BQ()
    lb.BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=bq).ensure_schema()
    sql = "\n".join(bq.queries)
    assert "ADD COLUMN IF NOT EXISTS synthesis_model STRING" in sql and "ADD COLUMN IF NOT EXISTS judge_model STRING" in sql
    assert "DROP" not in sql.upper() and "CREATE OR REPLACE" not in sql.upper()


def test_legacy_levels_reject_a_non_default_synthesis_model() -> None:
    settings = live_settings(enabled_levels=("level1_naive", "level2_hybrid"))
    models = load_catalog(settings).select("gemini-3.8-flash", "claude-opus-5-5")
    with pytest.raises(ValueError):
        build_engine("level2_hybrid", settings, models)


# ---------------------------------------------------------------- recordings, replay, leaderboard


def _record(root: Path, synthesis: str, judge: str, qid: str = _QID) -> Path:
    settings = live_settings(demo_mode="record", recordings_dir=str(root))
    models = load_catalog(settings).select(synthesis, judge)
    emitter = RecordingStageEmitter(run_id="run_rec", engine="level1_naive")

    async def scenario():
        result = await Level1NaiveEngine(settings, synthesis_model=models.synthesis).run_query(
            _QUERY, question_id=qid, solicitation_filter=None, emitter=emitter)
        outcome = await run_evaluation(_QUERY, result, question_id=qid, settings=settings, emitter=emitter, models=models)
        return result, outcome

    with _clients(synthesis):
        result, outcome = run(scenario())
    recording = build_recording(settings=settings, engine_id="level1_naive", question_id=qid, query=_QUERY,
                                run_id="run_rec", events=emitter.events, result=result, evaluation=outcome, models=models)
    return record_run(RecordingStore(root), recording)


def test_recordings_are_written_under_the_pair_directory_with_pair_header() -> None:
    root = Path(tempfile.mkdtemp(prefix="rag_arena_pairs_"))
    path = _record(root, "gemini-3.8-flash", "claude-opus-5-5")
    assert path == root / "level1_naive" / "gemini-3.8-flash__claude-opus-5-5" / f"{_QID}.json"
    header = json.loads(path.read_text(encoding="utf-8"))["header"]
    assert (header["synthesis_model"], header["judge_model"]) == ("gemini-3.8-flash", "claude-opus-5-5")
    assert header["model_ids"]["generation"] == "gemini-3.8-flash" and header["model_ids"]["judge"] == "claude-opus-5-5"


def test_record_mode_battle_writes_the_selected_pair() -> None:
    root = Path(tempfile.mkdtemp(prefix="rag_arena_recmode_"))
    settings = live_settings(demo_mode="record", recordings_dir=str(root))
    body = {"synthesis_model": "claude-opus-5-5", "judge_model": "gemini-4-argon", "engines": ["level1_naive"]}
    status, events, _ = _battle(settings, body, _CapturingWriter())
    saved = next(e for e in events if e["event"] == "recording_saved")
    assert saved["path"] == str(root / "level1_naive" / "claude-opus-5-5__gemini-4-argon" / f"{_QID}.json")


def test_replay_plays_only_the_requested_pair_and_names_it_when_missing() -> None:
    root = Path(tempfile.mkdtemp(prefix="rag_arena_replay_"))
    _record(root, "gemini-3.8-flash", "claude-opus-5-5")
    settings = ArenaSettings(demo_mode="replay", recordings_dir=str(root), replay_speed=50.0,
                             model_verification_file=str(write_verification_file()))
    client = TestClient(create_app(settings=settings))
    ok = _events(client.post("/api/v1/battle/stream", json={
        "query": _QUERY, "question_id": _QID, "synthesis_model": "gemini-3.8-flash", "judge_model": "claude-opus-5-5"}).text)
    assert ok[0]["synthesis_model"] == "gemini-3.8-flash" and ok[-1]["engines_completed"] == ["level1_naive"]
    assert next(e for e in ok if e["event"] == "eval_complete")["judge_model"] == "claude-opus-5-5"
    missing = _events(client.post("/api/v1/battle/stream", json={"query": _QUERY, "question_id": _QID}).text)
    err = next(e for e in missing if e["event"] == "engine_error")
    assert err["error_type"] == "ReplayUnavailableError"
    assert err["error"] == f"No recording for question {_QID} with synthesis=gemini-4-argon, judge=claude-opus-5-5 in replay mode"
    pairs = client.get("/api/v1/models").json()["recorded_pairs"]
    assert pairs == [{"engine": "level1_naive", "synthesis_model": "gemini-3.8-flash", "judge_model": "claude-opus-5-5",
                      "question_ids": [_QID]}]


def test_leaderboard_groups_by_engine_and_model_pair_without_averaging_across_pairs() -> None:
    root = Path(tempfile.mkdtemp(prefix="rag_arena_lb_"))
    _record(root, "gemini-4-argon", "claude-opus-5-5")
    _record(root, "claude-opus-5-5", "gemini-3.8-flash")
    board = leaderboard_from_recordings(RecordingStore(root))
    assert board["grouped_by"] == ["engine", "synthesis_model", "judge_model"]
    keys = sorted((r["engine"], r["synthesis_model"], r["judge_model"], r["runs"]) for r in board["engines"])
    assert keys == [("level1_naive", "claude-opus-5-5", "gemini-3.8-flash", 1), ("level1_naive", "gemini-4-argon", "claude-opus-5-5", 1)]


def test_redaction_removes_project_numbers_from_resource_names() -> None:
    tokens = redaction_tokens(live_settings(gcp_project_id="acme-proj-1"), home="/home/someone")
    doc = redact_value({"e": "projects/123456789012/locations/global and acme-proj-1"}, tokens)
    assert doc == {"e": "projects/${GCP_PROJECT_NUMBER}/locations/global and ${GCP_PROJECT_ID}"}


def test_verify_script_records_redacted_failures_without_raising() -> None:
    spec_obj = importlib.util.spec_from_file_location("verify_models", _PROJECT_ROOT / "scripts" / "verify_models.py")
    module = importlib.util.module_from_spec(spec_obj)
    spec_obj.loader.exec_module(module)
    settings = live_settings(gcp_project_id="acme-proj-1")
    fable = load_catalog(settings).get("claude-fable-5-1")
    body = json.dumps({"error": {"code": 403, "message": "Denied for projects/acme-proj-1 (projects/998877665544)"}})
    reply = httpx.Response(403, text=body, request=httpx.Request("POST", "u"))
    with patch.object(module, "get_access_token", lambda s: "tok"), patch.object(module.httpx, "post", return_value=reply) as post:
        entry = module._probe(settings, fable)
    assert post.call_args.kwargs["timeout"].read == 60 and "temperature" not in post.call_args.kwargs["json"]
    assert entry["ok"] is False and entry["http_status"] == 403 and set(entry) == {"id", "ok", "http_status", "message", "verified_at"}
    redacted = redact_value(entry, redaction_tokens(settings))
    assert "acme-proj-1" not in json.dumps(redacted) and "998877665544" not in json.dumps(redacted)
    time.sleep(0)  # keep the import of time used for symmetry with other tests
