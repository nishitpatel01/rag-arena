"""Unit tests for canonical ``PipelineSpec`` schema, validator, presets, and dynamic architecture descriptors."""

from __future__ import annotations

from app.core.architectures import get_architecture_descriptor
from app.core.config import ALL_LEVEL_IDS, ArenaSettings
from app.core.models_catalog import (
    RAG_ENGINE_GROUNDED_MODEL_ID,
    VAIS_SUMMARY_MODEL_ID,
    load_catalog,
)
from app.core.pipeline_spec import (
    BACKING_LEVEL_BY_PROFILE,
    CANONICAL_AGENT_TOOLS,
    CONTEXT_ORDERS,
    INDEX_PROFILES,
    PRESET_LEVEL_IDS,
    QUERY_TRANSFORMS,
    REASONING_MODES,
    RERANKERS,
    RETRIEVAL_MODES,
    PipelineSpec,
    PipelineSpecError,
    QuerySlots,
    active_query_stages,
    descriptor_for_pipeline,
    list_preset_specs,
    preset_spec,
    validate_pipeline_models,
    validate_slot_id,
)
from fakes import live_settings


def _expect_spec_error(fn, needle: str = "") -> str:
    try:
        fn()
    except PipelineSpecError as exc:
        msg = str(exc)
        if needle:
            assert needle.lower() in msg.lower(), f"expected {needle!r} in {msg!r}"
        return msg
    raise AssertionError("expected PipelineSpecError, but call succeeded")


def test_presets_cover_all_six_levels_and_round_trip() -> None:
    settings = ArenaSettings()
    assert PRESET_LEVEL_IDS == ALL_LEVEL_IDS
    presets = list_preset_specs(settings)
    assert len(presets) == 6

    seen_ids: set[str] = set()
    for level_id in ALL_LEVEL_IDS:
        spec = preset_spec(level_id, settings)
        assert spec.preset_level_id(settings) == level_id
        assert spec.pipeline_id.startswith("pipe_")
        assert len(spec.pipeline_id) == 17  # "pipe_" + 12 lowercase hex
        assert spec.pipeline_id not in seen_ids
        seen_ids.add(spec.pipeline_id)

        as_dict = spec.to_dict()
        restored = PipelineSpec.from_dict(as_dict)
        assert restored == spec
        assert restored.pipeline_id == spec.pipeline_id
        assert restored.preset_level_id(settings) == level_id
        assert restored.backing_level_id == BACKING_LEVEL_BY_PROFILE[spec.index_profile]


def test_pipeline_id_is_structural_and_model_independent() -> None:
    settings = ArenaSettings()
    base = preset_spec("rag_engine", settings)
    with_argon = PipelineSpec(
        index_profile=base.index_profile,
        slots=base.slots,
        synthesis_model="gemini-4-argon",
        judge_model="claude-opus-5-5",
        label="L6 Argon",
    )
    with_flash = PipelineSpec(
        index_profile=base.index_profile,
        slots=base.slots,
        synthesis_model="gemini-3.8-flash",
        judge_model="claude-opus-5-5",
        label="L6 Flash",
    )
    assert with_argon.pipeline_id == base.pipeline_id
    assert with_flash.pipeline_id == base.pipeline_id
    assert with_argon.preset_level_id(settings) == "rag_engine"
    assert with_flash.preset_level_id(settings) == "rag_engine"
    assert PipelineSpec.from_dict(with_argon.to_dict()) == with_argon


def test_custom_spec_does_not_match_any_preset() -> None:
    settings = ArenaSettings()
    # Cross-level ablation: Level 6 RAG Engine retrieval + Discovery Engine Ranking API + authority context order.
    custom = PipelineSpec(
        index_profile="rag_engine",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="managed_retrieval",
            candidate_k=20,
            top_k=5,
            reranker="vertex_ranking_api",
            context_order="authority_amendment_first",
            reasoning_mode="single_pass",
        ),
    )
    assert custom.preset_level_id(settings) is None
    assert custom.backing_level_id == "rag_engine"
    assert active_query_stages(custom) == (
        "query_prep",
        "retrieve",
        "rerank",
        "synthesize",
        "evaluate",
    )


