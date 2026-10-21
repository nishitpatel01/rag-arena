"""Tests for ArchitectureDescriptor design metadata (spec §7.2.1) and GET /api/v1/levels."""

from __future__ import annotations

import json
import re

from starlette.testclient import TestClient

from app.core.architectures import (
    CANONICAL_INGEST_STAGES,
    CANONICAL_QUERY_STAGES,
    ArchitectureDescriptor,
    get_architecture_descriptor,
    list_architecture_descriptors,
)
from app.core.config import ALL_LEVEL_IDS, ArenaSettings
from app.engines import ENGINE_CLASSES, get_engine_registry, list_levels
from app.main import create_app

_SCORE_OR_TIMING_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|%|sec|seconds|points?|score)\b", re.IGNORECASE)
_NODE_KINDS = {"actor", "model", "store", "api", "process", "agent"}


def test_descriptors_exist_for_all_five_levels_with_valid_grid_nodes_and_edges() -> None:
    descriptors = list_architecture_descriptors()
    assert [d.level_id for d in descriptors] == list(ALL_LEVEL_IDS)
    for desc in descriptors:
        assert isinstance(desc, ArchitectureDescriptor)
        node_ids = {n.id for n in desc.nodes}
        assert len(node_ids) == len(desc.nodes), f"duplicate node ids in {desc.level_id}"
        for node in desc.nodes:
            assert node.kind in _NODE_KINDS
            assert node.col >= 0 and node.row >= 0
            assert node.label and node.service
            assert set(node.stage_ids) <= set(CANONICAL_QUERY_STAGES)
        for edge in desc.edges:
            assert edge.source in node_ids and edge.target in node_ids, f"dangling edge in {desc.level_id}"
        ingest_ids = {n.id for n in desc.ingest_nodes}
        for node in desc.ingest_nodes:
            assert set(node.stage_ids) <= set(CANONICAL_INGEST_STAGES)
        for edge in desc.ingest_edges:
            assert edge.source in ingest_ids and edge.target in ingest_ids
        assert all(stage in {s for n in desc.ingest_nodes for s in n.stage_ids} for stage in CANONICAL_INGEST_STAGES)


def test_every_canonical_engine_stage_maps_to_at_least_one_node() -> None:
    for engine_cls in ENGINE_CLASSES:
        desc = get_architecture_descriptor(engine_cls.engine_id)
        mapped = {stage for node in desc.nodes for stage in node.stage_ids}
        assert engine_cls.stages, f"{engine_cls.engine_id} declares no stages"
        for stage in engine_cls.stages:
            assert stage in CANONICAL_QUERY_STAGES
            assert stage in mapped, f"stage '{stage}' of {engine_cls.engine_id} is not mapped to a node"


def test_descriptors_carry_no_scores_or_timings() -> None:
    for desc in list_architecture_descriptors():
        text = json.dumps(desc.to_dict())
        assert not _SCORE_OR_TIMING_RE.search(text), f"{desc.level_id} descriptor contains a score/timing: {text[:200]}"
        for key in ("duration_ms", "latency_ms", "battle_score", "groundedness", "accuracy"):
            assert key not in text


def test_levels_endpoint_lists_all_levels_with_enabled_flag_and_descriptor() -> None:
    settings = ArenaSettings(demo_mode="replay", enabled_levels=("level1_naive",))
    levels = list_levels(settings)
    assert [lvl["level_id"] for lvl in levels] == list(ALL_LEVEL_IDS)
    by_id = {lvl["level_id"]: lvl for lvl in levels}
    assert by_id["level1_naive"]["enabled"] is True
    assert by_id["level2_hybrid"]["enabled"] is False
    assert by_id["level1_naive"]["stages"] == ["query_prep", "embed_query", "retrieve", "synthesize", "evaluate"]
    assert by_id["level1_naive"]["architecture"]["level_id"] == "level1_naive"
    assert set(get_engine_registry(settings)) == {"level1_naive"}

    client = TestClient(create_app(settings=settings))
    resp = client.get("/api/v1/levels")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled_levels"] == ["level1_naive"]
    assert [lvl["level_id"] for lvl in body["levels"]] == list(ALL_LEVEL_IDS)
    first = body["levels"][0]
    assert {"level_id", "label", "order", "enabled", "stages", "tier_profile", "architecture"} <= set(first)
    node = first["architecture"]["nodes"][0]
    assert {"id", "label", "service", "kind", "col", "row", "stage_ids"} <= set(node)
    assert set(node) <= {"id", "label", "service", "kind", "col", "row", "stage_ids", "region", "code_ref"}
    bq_node = next(n for n in first["architecture"]["nodes"] if n["id"] == "bq")
    assert bq_node["code_ref"].startswith("app.engines.level1_naive:") and bq_node["region"]
    edge = first["architecture"]["edges"][0]
    assert {"from", "to", "label"} == set(edge)


def test_descriptor_regions_are_placeholders_resolved_from_settings_not_hardcoded() -> None:
    """Regions are never hardcoded: raw descriptors carry placeholders; settings resolve them."""
    from app.core.architectures import GCP_LOCATION_PLACEHOLDER, GENAI_LOCATION_PLACEHOLDER, JUDGE_LOCATION_PLACEHOLDER
    from app.core.config import ArenaSettings

    raw = get_architecture_descriptor("level1_naive")
    raw_regions = {n.region for n in raw.nodes if n.region}
    assert raw_regions == {GCP_LOCATION_PLACEHOLDER, GENAI_LOCATION_PLACEHOLDER, JUDGE_LOCATION_PLACEHOLDER}
    settings = ArenaSettings(
        demo_mode="replay", gcp_location="europe-west4", vertex_genai_location="us-east4", judge_location="global"
    )
    resolved = get_architecture_descriptor("level1_naive", settings)
    resolved_regions = {n.region for n in resolved.nodes if n.region}
    assert resolved_regions == {"europe-west4", "us-east4", "global"}
    dumped = json.dumps(resolved.to_dict())
    for placeholder in (GCP_LOCATION_PLACEHOLDER, GENAI_LOCATION_PLACEHOLDER, JUDGE_LOCATION_PLACEHOLDER):
        assert placeholder not in dumped
    assert len(list_architecture_descriptors(settings)) == 5


def test_judge_node_service_and_region_come_from_judge_settings() -> None:
    """The judge box names the configured judge model (never a hardcoded string) and its JUDGE_LOCATION."""
    from app.core.architectures import JUDGE_LOCATION_PLACEHOLDER, JUDGE_SERVICE_PLACEHOLDER

    raw = get_architecture_descriptor("level1_naive")
    raw_judge = next(n for n in raw.nodes if n.id == "judge")
    assert raw_judge.service == JUDGE_SERVICE_PLACEHOLDER and raw_judge.region == JUDGE_LOCATION_PLACEHOLDER
    settings = ArenaSettings(demo_mode="replay", judge_location="us-east5")
    for desc in list_architecture_descriptors(settings):
        judge = next(n for n in desc.nodes if n.id == "judge")
        assert judge.service == "Vertex AI · claude-opus-5-5"
        assert judge.region == "us-east5"
        assert JUDGE_SERVICE_PLACEHOLDER not in json.dumps(desc.to_dict())
    other = ArenaSettings(demo_mode="replay", judge_provider="gemini", judge_model="gemini-4-argon-judge")
    judge = next(n for n in get_architecture_descriptor("level1_naive", other).nodes if n.id == "judge")
    assert judge.service == "Vertex AI · gemini-4-argon-judge"
