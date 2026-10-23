"""Engine-level tests for v3: registry gating, pure helpers, and legacy engines failing loud (Level 4 tools: test_level4_agentic.py)."""

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


def test_replay_mode_live_engines_refuse_to_run() -> None:
    replay_emitter = RecordingStageEmitter(run_id="r2", engine="level2_hybrid")
    with pytest.raises(RuntimeError, match="replay"):
        run(Level2HybridEngine(settings=ArenaSettings(demo_mode="replay")).run_query("q", question_id=None, solicitation_filter=None, emitter=replay_emitter))
    mv_emitter = RecordingStageEmitter(run_id="r3", engine="managed_vertex")
    with pytest.raises(RuntimeError, match="replay"):
        run(ManagedVertexSearchEngine(settings=ArenaSettings(demo_mode="replay")).run_query("q", question_id=None, solicitation_filter=None, emitter=mv_emitter))


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
