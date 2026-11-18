"""TDD suite for the deterministic Model Pair Recommender (`app.core.model_recommender`)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core.config import ArenaSettings
from app.core.model_recommender import (
    PROFILE_ANTHROPIC_WORKER,
    PROFILE_BALANCED_ENTERPRISE,
    PROFILE_COST_LATENCY,
    PROFILE_MULTIMODAL_AGENTIC,
    RECOMMENDATION_PROFILE_IDS,
    RECOMMENDED_PROFILES,
    ModelPairRecommendation,
    is_cross_family_pair,
    model_family,
    recommend_model_pair,
    recommended_pairs_for_catalog,
)
from app.core.models_catalog import VERTEX_GENAI_EVAL_MODEL_ID, load_catalog
from app.main import create_app
from fakes import live_settings, write_verification_file


def test_canonical_profiles_enforce_cross_family_and_distinct_pairs() -> None:
    assert tuple(p.profile_id for p in RECOMMENDED_PROFILES) == RECOMMENDATION_PROFILE_IDS == (
        PROFILE_COST_LATENCY,
        PROFILE_BALANCED_ENTERPRISE,
        PROFILE_MULTIMODAL_AGENTIC,
        PROFILE_ANTHROPIC_WORKER,
    )
    by_id = {p.profile_id: p for p in RECOMMENDED_PROFILES}
    assert (by_id[PROFILE_COST_LATENCY].synthesis_model, by_id[PROFILE_COST_LATENCY].judge_model) == (
        "gemini-4-hydrogen",
        "claude-sonnet-4-6",
    )
    assert by_id[PROFILE_COST_LATENCY].alt_judge_model == VERTEX_GENAI_EVAL_MODEL_ID
    assert (by_id[PROFILE_BALANCED_ENTERPRISE].synthesis_model, by_id[PROFILE_BALANCED_ENTERPRISE].judge_model) == (
        "gemini-4-carbon",
        "claude-opus-5-5",
    )
    assert (by_id[PROFILE_MULTIMODAL_AGENTIC].synthesis_model, by_id[PROFILE_MULTIMODAL_AGENTIC].judge_model) == (
        "gemini-4-argon",
        "claude-opus-5-5",
    )
    assert (by_id[PROFILE_ANTHROPIC_WORKER].synthesis_model, by_id[PROFILE_ANTHROPIC_WORKER].judge_model) == (
        "claude-sonnet-4-6",
        "gemini-4-argon",
    )
    for profile in RECOMMENDED_PROFILES:
        assert profile.synthesis_model != profile.judge_model
        assert profile.cross_family is True
        assert is_cross_family_pair(profile.synthesis_model, profile.judge_model)
        assert profile.rationale and profile.judge_rationale

    with pytest.raises(ValueError):
        ModelPairRecommendation(
            profile_id="bad",
            profile_label="Bad",
            synthesis_model="gemini-4-argon",
            judge_model="gemini-4-argon",
            alt_judge_model=None,
            rationale="r",
            judge_rationale="jr",
        )


def test_recommend_model_pair_maps_workload_and_enforces_adk_gemini_only() -> None:
    high_qps = recommend_model_pair(monthly_queries=1_000_000, latency_sla="sub_second_retrieval")
    assert high_qps.profile_id == PROFILE_COST_LATENCY
    assert (high_qps.synthesis_model, high_qps.judge_model) == ("gemini-4-hydrogen", "claude-sonnet-4-6")

    bq = recommend_model_pair(data_gravity="bigquery_warehouse", doc_complexity="plain_text", latency_sla="interactive_rag")
    assert bq.profile_id == PROFILE_BALANCED_ENTERPRISE
    assert (bq.synthesis_model, bq.judge_model) == ("gemini-4-carbon", "claude-opus-5-5")

    mm = recommend_model_pair(doc_complexity="tables_diagrams")
    assert mm.profile_id == PROFILE_MULTIMODAL_AGENTIC
    assert (mm.synthesis_model, mm.judge_model) == ("gemini-4-argon", "claude-opus-5-5")

    bakeoff = recommend_model_pair(level_id="level2_hybrid", prefer_anthropic_worker=True)
    assert bakeoff.profile_id == PROFILE_ANTHROPIC_WORKER
    assert (bakeoff.synthesis_model, bakeoff.judge_model) == ("claude-sonnet-4-6", "gemini-4-argon")
    assert bakeoff.adk_compatible is False

    # Level 8 ADK must ALWAYS return a Vertex Gemini worker even if anthropic_worker is requested
    adk_override = recommend_model_pair(level_id="level4_agentic", profile_id=PROFILE_ANTHROPIC_WORKER)
    assert model_family(adk_override.synthesis_model) == "gemini"
    assert adk_override.synthesis_model == "gemini-4-argon"
    assert adk_override.judge_model == "claude-opus-5-5"
    assert adk_override.adk_compatible is True
    assert "ADK" in adk_override.rationale

    adk_pref = recommend_model_pair(reasoning_mode="adk_agent", prefer_anthropic_worker=True)
    assert model_family(adk_pref.synthesis_model) == "gemini"

    # Rubric judge preference switches to vertex-genai-eval
    rubric = recommend_model_pair(profile_id=PROFILE_COST_LATENCY, prefer_rubric_judge=True)
    assert rubric.judge_model == VERTEX_GENAI_EVAL_MODEL_ID
    assert rubric.cross_family is True

    with pytest.raises(ValueError):
        recommend_model_pair(profile_id="unknown_profile")


def test_recommend_model_pair_respects_catalog_availability_and_api_exposure() -> None:
    partial_flags = {
        "gemini-4-hydrogen": False,
        "gemini-4-carbon": True,
        "gemini-4-argon": True,
        "claude-sonnet-4-6": False,
        "claude-opus-5-5": True,
        "gemini-3.8-flash": True,
        "claude-fable-5-2": False,
        "claude-fable-5-1": False,
    }
    catalog = load_catalog(live_settings(model_verification_file=str(write_verification_file(partial_flags))))
    rec = recommend_model_pair(profile_id=PROFILE_COST_LATENCY, catalog=catalog)
    # Hydrogen & Sonnet 4.6 unavailable -> resolves to available cross-family pair
    assert rec.synthesis_model in ("gemini-4-argon", "gemini-4-carbon")
    assert rec.judge_model == "claude-opus-5-5"
    assert rec.cross_family is True

    pairs = recommended_pairs_for_catalog(catalog)
    assert len(pairs) == 4
    for p in pairs:
        assert p["synthesis_model"] != p["judge_model"]
        assert catalog.get(p["synthesis_model"]).available is True
        assert catalog.get(p["judge_model"]).available is True

    # GET /api/v1/models exposes recommended_pairs
    replay_settings = ArenaSettings(demo_mode="replay", model_verification_file=str(write_verification_file()))
    body = TestClient(create_app(settings=replay_settings)).get("/api/v1/models").json()
    assert "recommended_pairs" in body
    assert [p["profile_id"] for p in body["recommended_pairs"]] == list(RECOMMENDATION_PROFILE_IDS)
    # In replay mode with 0 vertex-genai-eval recordings, alt_judge_model omits unavailable vertex-genai-eval
    cost_rec = next(p for p in body["recommended_pairs"] if p["profile_id"] == PROFILE_COST_LATENCY)
    assert cost_rec["alt_judge_model"] is None
