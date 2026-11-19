"""TDD suite for Slice #1: FCIS Enterprise TCO & Workload-Fit Engine + Starlette API routes."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.api.tco_api import clear_tco_cohort_cache, get_tco_routes
from app.core.config import ALL_LEVEL_IDS, ArenaSettings
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
    TcoValidationError,
    TcoWorkload,
    estimate_enterprise_tco,
    parse_tco_workload,
)


def _build_tco_client() -> TestClient:
    settings = ArenaSettings(demo_mode="replay")
    app = Starlette(debug=False, routes=get_tco_routes())
    app.state.settings = settings
    return TestClient(app)


def _assert_validation_error(payload: object, expected_substring: str = "") -> None:
    try:
        parse_tco_workload(payload)  # type: ignore[arg-type]
    except TcoValidationError as exc:
        if expected_substring:
            assert expected_substring in str(exc), f"Expected {expected_substring!r} in {str(exc)!r}"
        return
    raise AssertionError(f"Expected TcoValidationError for payload {payload!r}")


def test_parse_tco_workload_enforces_strict_bounds_enums_and_types() -> None:
    """Boundary value analysis (BVA) for all 7 workload parameters and strict ingress validation."""
    default_parsed = parse_tco_workload({})
    assert default_parsed == DEFAULT_WORKLOAD

    # Exact lower bounds succeed
    min_wl = parse_tco_workload(
        {
            "data_gravity": "bigquery_warehouse",
            "monthly_queries": MONTHLY_QUERIES_BOUNDS[0],
            "corpus_pages": CORPUS_PAGES_BOUNDS[0],
            "latency_sla": "sub_second_retrieval",
            "compliance_posture": "standard_serverless",
            "doc_complexity": "plain_text",
            "synthesis_model": "gemini-3.8-flash",
        }
    )
    assert min_wl.monthly_queries == 1_000
    assert min_wl.corpus_pages == 100

    # Exact upper bounds succeed
    max_wl = parse_tco_workload(
        {
            "data_gravity": "managed_turnkey",
            "monthly_queries": MONTHLY_QUERIES_BOUNDS[1],
            "corpus_pages": CORPUS_PAGES_BOUNDS[1],
            "latency_sla": "deep_agentic_reasoning",
            "compliance_posture": "cmek_vpc",
            "doc_complexity": "multi_hop_math",
            "synthesis_model": "gemini-4-argon",
        }
    )
    assert max_wl.monthly_queries == 10_000_000
    assert max_wl.corpus_pages == 500_000

    # Out-of-range numeric boundaries fail loud (N-1, N+1, negatives, booleans, floats, strings)
    _assert_validation_error({"monthly_queries": 999}, "monthly_queries")
    _assert_validation_error({"monthly_queries": 10_000_001}, "monthly_queries")
    _assert_validation_error({"monthly_queries": 0}, "monthly_queries")
    _assert_validation_error({"monthly_queries": -1000}, "monthly_queries")
    _assert_validation_error({"monthly_queries": True}, "monthly_queries")
    _assert_validation_error({"monthly_queries": 5000.5}, "monthly_queries")
    _assert_validation_error({"monthly_queries": "25000"}, "monthly_queries")

    _assert_validation_error({"corpus_pages": 99}, "corpus_pages")
    _assert_validation_error({"corpus_pages": 500_001}, "corpus_pages")
    _assert_validation_error({"corpus_pages": False}, "corpus_pages")
    _assert_validation_error({"corpus_pages": 250.25}, "corpus_pages")

    # Invalid enum values and unknown fields fail loud
    _assert_validation_error({"data_gravity": "snowflake"}, "data_gravity")
    _assert_validation_error({"latency_sla": "batch_overnight"}, "latency_sla")
    _assert_validation_error({"compliance_posture": "hipaa_custom"}, "compliance_posture")
    _assert_validation_error({"doc_complexity": "audio_video"}, "doc_complexity")
    _assert_validation_error({"synthesis_model": "gpt-4o"}, "synthesis_model")
    _assert_validation_error({"unexpected_field": 123}, "Unknown")
    _assert_validation_error(["not", "a", "dict"], "JSON object")

    # Expanded Gemini 4 + Anthropic Claude TCO synthesis models all parse cleanly; default is Gemini 4 Hydrogen
    assert DEFAULT_WORKLOAD.synthesis_model == "gemini-4-hydrogen"
    assert TCO_SYNTHESIS_MODELS == (
        "gemini-4-hydrogen",
        "gemini-4-carbon",
        "gemini-4-argon",
        "claude-sonnet-4-6",
        "claude-opus-5-5",
        "gemini-3.8-flash",
    )
    for model_id in TCO_SYNTHESIS_MODELS:
        assert parse_tco_workload({"synthesis_model": model_id}).synthesis_model == model_id
    for preset in ENTERPRISE_SCENARIO_PRESETS:
        assert preset.workload.synthesis_model != "gemini-3.8-flash"


def test_pure_core_never_invents_hardcoded_benchmark_scores() -> None:
    """Spec P3 authenticity invariant: pure core without recorded cohorts emits None for QA/GR/CP."""
    est = estimate_enterprise_tco(DEFAULT_WORKLOAD)
    assert len(est.architectures) == len(ALL_LEVEL_IDS) == 8
    assert tuple(a.level_id for a in est.architectures) == ALL_LEVEL_IDS == (
        "managed_vertex",
        "rag_engine",
        "level1_naive",
        "level2_hybrid",
        "alloydb_scann",
        "vertex_vector_v2",
        "level3_multimodal",
        "level4_agentic",
    )
    for idx, arch in enumerate(est.architectures, start=1):
        assert arch.order == idx
        assert arch.short_label.startswith(f"L{idx} · ")
        assert arch.recorded_cohort is None
        assert arch.alternate_cohort is None


def test_standing_compute_ingestion_and_scaling_math_across_all_eight_levels() -> None:
    """Verify $0/mo serverless idle vs L7 AlloyDB & L8 VVS2 dedicated clusters, DocAI+Vision ingest, and crossover."""
    flash_wl = TcoWorkload(
        data_gravity="unstructured_pdfs",
        monthly_queries=25_000,
        corpus_pages=5_000,
        latency_sla="interactive_rag",
        compliance_posture="standard_serverless",
        doc_complexity="tables_diagrams",
        synthesis_model="gemini-3.8-flash",
    )
    argon_wl = TcoWorkload(
        data_gravity="unstructured_pdfs",
        monthly_queries=25_000,
        corpus_pages=5_000,
        latency_sla="interactive_rag",
        compliance_posture="standard_serverless",
        doc_complexity="tables_diagrams",
        synthesis_model="gemini-4-argon",
    )
    flash_est = estimate_enterprise_tco(flash_wl)
    argon_est = estimate_enterprise_tco(argon_wl)
    by_id_flash = {a.level_id: a for a in flash_est.architectures}
    by_id_argon = {a.level_id: a for a in argon_est.architectures}

    # L1..L6 have $0.00/mo standing compute in serverless mode; L7 & L8 carry 24x7 dedicated compute
    for serverless_id in (
        "level1_naive",
        "level2_hybrid",
        "level3_multimodal",
        "level4_agentic",
        "managed_vertex",
        "rag_engine",
    ):
        assert by_id_flash[serverless_id].standing_monthly_usd == 0.0
        assert by_id_flash[serverless_id].idle_mode == "serverless_zero"

    assert by_id_flash["alloydb_scann"].standing_monthly_usd > 200.0
    assert by_id_flash["vertex_vector_v2"].standing_monthly_usd > 150.0
    assert by_id_flash["alloydb_scann"].idle_mode == "dedicated_cluster"
    assert by_id_flash["vertex_vector_v2"].idle_mode == "dedicated_replica"
    assert "Spanner Basic" in by_id_flash["rag_engine"].standing_note

    # Argon synthesis increases query-variable cost across every architecture while standing cost stays identical
    for level_id in ALL_LEVEL_IDS:
        assert by_id_argon[level_id].standing_monthly_usd == by_id_flash[level_id].standing_monthly_usd
        assert by_id_argon[level_id].synthesis_monthly_usd > by_id_flash[level_id].synthesis_monthly_usd
        assert by_id_argon[level_id].total_monthly_usd > by_id_flash[level_id].total_monthly_usd

    # L3 & L4 include Document AI Layout Parser ($10/1k pages = $50 for 5k pages) + Gemini Vision on diagram pages
    assert by_id_flash["level3_multimodal"].one_time_ingestion_usd >= 50.0
    assert by_id_flash["level4_agentic"].one_time_ingestion_usd == by_id_flash["level3_multimodal"].one_time_ingestion_usd
    assert by_id_flash["level3_multimodal"].one_time_ingestion_usd > by_id_flash["level2_hybrid"].one_time_ingestion_usd * 10

    plain_wl = TcoWorkload(
        data_gravity="unstructured_pdfs",
        monthly_queries=25_000,
        corpus_pages=5_000,
        latency_sla="interactive_rag",
        compliance_posture="standard_serverless",
        doc_complexity="plain_text",
        synthesis_model="gemini-3.8-flash",
    )
    plain_est = estimate_enterprise_tco(plain_wl)
    plain_l3 = {a.level_id: a for a in plain_est.architectures}["level3_multimodal"]
    assert by_id_flash["level3_multimodal"].one_time_ingestion_usd > plain_l3.one_time_ingestion_usd

    # Total monthly = standing + storage + query_variable (within 1 cent rounding)
    for arch in flash_est.architectures:
        summed = arch.standing_monthly_usd + arch.storage_monthly_usd + arch.query_variable_monthly_usd
        assert abs(arch.total_monthly_usd - summed) <= 0.02
        var_sum = (
            arch.embedding_monthly_usd
            + arch.retrieval_scan_monthly_usd
            + arch.rerank_monthly_usd
            + arch.synthesis_monthly_usd
        )
        assert abs(arch.query_variable_monthly_usd - var_sum) <= 0.02


def test_crossover_analysis_and_high_qps_replica_scaling() -> None:
    """Dedicated ScaNN loses on TCO at 1k queries/mo, crosses below VAIS at scale, and scales replicas at 10M/mo."""
    low_q = estimate_enterprise_tco(
        parse_tco_workload({"monthly_queries": 1_000, "corpus_pages": 5_000})
    )
    high_q = estimate_enterprise_tco(
        parse_tco_workload({"monthly_queries": 1_000_000, "corpus_pages": 100_000})
    )
    max_q = estimate_enterprise_tco(
        parse_tco_workload({"monthly_queries": 10_000_000, "corpus_pages": 500_000})
    )

    low_map = {a.level_id: a for a in low_q.architectures}
    high_map = {a.level_id: a for a in high_q.architectures}
    max_map = {a.level_id: a for a in max_q.architectures}

    # At 1k queries/mo, serverless L5 VAIS and L2 BQ Hybrid are dramatically cheaper than L7/L8
    assert low_map["managed_vertex"].total_monthly_usd < low_map["vertex_vector_v2"].total_monthly_usd
    assert low_map["level2_hybrid"].total_monthly_usd < low_map["alloydb_scann"].total_monthly_usd
    assert low_q.crossover.regime == "serverless_favorable"

    # At 1M queries/mo & 100k pages, L8 Vector Search 2.0 beats L5 VAIS ($4/1k = $4,000 search alone!) and L3 unindexed BQ scan
    assert high_map["vertex_vector_v2"].total_monthly_usd < high_map["managed_vertex"].total_monthly_usd
    assert high_map["vertex_vector_v2"].total_monthly_usd < high_map["level3_multimodal"].total_monthly_usd
    assert high_q.crossover.regime == "dedicated_scann_favorable"

    # Crossover vs BigQuery multimodal shrinks as corpus_pages grows from 5k -> 100k
    assert high_q.crossover.vvs2_vs_bq_multimodal_queries < low_q.crossover.vvs2_vs_bq_multimodal_queries
    assert 40_000 <= low_q.crossover.vvs2_vs_vais_queries <= 120_000

    # At 10M queries/mo, dedicated clusters scale capacity units > 1 (honest QPS scaling)
    assert max_map["alloydb_scann"].standing_monthly_usd > low_map["alloydb_scann"].standing_monthly_usd
    assert max_map["vertex_vector_v2"].standing_monthly_usd > low_map["vertex_vector_v2"].standing_monthly_usd


def test_workload_fit_recommendations_cover_enterprise_archetypes() -> None:
    """Verify deterministic Primary + Runner-up recommendations across all 5 enterprise presets."""
    assert len(ENTERPRISE_SCENARIO_PRESETS) >= 5
    seen_primaries: set[str] = set()
    for preset in ENTERPRISE_SCENARIO_PRESETS:
        est = estimate_enterprise_tco(preset.workload)
        assert est.recommended_level_id != "level1_naive", "Baseline L1 should never be primary enterprise recommendation"
        assert est.runner_up_level_id != est.recommended_level_id
        assert est.runner_up_level_id != "level1_naive"
        seen_primaries.add(est.recommended_level_id)
        primary = next(a for a in est.architectures if a.level_id == est.recommended_level_id)
        runner_up = next(a for a in est.architectures if a.level_id == est.runner_up_level_id)
        assert primary.fit_score >= runner_up.fit_score
        assert primary.fit_tier == "recommended"
        assert runner_up.fit_tier == "runner_up"
        assert len(primary.rationale) >= 2

    # Ensure distinct enterprise archetypes select distinct GCP RAG architectures
    assert "level2_hybrid" in seen_primaries
    assert "level3_multimodal" in seen_primaries
    assert "level4_agentic" in seen_primaries
    assert "alloydb_scann" in seen_primaries
    assert seen_primaries.intersection({"rag_engine", "managed_vertex"})


def test_tco_api_endpoints_join_real_recorded_cohorts_and_reject_bad_inputs() -> None:
    """Verify GET /api/v1/tco/defaults and POST /api/v1/tco/estimate join real recordings and enforce HTTP 400."""
    clear_tco_cohort_cache()
    client = _build_tco_client()

    res_def = client.get("/api/v1/tco/defaults")
    assert res_def.status_code == 200
    body_def = res_def.json()
    assert body_def["defaults"]["monthly_queries"] == DEFAULT_WORKLOAD.monthly_queries
    assert body_def["bounds"]["monthly_queries"] == list(MONTHLY_QUERIES_BOUNDS)
    assert body_def["bounds"]["corpus_pages"] == list(CORPUS_PAGES_BOUNDS)
    assert body_def["enums"]["data_gravity"] == list(DATA_GRAVITY_OPTIONS)
    assert body_def["enums"]["latency_sla"] == list(LATENCY_SLA_OPTIONS)
    assert body_def["enums"]["compliance_posture"] == list(COMPLIANCE_POSTURE_OPTIONS)
    assert body_def["enums"]["doc_complexity"] == list(DOC_COMPLEXITY_OPTIONS)
    assert body_def["enums"]["synthesis_model"] == list(TCO_SYNTHESIS_MODELS)
    assert len(body_def["presets"]) >= 5

    # Selecting Claude synthesis adds explicit Level 8 ADK Gemini-4 worker watch-out
    claude_est = estimate_enterprise_tco(parse_tco_workload({"synthesis_model": "claude-sonnet-4-6"}))
    claude_l8 = next(a for a in claude_est.architectures if a.level_id == "level4_agentic")
    assert any("Gemini 4 worker" in t and "ADK" in t for t in claude_l8.tradeoffs)

    # Standing levels join direct Gemini 4 Hydrogen cohorts; ephemeral ScaNN levels fall back honestly to reference Argon cohort
    initial_archs = {a["level_id"]: a for a in body_def["estimate"]["architectures"]}
    assert body_def["defaults"]["synthesis_model"] == "gemini-4-hydrogen"
    for standing_id in ("managed_vertex", "rag_engine", "level1_naive", "level2_hybrid", "level3_multimodal", "level4_agentic"):
        assert initial_archs[standing_id]["recorded_cohort"] is not None
        assert initial_archs[standing_id]["recorded_cohort"]["synthesis_model"] == "gemini-4-hydrogen"
        assert initial_archs[standing_id]["recorded_cohort"]["runs"] == 8
        assert initial_archs[standing_id]["alternate_cohort"] is None
    for scann_id in ("alloydb_scann", "vertex_vector_v2"):
        assert initial_archs[scann_id]["recorded_cohort"] is None
        assert initial_archs[scann_id]["alternate_cohort"] is not None
        assert initial_archs[scann_id]["alternate_cohort"]["synthesis_model"] == "gemini-4-argon"
        assert initial_archs[scann_id]["alternate_cohort"]["runs"] == 8

    # POST /api/v1/tco/estimate with Argon joins exact Argon cohorts across all 8 levels
    res_argon = client.post(
        "/api/v1/tco/estimate",
        json={
            "data_gravity": "unstructured_pdfs",
            "monthly_queries": 15_000,
            "corpus_pages": 10_000,
            "latency_sla": "deep_agentic_reasoning",
            "compliance_posture": "cmek_vpc",
            "doc_complexity": "multi_hop_math",
            "synthesis_model": "gemini-4-argon",
        },
    )
    assert res_argon.status_code == 200
    est_argon = res_argon.json()
    assert est_argon["recommended_level_id"] == "level4_agentic"
    argon_archs = {a["level_id"]: a for a in est_argon["architectures"]}
    for lid in ALL_LEVEL_IDS:
        assert argon_archs[lid]["recorded_cohort"] is not None
        assert argon_archs[lid]["recorded_cohort"]["synthesis_model"] == "gemini-4-argon"
    assert argon_archs["level4_agentic"]["recorded_cohort"]["avg_qa_relevance"] >= 0.95

    # Strict input rejection on POST /api/v1/tco/estimate
    bad_payloads: list[object] = [
        {"monthly_queries": 500},
        {"monthly_queries": 25_000_000},
        {"monthly_queries": True},
        {"corpus_pages": 10},
        {"corpus_pages": 999_999},
        {"data_gravity": "oracle_exadata"},
        {"synthesis_model": "gpt-4o"},
        {"unknown_extra_key": "boom"},
        ["array", "body"],
    ]
    for bad in bad_payloads:
        resp = client.post("/api/v1/tco/estimate", json=bad)
        assert resp.status_code in (400, 422), f"Expected 400/422 for {bad!r}, got {resp.status_code}"
        assert "error" in resp.json()

    resp_bad_json = client.post(
        "/api/v1/tco/estimate",
        content=b"{not-valid-json",
        headers={"content-type": "application/json"},
    )
    assert resp_bad_json.status_code in (400, 422)

    resp_oversized = client.post(
        "/api/v1/tco/estimate",
        content=b"{" + b" " * 20_000 + b"}",
        headers={"content-type": "application/json"},
    )
    assert resp_oversized.status_code in (400, 413, 422)