def test_active_query_stages_match_all_presets_and_lexical_omits_embed() -> None:
    settings = ArenaSettings()
    assert active_query_stages(preset_spec("level1_naive", settings)) == (
        "query_prep",
        "embed_query",
        "retrieve",
        "synthesize",
        "evaluate",
    )
    assert active_query_stages(preset_spec("level2_hybrid", settings)) == (
        "query_prep",
        "embed_query",
        "retrieve",
        "rerank",
        "synthesize",
        "evaluate",
    )
    assert active_query_stages(preset_spec("level3_multimodal", settings)) == (
        "query_prep",
        "embed_query",
        "retrieve",
        "rerank",
        "synthesize",
        "evaluate",
    )
    assert active_query_stages(preset_spec("level4_agentic", settings)) == (
        "query_prep",
        "agent_turn",
        "synthesize",
        "evaluate",
    )
    assert active_query_stages(preset_spec("managed_vertex", settings)) == (
        "query_prep",
        "retrieve",
        "synthesize",
        "evaluate",
    )
    assert active_query_stages(preset_spec("rag_engine", settings)) == (
        "query_prep",
        "retrieve",
        "synthesize",
        "evaluate",
    )

    lexical_only = PipelineSpec(
        index_profile="hybrid",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="lexical_bm25",
            candidate_k=10,
            top_k=5,
            reranker="none",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    assert active_query_stages(lexical_only) == (
        "query_prep",
        "retrieve",
        "synthesize",
        "evaluate",
    )


def test_agent_tools_canonicalized_in_from_dict() -> None:
    spec = PipelineSpec.from_dict(
        {
            "index_profile": "multimodal",
            "slots": {
                "query_transform": "acronym_plus_intent",
                "retrieval_mode": "hybrid_rrf",
                "candidate_k": 30,
                "top_k": 5,
                "reranker": "vertex_ranking_api",
                "context_order": "authority_amendment_first",
                "reasoning_mode": "adk_agent",
                "agent_tools": ["verify_citations", "search_procurement_corpus"],
            },
        }
    )
    assert spec.slots.agent_tools == ("search_procurement_corpus", "verify_citations")
    assert CANONICAL_AGENT_TOOLS[0] == "search_procurement_corpus"


def test_illegal_combinations_are_rejected_with_explicit_reasons() -> None:
    # 1. BQ retrieval mode on managed profile
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="rag_engine",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="lexical_bm25",
                candidate_k=10,
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "managed_retrieval",
    )

    # 2. Managed retrieval mode on BigQuery profile
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="hybrid",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="managed_retrieval",
                candidate_k=10,
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "bigquery",
    )

    # 3. candidate_k < top_k
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="hybrid",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="hybrid_rrf",
                candidate_k=4,
                top_k=8,
                reranker="vertex_ranking_api",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "candidate_k",
    )

    # 4. bool masquerading as int or out-of-range k
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="naive",
            slots=QuerySlots(
                query_transform="none",
                retrieval_mode="vector_cosine",
                candidate_k=True,  # type: ignore[arg-type]
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "integer",
    )
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="vertex_ai_search",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="managed_retrieval",
                candidate_k=25,  # VAIS page_size ceiling is 20
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "20",
    )
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="rag_engine",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="managed_retrieval",
                candidate_k=60,  # RAG Engine retrieveContexts ceiling is 50
                top_k=10,
                reranker="vertex_ranking_api",
                context_order="score_order",
                reasoning_mode="single_pass",
            ),
        ),
        "50",
    )

    # 5. single_pass with non-empty agent_tools
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="multimodal",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="hybrid_rrf",
                candidate_k=20,
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="single_pass",
                agent_tools=("search_procurement_corpus",),
            ),
        ),
        "single_pass",
    )

    # 6. adk_agent missing search_procurement_corpus or on managed profile
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="multimodal",
            slots=QuerySlots(
                query_transform="acronym_plus_intent",
                retrieval_mode="hybrid_rrf",
                candidate_k=20,
                top_k=5,
                reranker="vertex_ranking_api",
                context_order="authority_amendment_first",
                reasoning_mode="adk_agent",
                agent_tools=("calculate_contract_pricing",),
            ),
        ),
        "search_procurement_corpus",
    )
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="rag_engine",
            slots=QuerySlots(
                query_transform="acronym",
                retrieval_mode="managed_retrieval",
                candidate_k=10,
                top_k=5,
                reranker="none",
                context_order="score_order",
                reasoning_mode="adk_agent",
                agent_tools=("search_procurement_corpus",),
            ),
        ),
        "adk_agent",
    )


