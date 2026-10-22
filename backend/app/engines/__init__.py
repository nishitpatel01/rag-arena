"""Engine registry gated by ``ENABLED_LEVELS`` (spec P4). Parked engines live under ``engines/parked`` and are
never registered. In ``DEMO_MODE=replay`` every enabled level is served by a ``ReplayRAGEngine`` that mirrors
the live engine's metadata (label, stages, tier) and plays back recordings."""

from __future__ import annotations

from typing import Any

from app.core.architectures import get_architecture_descriptor
from app.core.config import ALL_LEVEL_IDS, ArenaSettings, get_settings
from app.core.models_catalog import RunModels
from app.engines.base import BaseRAGEngine
from app.engines.level1_naive import Level1NaiveEngine, build_vector_search_sql
from app.engines.level2_hybrid import Level2HybridEngine, expand_procurement_acronyms, prepare_query
from app.engines.level3_multimodal import (
    Level3MultimodalEngine,
    format_chunk_with_authority_header,
)
from app.engines.level4_agentic import Level4AgenticEngine
from app.engines.managed_vertex import ManagedVertexSearchEngine
from app.engines.replay_engine import ReplayRAGEngine

ENGINE_CLASSES: tuple[type[BaseRAGEngine], ...] = (
    Level1NaiveEngine,
    Level2HybridEngine,
    Level3MultimodalEngine,
    Level4AgenticEngine,
    ManagedVertexSearchEngine,
)
_ENGINE_CLASS_BY_ID: dict[str, type[BaseRAGEngine]] = {cls.engine_id: cls for cls in ENGINE_CLASSES}

if tuple(_ENGINE_CLASS_BY_ID) != ALL_LEVEL_IDS:
    raise RuntimeError(f"ENGINE_CLASSES {tuple(_ENGINE_CLASS_BY_ID)} must match config.ALL_LEVEL_IDS {ALL_LEVEL_IDS}")


def _resolve(settings: ArenaSettings | None) -> ArenaSettings:
    resolved = get_settings() if settings is None else settings
    if not isinstance(resolved, ArenaSettings):
        raise TypeError("settings must be an ArenaSettings instance or None")
    return resolved


def build_engine(level_id: str, settings: ArenaSettings, models: RunModels | None = None) -> BaseRAGEngine:
    """Instantiate the engine for ``level_id`` for one run: live class in live/record, a replay player in replay.

    ``models`` is the run's (synthesis, judge) selection. Levels 1–3 synthesize with ``models.synthesis``; Level 4 runs
    its ADK agent on ``models.synthesis`` (Gemini only — a Claude selection raises ``ValueError`` → HTTP 400); replay
    plays the recording of exactly that pair. The legacy level (5) cannot switch models yet and rejects any
    synthesis model other than ``GEMINI_FAST_MODEL`` (fail loud, never a silent substitution).
    """
    if level_id not in _ENGINE_CLASS_BY_ID:
        raise KeyError(f"Unknown level '{level_id}'")
    if models is not None and not isinstance(models, RunModels):
        raise TypeError("models must be a RunModels or None")
    cls = _ENGINE_CLASS_BY_ID[level_id]
    if settings.demo_mode == "replay":
        return ReplayRAGEngine(
            engine_id=cls.engine_id,
            engine_label=cls.engine_label,
            stages=cls.stages,
            tier_profile=cls.tier_profile,
            settings=settings,
            models=models,
        )
    if cls in (Level1NaiveEngine, Level2HybridEngine, Level3MultimodalEngine, Level4AgenticEngine):
        return cls(settings=settings, synthesis_model=models.synthesis if models else None)
    if models is not None and models.synthesis.id != settings.gemini_fast_model:
        raise ValueError(
            f"{level_id} does not support synthesis model selection yet (uses GEMINI_FAST_MODEL="
            f"{settings.gemini_fast_model}); requested synthesis_model={models.synthesis.id}"
        )
    return cls(settings=settings)


def get_engine_registry(settings: ArenaSettings | None = None, models: RunModels | None = None) -> dict[str, BaseRAGEngine]:
    """Engines keyed by id, restricted to ``settings.enabled_levels`` in delivery order (built for one run)."""
    resolved = _resolve(settings)
    return {
        level_id: build_engine(level_id, resolved, models)
        for level_id in ALL_LEVEL_IDS
        if level_id in resolved.enabled_levels
    }


def list_levels(settings: ArenaSettings | None = None) -> list[dict[str, Any]]:
    """Metadata for all five levels (``GET /api/v1/levels``): enabled flag, stages, architecture descriptor."""
    resolved = _resolve(settings)
    levels: list[dict[str, Any]] = []
    for order, level_id in enumerate(ALL_LEVEL_IDS, start=1):
        cls = _ENGINE_CLASS_BY_ID[level_id]
        levels.append(
            {
                "level_id": level_id,
                "label": cls.engine_label,
                "order": order,
                "enabled": level_id in resolved.enabled_levels,
                "stages": list(cls.stages),
                "tier_profile": cls.tier_profile,
                "architecture": get_architecture_descriptor(level_id, resolved).to_dict(),
            }
        )
    return levels


__all__ = [
    "BaseRAGEngine",
    "ENGINE_CLASSES",
    "Level1NaiveEngine",
    "Level2HybridEngine",
    "Level3MultimodalEngine",
    "Level4AgenticEngine",
    "ManagedVertexSearchEngine",
    "ReplayRAGEngine",
    "build_engine",
    "build_vector_search_sql",
    "expand_procurement_acronyms",
    "format_chunk_with_authority_header",
    "get_engine_registry",
    "list_levels",
    "prepare_query",
]
