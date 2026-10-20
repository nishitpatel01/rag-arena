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


def test_defaults_enable_only_level1_in_replay_mode() -> None:
    settings = get_settings(_env())
    assert settings.enabled_levels == ("level1_naive",)
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
