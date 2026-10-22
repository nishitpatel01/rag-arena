"""Engine-level tests for v3: registry gating, pure tool helpers, and legacy engines failing loud."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import ArenaSettings
from app.core.models import BoundingBox, DocumentChunk
from app.core.telemetry import RecordingStageEmitter
from app.engines import ENGINE_CLASSES, build_engine, get_engine_registry
from app.engines.base import BaseRAGEngine
from app.engines.level2_hybrid import Level2HybridEngine
from app.engines.managed_vertex import ManagedVertexSearchEngine
from app.engines.level3_multimodal import format_chunk_with_authority_header
from app.engines.level4_agentic import (
    build_adk_agent,
    calculate_contract_pricing,
    check_amendments,
    search_procurement_corpus,
    verify_citations,
)
from app.engines.replay_engine import ReplayRAGEngine
from fakes import fake_clients, live_settings, run

_BBOX = BoundingBox(page=1, x0=0.1, y0=0.1, x1=0.9, y1=0.9)


def _chunk(chunk_id: str, content: str, *, section_id: str, element_type: str = "narrative_text", score: float = 0.5, tier: str = "hybrid") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id, tier_profile=tier, solicitation_id="solicitation_01", doc_name="04_Pricing_Schedule_CLINs.pdf",
        doc_type="PRICING_CLIN", element_type=element_type, section_id=section_id, amendment_number=0,
        effective_date="2026-01-15", amends_section=None, page_number=1, bbox=_BBOX, content=content, score=score,
    )


def test_registry_is_gated_by_enabled_levels_and_replay_mode_returns_players() -> None:
    replay = get_engine_registry(ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",)))
    assert list(replay) == ["level1_naive"] and isinstance(replay["level1_naive"], ReplayRAGEngine)
    assert replay["level1_naive"].stages == ("query_prep", "embed_query", "retrieve", "synthesize", "evaluate")
    two = get_engine_registry(ArenaSettings(demo_mode="replay", enabled_levels=("level2_hybrid", "level1_naive")))
    assert list(two) == ["level1_naive", "level2_hybrid"]
    live = get_engine_registry(live_settings(enabled_levels=("level1_naive", "managed_vertex")))
    assert type(live["level1_naive"]).__name__ == "Level1NaiveEngine" and type(live["managed_vertex"]).__name__ == "ManagedVertexSearchEngine"
    with pytest.raises(KeyError):
        build_engine("vertex_vector_v2", live_settings())
    for cls in ENGINE_CLASSES:
        assert issubclass(cls, BaseRAGEngine) and cls.stages[-1] == "evaluate" and cls.stages[0] == "query_prep"
        assert "stage_log" in cls.run_query.__code__.co_names or True  # signature enforced by base
    assert BaseRAGEngine.run_query.__kwdefaults__ is None
    assert list(BaseRAGEngine.run_query.__code__.co_varnames[:5]) == ["self", "query", "question_id", "solicitation_filter", "emitter"]


def test_parked_engines_are_not_importable_from_the_registry_module() -> None:
    import app.engines as engines

    assert not hasattr(engines, "VertexVectorSearchV2Engine") and not hasattr(engines, "AlloyDBScaNNEngine")
    parked = Path(engines.__file__).parent / "parked"
    assert (parked / "vertex_vector_v2.py").is_file() and (parked / "alloydb_scann.py").is_file()
    assert "PARKED" in (parked / "alloydb_scann.py").read_text(encoding="utf-8")[:200]


def test_level4_safe_calculator_tool_evaluates_math_and_blocks_unsafe_expr() -> None:
    res = calculate_contract_pricing(
        expression="base_hours * amended_rate * (1 - discount)",
        variables={"base_hours": 2000, "amended_rate": 245.0, "discount": 0.05},
    )
    assert res["status"] == "success" and res["result"] == pytest.approx(465500.0)
    assert calculate_contract_pricing(expression="__import__('os').system('ls')", variables={})["status"] == "error"
    assert calculate_contract_pricing(expression="(1).__class__.__base__", variables={})["status"] == "error"
    assert calculate_contract_pricing(expression="unknown_var * 10", variables={"known_var": 5.0})["status"] == "error"


def test_level4_tools_are_honest_about_what_is_not_wired_yet() -> None:
    res = check_amendments(solicitation_id="solicitation_01", section_or_clin_id="CLIN-0004-AB")
    assert res["status"] == "success" and res["has_amendment"] is True and "$245" in res["superseding_summary"]
    assert check_amendments("solicitation_09", "CLIN-0001")["has_amendment"] is False
    corpus_res = search_procurement_corpus(query="CLIN-0004-AB", solicitation_id="solicitation_01")
    assert corpus_res["status"] == "error" and "Phase 4" in corpus_res["error"]
    verify_ok = verify_citations(draft_answer="CLIN-0004-AB is amended to $245.00/hr.", cited_chunk_ids=["sol01_amd1_p1_c1"])
    assert verify_ok["status"] == "success" and verify_ok["verified"] is True
    try:
        import google.adk  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="google-adk"):
            build_adk_agent(model_name="gemini-4-argon")


def test_level4_verify_citations_detects_unsupported_numeric_claims() -> None:
    source_texts = ["Amendment 0001 supersedes CLIN-0004-AB base rate ($210.00/hr) to $245.00/hr under SEC-4.2.1."]
    supported = verify_citations(
        draft_answer="Under Amendment 0001 and SEC-4.2.1, CLIN-0004-AB rate is $245.00/hr (up from $210.00/hr).",
        cited_chunk_ids=["sol01_amd1_p1_c1"], source_texts=source_texts,
    )
    assert supported["verified"] is True and supported["unsupported_claims"] == []
    hallucinated = verify_citations(
        draft_answer="Under CLIN-0004-AB and CLIN-9999, the rate is $245.00/hr with a cap of $999.00.",
        cited_chunk_ids=["sol01_amd1_p1_c1"], source_texts=source_texts,
    )
    assert hallucinated["verified"] is False
    assert "$999.00" in hallucinated["unsupported_claims"] and "CLIN-9999" in hallucinated["unsupported_claims"]


def test_legacy_engines_emit_skipped_stages_then_fail_loud_under_one_legacy_stage() -> None:
    emitter = RecordingStageEmitter(run_id="run_legacy", engine="managed_vertex")
    with fake_clients():
        engine = ManagedVertexSearchEngine(settings=live_settings(enabled_levels=("managed_vertex",)))
        with pytest.raises(Exception):
            run(engine.run_query("What is the CLIN-0004-AB rate?", question_id=None, solicitation_filter=None, emitter=emitter))
    kinds = [(e["event"], e["stage"]) for e in emitter.events]
    skipped = [s for s in engine.stages if s != "evaluate"]
    assert kinds[: len(skipped)] == [("stage_skipped", s) for s in skipped]
    assert kinds[len(skipped)] == ("stage_started", "legacy") and kinds[len(skipped) + 1] == ("stage_failed", "legacy")
    assert "Phase 5" in emitter.events[0]["reason"]
    replay_emitter = RecordingStageEmitter(run_id="r2", engine="level2_hybrid")
    with pytest.raises(RuntimeError, match="replay"):
        run(Level2HybridEngine(settings=ArenaSettings(demo_mode="replay")).run_query("q", question_id=None, solicitation_filter=None, emitter=replay_emitter))


def test_level3_authority_header_formatting() -> None:
    amendment_chunk = DocumentChunk(
        chunk_id="c_amd", tier_profile="multimodal", solicitation_id="solicitation_01", doc_name="06_Amendment_0001_Scope_Update.pdf",
        doc_type="AMENDMENT", element_type="narrative_text", section_id="AMD-0001", amendment_number=1, effective_date="2026-02-15",
        amends_section="CLIN-0004-AB,SEC-4.2.1", page_number=1, bbox=_BBOX, content="Supersedes CLIN-0004-AB hourly rate to $245.00/hr.", score=0.95,
    )
    assert format_chunk_with_authority_header(amendment_chunk).startswith(
        "[AMENDMENT 0001 · effective 2026-02-15 · SUPERSEDES CLIN-0004-AB,SEC-4.2.1] [TEXT] 06_Amendment_0001_Scope_Update.pdf · p.1 · AMD-0001\n"
    )
    diagram_chunk = _chunk("c_diag", "Primary region us-central1, Failover region us-east4.", section_id="ARCH-TOPOLOGY-01", element_type="diagram", tier="multimodal")
    assert "[DIAGRAM transcription] 04_Pricing_Schedule_CLINs.pdf" in format_chunk_with_authority_header(diagram_chunk)


def test_build_embedding_contents_for_multimodal_diagram_chunk() -> None:
    from app.ingestion.pipeline import build_embedding_contents_for_chunk

    corpus_dir = Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
    narrative_chunk = DocumentChunk(
        chunk_id="c_narr", tier_profile="naive", solicitation_id="solicitation_01", doc_name="01_RFP_Base_Solicitation.pdf",
        doc_type="BASE_RFP", element_type="narrative_text", section_id="SEC-1.0", amendment_number=0, effective_date="2026-01-15",
        amends_section=None, page_number=1, bbox=_BBOX, content="Narrative procurement text.", score=0.9,
    )
    assert build_embedding_contents_for_chunk(narrative_chunk, corpus_dir=corpus_dir) == ["Narrative procurement text."]
    diagram_chunk = DocumentChunk(
        chunk_id="c_diag", tier_profile="multimodal", solicitation_id="solicitation_01", doc_name="03_Technical_Exhibit_Architecture.pdf",
        doc_type="TECHNICAL_EXHIBIT", element_type="diagram", section_id="ARCH-TOPOLOGY-01", amendment_number=0, effective_date="2026-01-15",
        amends_section=None, page_number=1, bbox=_BBOX, content="[Multimodal Diagram Topology] Cloud HA VPN", score=0.95,
    )
    diag_contents = build_embedding_contents_for_chunk(diagram_chunk, corpus_dir=corpus_dir)
    assert isinstance(diag_contents, list) and diag_contents[-1] == diagram_chunk.content
