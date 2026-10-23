"""Tests for v3 runtime configuration gates (spec P4/P6, §6.4)."""

from __future__ import annotations

import pytest

from app.core.config import (
    ALL_LEVEL_IDS,
    ArenaSettings,
    get_settings,
)


def _env(**overrides: str) -> dict[str, str]:
    base = {"DEMO_MODE": "replay", "GCP_PROJECT_ID": "test-project"}
    base.update(overrides)
    return base


def test_defaults_enable_signed_off_levels_in_replay_mode() -> None:
    settings = get_settings(_env())
    assert settings.enabled_levels == ("level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic", "managed_vertex")
    assert settings.demo_mode == "replay"
    assert settings.replay_speed == 1.0
    assert settings.max_sse_frames == 600
    assert settings.ingest_job_timeout_seconds >= 60
    assert settings.allow_dev_fixtures is False
    assert settings.is_live is False
    assert ALL_LEVEL_IDS == (
        "level1_naive",
        "level2_hybrid",
        "level3_multimodal",
        "level4_agentic",
        "managed_vertex",
    )


def test_enabled_levels_parses_comma_list_and_rejects_unknown_or_parked() -> None:
    settings = get_settings(_env(ENABLED_LEVELS=" level1_naive, level2_hybrid "))
    assert settings.enabled_levels == ("level1_naive", "level2_hybrid")
    with pytest.raises(ValueError, match="ENABLED_LEVELS"):
        get_settings(_env(ENABLED_LEVELS="level1_naive,vertex_vector_v2"))
    with pytest.raises(ValueError, match="ENABLED_LEVELS"):
        get_settings(_env(ENABLED_LEVELS=""))


def test_demo_mode_accepts_live_record_replay_only() -> None:
    assert get_settings(_env(DEMO_MODE="record")).demo_mode == "record"
    assert get_settings(_env(DEMO_MODE="record")).is_live is True
    assert get_settings(_env(DEMO_MODE="live")).is_live is True
    with pytest.raises(ValueError, match="demo_mode"):
        get_settings(_env(DEMO_MODE="demo"))


def test_replay_speed_frames_and_timeout_bounds_are_validated() -> None:
    assert get_settings(_env(REPLAY_SPEED="4")).replay_speed == 4.0
    with pytest.raises(ValueError, match="REPLAY_SPEED"):
        get_settings(_env(REPLAY_SPEED="0"))
    with pytest.raises(ValueError, match="REPLAY_SPEED"):
        get_settings(_env(REPLAY_SPEED="fast"))
    with pytest.raises(ValueError, match="MAX_SSE_FRAMES"):
        get_settings(_env(MAX_SSE_FRAMES="5"))
    with pytest.raises(ValueError, match="INGEST_JOB_TIMEOUT_SECONDS"):
        get_settings(_env(INGEST_JOB_TIMEOUT_SECONDS="1"))
    assert get_settings(_env(ALLOW_DEV_FIXTURES="1")).allow_dev_fixtures is True
    assert isinstance(ArenaSettings(demo_mode="replay"), ArenaSettings)


def test_gcp_identity_comes_only_from_env_and_is_required_in_live_modes() -> None:
    """No project/bucket defaults in code; live|record fail at startup without GCP_PROJECT_ID; bucket derives."""
    replay = get_settings({"DEMO_MODE": "replay"})
    assert replay.gcp_project_id == "" and replay.gcs_corpus_bucket == ""
    assert replay.vertex_genai_location == "global"
    try:
        get_settings({"DEMO_MODE": "live"})
    except ValueError as exc:
        assert "GCP_PROJECT_ID" in str(exc)
    else:
        raise AssertionError("live mode without GCP_PROJECT_ID must fail at startup")
    live = get_settings({"DEMO_MODE": "live", "GCP_PROJECT_ID": "example-project", "VERTEX_GENAI_LOCATION": "us-east4"})
    assert live.gcs_corpus_bucket == "example-project-rag-arena-corpus"
    assert live.vertex_genai_location == "us-east4"
    explicit = get_settings({"DEMO_MODE": "live", "GCP_PROJECT_ID": "example-project", "GCS_CORPUS_BUCKET": "my-bucket"})
    assert explicit.gcs_corpus_bucket == "my-bucket"
    try:
        get_settings({"DEMO_MODE": "replay", "GCP_PROJECT_ID": "Bad Project!"})
    except ValueError as exc:
        assert "GCP_PROJECT_ID" in str(exc)
    else:
        raise AssertionError("invalid project id characters must be rejected")


# ---------------------------------------------------------------- LLM-as-a-judge settings (owner: judge != synthesis model)


def test_judge_defaults_to_claude_opus_on_the_vertex_global_endpoint() -> None:
    settings = get_settings(_env())
    assert settings.judge_provider == "anthropic_vertex"
    assert settings.judge_model == "claude-opus-5-5"
    assert settings.judge_location == "global"
    assert settings.judge_max_tokens == 1024 and settings.judge_timeout_seconds == 60


def test_judge_settings_parse_from_env() -> None:
    settings = get_settings(
        _env(JUDGE_PROVIDER="gemini", JUDGE_MODEL="gemini-4-argon-judge", JUDGE_LOCATION="us-east5",
             JUDGE_MAX_TOKENS="2048", JUDGE_TIMEOUT_SECONDS="30")
    )
    assert (settings.judge_provider, settings.judge_model, settings.judge_location) == ("gemini", "gemini-4-argon-judge", "us-east5")
    assert settings.judge_max_tokens == 2048 and settings.judge_timeout_seconds == 30


def test_judge_model_equal_to_a_synthesis_model_is_rejected_at_startup() -> None:
    for env in (
        _env(JUDGE_PROVIDER="gemini", JUDGE_MODEL="gemini-4-argon"),  # == GEMINI_FAST_MODEL default
        _env(JUDGE_PROVIDER="gemini", JUDGE_MODEL="gemini-4-pro-x", GEMINI_PRO_MODEL="gemini-4-pro-x"),
        _env(JUDGE_MODEL="claude-opus-5-5", GEMINI_FAST_MODEL="claude-opus-5-5"),
    ):
        with pytest.raises(ValueError, match="must differ from the synthesis model"):
            get_settings(env)


def test_judge_settings_are_validated() -> None:
    bad_envs = (
        _env(JUDGE_PROVIDER="openai"),
        _env(JUDGE_MODEL="claude opus; rm -rf"),
        _env(JUDGE_MODEL=""),
        _env(JUDGE_LOCATION="Global/../x"),
        _env(JUDGE_MAX_TOKENS="10"),
        _env(JUDGE_MAX_TOKENS="100000"),
        _env(JUDGE_TIMEOUT_SECONDS="0"),
        _env(JUDGE_TIMEOUT_SECONDS="999"),
        _env(JUDGE_PROVIDER="gemini"),  # default claude model is not a Gemini judge
        _env(JUDGE_MODEL="gemini-4-argon-judge"),  # anthropic provider needs a claude-* model
    )
    for env in bad_envs:
        with pytest.raises(ValueError):
            get_settings(env)