def test_pseudo_and_agent_model_compatibility_checks() -> None:
    settings = live_settings(
        vais_data_store_id="ds-proc",
        vais_engine_id="eng-proc",
        rag_engine_corpus_id="54321",
    )
    catalog = load_catalog(settings)

    # VAIS summary pseudo-model rejected on rag_engine or with reranker
    _expect_spec_error(
        lambda: PipelineSpec(
            index_profile="rag_engine",
            slots=preset_spec("rag_engine", settings).slots,
            synthesis_model=VAIS_SUMMARY_MODEL_ID,
        ),
        "vertex_ai_search",
    )
    vais_reranked = PipelineSpec(
        index_profile="vertex_ai_search",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="managed_retrieval",
            candidate_k=10,
            top_k=5,
            reranker="vertex_ranking_api",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    vais_pair = catalog.select(VAIS_SUMMARY_MODEL_ID, "claude-opus-5-5")
    _expect_spec_error(lambda: validate_pipeline_models(vais_reranked, vais_pair), "reranker")

    # RAG Engine grounded pseudo-model rejected with reranker or outside rag_engine
    rag_reranked = PipelineSpec(
        index_profile="rag_engine",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="managed_retrieval",
            candidate_k=10,
            top_k=5,
            reranker="vertex_ranking_api",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    rag_pair = catalog.select(RAG_ENGINE_GROUNDED_MODEL_ID, "claude-opus-5-5")
    _expect_spec_error(lambda: validate_pipeline_models(rag_reranked, rag_pair), "reranker")

    # ADK agent rejected with Anthropic synthesis model
    agent_spec = preset_spec("level4_agentic", settings)
    claude_pair = catalog.select("claude-opus-5-5", "gemini-4-argon")
    _expect_spec_error(lambda: validate_pipeline_models(agent_spec, claude_pair), "vertex_gemini")

    # Valid pair passes
    gemini_pair = catalog.select("gemini-4-argon", "claude-opus-5-5")
    validate_pipeline_models(agent_spec, gemini_pair)
    validate_pipeline_models(rag_reranked, gemini_pair)


def test_dynamic_descriptor_tracks_every_slot_without_dangling_edges() -> None:
    settings = ArenaSettings()

    # Preset returns exact level descriptor
    for level_id in ALL_LEVEL_IDS:
        spec = preset_spec(level_id, settings)
        desc = descriptor_for_pipeline(spec, settings)
        expected = get_architecture_descriptor(level_id, settings)
        assert desc == expected

    # Custom 1: lexical_bm25 without reranker -> NO embed node, NO rank node
    lex_spec = PipelineSpec(
        index_profile="hybrid",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="lexical_bm25",
            candidate_k=10,
            top_k=5,
            reranker="none",
            context_order="score_order",
            reasoning_mode="single_pass",
        ),
    )
    lex_desc = descriptor_for_pipeline(lex_spec, settings)
    lex_ids = {n.id for n in lex_desc.nodes}
    assert "embed" not in lex_ids
    assert "rank" not in lex_ids
    assert {"user", "bq", "llm", "judge"}.issubset(lex_ids)

    # Custom 2: rag_engine + vertex_ranking_api -> includes retrieve, rag_corpus, rank, llm, judge
    rag_rank_spec = PipelineSpec(
        index_profile="rag_engine",
        slots=QuerySlots(
            query_transform="acronym",
            retrieval_mode="managed_retrieval",
            candidate_k=20,
            top_k=5,
            reranker="vertex_ranking_api",
            context_order="authority_amendment_first",
            reasoning_mode="single_pass",
        ),
    )
    rag_desc = descriptor_for_pipeline(rag_rank_spec, settings)
    rag_ids = {n.id for n in rag_desc.nodes}
    assert {"user", "retrieve", "rag_corpus", "rank", "llm", "judge"} == rag_ids

    # Custom 3: adk_agent with 2-tool subset + lexical_bm25 + no reranker -> omits unused tools, embed, rank
    slim_agent = PipelineSpec(
        index_profile="naive",
        slots=QuerySlots(
            query_transform="none",
            retrieval_mode="lexical_bm25",
            candidate_k=8,
            top_k=5,
            reranker="none",
            context_order="score_order",
            reasoning_mode="adk_agent",
            agent_tools=("search_procurement_corpus", "calculate_contract_pricing"),
        ),
    )
    agent_desc = descriptor_for_pipeline(slim_agent, settings)
    agent_ids = {n.id for n in agent_desc.nodes}
    assert "tool_search" in agent_ids
    assert "tool_calc" in agent_ids
    assert "tool_amend" not in agent_ids
    assert "tool_verify" not in agent_ids
    assert "embed" not in agent_ids
    assert "rank" not in agent_ids


def test_validate_slot_id_and_enum_exports() -> None:
    assert validate_slot_id("recipe_a-1") == "recipe_a-1"
    _expect_spec_error(lambda: validate_slot_id(""), "slot_id")
    _expect_spec_error(lambda: validate_slot_id("bad space"), "slot_id")
    assert len(INDEX_PROFILES) == 5
    assert len(QUERY_TRANSFORMS) == 3
    assert len(RETRIEVAL_MODES) == 4
    assert len(RERANKERS) == 2
    assert len(CONTEXT_ORDERS) == 2
    assert len(REASONING_MODES) == 2
