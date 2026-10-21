"""Recorder redaction (P-leak rule): recordings never contain the project id, bucket, credential paths, or $HOME."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from app.core.models import EvalMetrics, EvalOutcome
from app.recording.recorder import build_recording, record_run
from app.recording.redaction import RedactionError, assert_redacted, redact_value, redaction_tokens
from app.recording.store import RecordingStore
from fakes import live_settings

_HOME = str(Path.home())


def _settings():
    return live_settings(
        demo_mode="record",
        gcp_project_id="acme-secret-proj",
        gcs_corpus_bucket="acme-secret-proj-rag-arena-corpus",
        cloudsdk_config=f"{_HOME}/.config/gcloud-profile-x",
        google_application_credentials=f"{_HOME}/.config/gcloud-profile-x/adc-credentials.json",
    )


def test_redaction_tokens_cover_identity_values_longest_first() -> None:
    tokens = redaction_tokens(_settings())
    values = [v for v, _ in tokens]
    assert values == sorted(values, key=len, reverse=True)
    mapping = dict(tokens)
    assert mapping["acme-secret-proj"] == "${GCP_PROJECT_ID}"
    assert mapping["acme-secret-proj-rag-arena-corpus"] == "${GCS_CORPUS_BUCKET}"
    assert mapping[f"{_HOME}/.config/gcloud-profile-x"] == "${CLOUDSDK_CONFIG}"
    assert mapping[_HOME] == "${HOME}"
    assert mapping[f"{_HOME}/.config/gcloud-profile-x/adc-credentials.json"] == "${GOOGLE_APPLICATION_CREDENTIALS}"


def test_redact_value_is_recursive_over_keys_lists_and_substrings() -> None:
    settings = _settings()
    doc = {
        "sql": "SELECT * FROM `acme-secret-proj.rag_arena.document_chunks_v3`",
        "nested": [{"uri": "gs://acme-secret-proj-rag-arena-corpus/sample_docs/a.pdf"}, ("x", f"{_HOME}/tmp/file")],
        "acme-secret-proj": 3,
        "n": 1.5,
        "none": None,
    }
    out = redact_value(doc, redaction_tokens(settings))
    assert out["sql"] == "SELECT * FROM `${GCP_PROJECT_ID}.rag_arena.document_chunks_v3`"
    assert out["nested"][0]["uri"] == "gs://${GCS_CORPUS_BUCKET}/sample_docs/a.pdf"
    assert out["nested"][1] == ["x", "${HOME}/tmp/file"]
    assert out["${GCP_PROJECT_ID}"] == 3 and out["n"] == 1.5 and out["none"] is None
    assert_redacted(out, redaction_tokens(settings))
    with pytest.raises(RedactionError, match="GCP_PROJECT_ID"):
        assert_redacted({"leak": "acme-secret-proj"}, redaction_tokens(settings))


def test_recordings_written_in_record_mode_contain_no_identity_values() -> None:
    from app.core.models import EngineFlags, EngineResult, TokenUsage

    settings = _settings()
    result = EngineResult(
        engine_id="level1_naive", engine_label="Level 1", answer=f"see gs://acme-secret-proj-rag-arena-corpus in {_HOME}",
        citations=[], retrieved_chunks=[], agent_steps=[], latency_ms=10, cost_usd=0.001, stage_log=(),
        usage=TokenUsage(model="gemini-4-argon", embedding_model="gemini-embedding-2"), flags=EngineFlags(),
    )
    events = (
        {"event": "stage_completed", "stage": "retrieve", "t_rel_ms": 5,
         "payload": {"sql": "FROM `acme-secret-proj.rag_arena.document_chunks_v3`", "job_id": "bqjob_1"}},
    )
    outcome = EvalOutcome(metrics=EvalMetrics(groundedness=0.9, qa_relevance=0.8, context_precision=0.7), judge_model="gemini-4-argon", reference_answer_used=True, duration_ms=12)
    recording = build_recording(
        settings=settings, engine_id="level1_naive", question_id="q_redact", query="acme-secret-proj question?",
        run_id="run_x", events=events, result=result, evaluation=outcome,
    )
    assert recording.header["project_id"] == "${GCP_PROJECT_ID}"
    path = record_run(RecordingStore(Path(tempfile.mkdtemp(prefix="rag_arena_redact_"))), recording)
    raw = path.read_text(encoding="utf-8")
    for secret in ("acme-secret-proj", _HOME, "gcloud-profile-x"):
        assert secret not in raw
    doc = json.loads(raw)
    assert doc["events"][0]["payload"]["sql"] == "FROM `${GCP_PROJECT_ID}.rag_arena.document_chunks_v3`"
    assert doc["header"]["query"] == "${GCP_PROJECT_ID} question?"
