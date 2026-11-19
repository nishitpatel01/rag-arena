"""v1.6.2 Option A + Option D backend & deploy TDD suite."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch

import httpx
import pytest

from app.core.models import BoundingBox, Citation, DocumentChunk, EngineFlags, EngineResult, EvalMetrics, EvalOutcome, TokenUsage
from app.core.models_catalog import load_catalog
from app.core.ranking import RankingClient
from app.core.telemetry import RecordingStageEmitter
from app.engines.agent_runtime import AGENT_INSTRUCTION, agent_instruction, agent_instruction_for_tools
from app.engines.level2_hybrid import HybridCandidate
from app.engines.level3_multimodal import Level3MultimodalEngine, dedupe_heading_stubs
from app.ingestion.layout_documentai import LayoutElement
from app.ingestion.multimodal import group_page_elements
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run
from test_cloudrun_deploy import _run_deploy

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_group_page_elements_eliminates_orphan_headings_before_tables_and_subheadings() -> None:
    elements = [
        LayoutElement(block_id="b1", kind="heading", text="EXHIBIT B: SOC INCIDENT RESPONSE SEVERITY MATRIX", page_number=1, heading="EXHIBIT B: SOC INCIDENT RESPONSE SEVERITY MATRIX"),
        LayoutElement(block_id="b2", kind="heading", text="Clause SLA-SEC-3.1: Mandatory 24/7 SOC Triage Matrix:", page_number=1, heading="Clause SLA-SEC-3.1: Mandatory 24/7 SOC Triage Matrix:"),
        LayoutElement(
            block_id="b3",
            kind="table",
            text="| Severity | Triage |\n| --- | --- |\n| P1 | 15m |",
            page_number=1,
            heading="",
            rows=1,
            cols=2,
        ),
        LayoutElement(block_id="b4", kind="heading", text="Section 4: Escalation Contacts", page_number=1, heading="Section 4: Escalation Contacts"),
        LayoutElement(block_id="b5", kind="heading", text="4.1 Primary Duty Officer", page_number=1, heading="4.1 Primary Duty Officer"),
        LayoutElement(block_id="b6", kind="paragraph", text="Notify the SOC duty officer within 15 minutes of detection.", page_number=1, heading="4.1 Primary Duty Officer"),
    ]
    groups = group_page_elements(elements)
    assert [g.kind for g in groups] == ["table", "narrative_text"]
    assert "EXHIBIT B" in groups[0].heading and "SLA-SEC-3.1" in groups[0].heading
    assert len(groups[1].texts) == 3
    assert groups[1].texts[0].startswith("Section 4:")
    assert "Notify the SOC duty officer" in groups[1].texts[-1]


def test_group_page_elements_preserves_heading_only_cover_page_when_no_table_follows() -> None:
    cover = [
        LayoutElement(block_id="c1", kind="heading", text="ENTERPRISE CLOUD SOLICITATION", page_number=1, heading="ENTERPRISE CLOUD SOLICITATION"),
        LayoutElement(block_id="c2", kind="heading", text="VOLUME I — TECHNICAL PROPOSAL", page_number=1, heading="VOLUME I — TECHNICAL PROPOSAL"),
    ]
    groups = group_page_elements(cover)
    assert len(groups) == 1
    assert groups[0].kind == "narrative_text"
    assert len(groups[0].texts) == 2


def _cand(cid: str, doc: str, page: int, kind: str, content: str, rank: int) -> HybridCandidate:
    chunk = DocumentChunk(
        chunk_id=cid,
        tier_profile="multimodal",
        solicitation_id="solicitation_03",
        doc_name=doc,
        doc_type="SLA_REQUIREMENT",
        element_type=kind,
        section_id="SLA-SEC-3.1",
        amendment_number=0,
        effective_date="2026-01-15",
        amends_section=None,
        page_number=page,
        bbox=BoundingBox(page=page, x0=0.0, y0=0.0, x1=1.0, y1=1.0),
        content=content,
        score=0.03,
        layout_source="documentai_layout",
        bbox_source="page",
    )
    return HybridCandidate(chunk=chunk, rrf_rank=rank, rrf_score=round(1.0 / (60 + rank), 6), vector_rank=rank, vector_distance=0.1, lexical_rank=rank, lexical_score=2.0)


def test_dedupe_heading_stubs_drops_same_page_redundant_stubs_and_keeps_real_prose() -> None:
    cands = [
        _cand("c1", "03_Incident_Response_SLAs.pdf", 1, "narrative_text", "EXHIBIT B: SOC INCIDENT RESPONSE SEVERITY MATRIX & PENALTY CREDITS", 1),
        _cand("c2", "03_Incident_Response_SLAs.pdf", 1, "narrative_text", "Clause SLA-SEC-3.1: Mandatory 24/7 SOC Triage, Containment & Penalty Matrix:", 2),
        _cand(
            "c3",
            "03_Incident_Response_SLAs.pdf",
            1,
            "table",
            "EXHIBIT B: SOC INCIDENT RESPONSE SEVERITY MATRIX & PENALTY CREDITS\nClause SLA-SEC-3.1: Mandatory 24/7 SOC Triage, Containment & Penalty Matrix:\n| Severity | Triage |\n| P1 | <= 15m |",
            3,
        ),
        # Clause heading NOT present in table text above, followed on same page by table that only has subheading:
        _cand("c4", "04_Compliance.pdf", 1, "narrative_text", "EXHIBIT B: SOC2 TYPE II, HIPAA & VPC SERVICE CONTROLS COMPLIANCE MATRIX", 4),
        _cand("c5", "04_Compliance.pdf", 1, "table", "EXHIBIT B: SOC2 TYPE II, HIPAA & VPC SERVICE CONTROLS COMPLIANCE MATRIX\n| Control | Mandatory |\n| CC6.1 | YES |", 5),
        # Genuine short standalone note not contained in any sibling on page 2:
        _cand("c6", "04_Compliance.pdf", 2, "narrative_text", "All controls audited semi-annually.", 6),
    ]
    kept, dropped = dedupe_heading_stubs(cands)
    assert [c.chunk.chunk_id for c in kept] == ["c3", "c5", "c6"]
    assert dropped == 3


def test_level3_engine_dedupes_heading_stubs_before_rerank_and_reports_count() -> None:
    rows = [
        {
            "chunk_id": "stub_1",
            "solicitation_id": "solicitation_03",
            "doc_name": "03_SLAs.pdf",
            "doc_type": "SLA",
            "element_type": "narrative_text",
            "section_id": "SLA-3.1",
            "amendment_number": 0,
            "effective_date": "2026-01-15",
            "amends_section": None,
            "page_number": 1,
            "bbox": {"page": 1, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
            "content": "Clause SLA-3.1 Matrix:",
            "layout_source": "documentai_layout",
            "bbox_source": "page",
            "vector_rank": 1,
            "vector_distance": 0.1,
            "lexical_rank": 1,
            "lexical_score": 2,
            "rrf_score": 0.032,
        },
        {
            "chunk_id": "tbl_1",
            "solicitation_id": "solicitation_03",
            "doc_name": "03_SLAs.pdf",
            "doc_type": "SLA",
            "element_type": "table",
            "section_id": "SLA-3.1",
            "amendment_number": 0,
            "effective_date": "2026-01-15",
            "amends_section": None,
            "page_number": 1,
            "bbox": {"page": 1, "x0": 0.0, "y0": 0.1, "x1": 1.0, "y1": 0.5},
            "content": "Clause SLA-3.1 Matrix:\n| Severity | Triage |\n| P1 | 15m |",
            "layout_source": "documentai_layout",
            "bbox_source": "pdf_text_layer",
            "vector_rank": 2,
            "vector_distance": 0.12,
            "lexical_rank": 2,
            "lexical_score": 2,
            "rrf_score": 0.031,
        },
    ]
    seen_rerank_ids: list[str] = []

    def rank_post(url: str, **kw):
        ids = [r["id"] for r in kw["json"]["records"]]
        seen_rerank_ids.extend(ids)
        return httpx.Response(200, json={"records": [{"id": rid, "score": 0.9} for rid in ids]}, request=httpx.Request("POST", url))

    settings = live_settings(enabled_levels=("level3_multimodal",))
    spec = load_catalog(settings).get("gemini-4-hydrogen")
    ranker = RankingClient(project_id="test-project", model="semantic-ranker-default@latest", attempt_timeout_seconds=5.0, budget_seconds=10.0, token_provider=lambda: "tok", sleep=lambda s: None, post=rank_post)
    emitter = RecordingStageEmitter(run_id="r_l3", engine="level3_multimodal")
    genai, bq = FakeGenaiClient(), FakeBigQueryClient(rows=rows)
    with fake_clients(genai, bq), patch("app.engines.level3_multimodal.get_genai_client", lambda s: genai), patch("app.engines.level3_multimodal.get_bigquery_client", lambda s: bq):
        eng = Level3MultimodalEngine(settings=settings, synthesis_model=spec, ranking_client=ranker)
        res = run(eng.run_query("What is the P1 triage SLA?", question_id="q", solicitation_filter=None, emitter=emitter))
    assert seen_rerank_ids == ["tbl_1"]
    retrieve_stage = next(s for s in res.stage_log if s.stage == "retrieve")
    assert retrieve_stage.payload["deduped_heading_stubs"] == 1


def test_agent_instruction_has_authoritative_negative_lookup_exit_rule() -> None:
    full = agent_instruction(8)
    partial = agent_instruction_for_tools(8, ("search_procurement_corpus", "check_amendments", "verify_citations"))
    for prompt in (AGENT_INSTRUCTION, full, partial):
        lower = prompt.lower()
        assert "authoritative" in lower
        assert "no amendments" in lower or "zero amendments" in lower
        assert "do not" in lower and "search_procurement_corpus" in prompt


def test_deploy_cloudrun_and_terraform_bind_model_armor_user_when_configured() -> None:
    proc_with = _run_deploy({"MODEL_ARMOR_TEMPLATE_ID": "rag-arena-guardrail", "MODEL_ARMOR_LOCATION": "us-central1"}, "--dry-run")
    assert proc_with.returncode == 0, proc_with.stderr
    lines_with = proc_with.stdout.splitlines()
    assert any("enable-apis" in ln and "modelarmor.googleapis.com" in ln for ln in lines_with)
    assert "PLAN iam project roles/modelarmor.user -> rag-arena-run" in lines_with
    assert "ENV live MODEL_ARMOR_TEMPLATE_ID=rag-arena-guardrail" in lines_with

    proc_without = _run_deploy({}, "--dry-run")
    assert proc_without.returncode == 0, proc_without.stderr
    lines_without = proc_without.stdout.splitlines()
    assert "PLAN iam project roles/modelarmor.user -> rag-arena-run" not in lines_without

    tf = (PROJECT_ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    assert 'variable "model_armor_template_id"' in tf
    assert 'variable "model_armor_location"' in tf
    assert '"modelarmor.googleapis.com"' in tf
    assert "roles/modelarmor.user" in tf
    assert "MODEL_ARMOR_TEMPLATE_ID" in tf


def test_record_live_benchmarks_resumes_enforces_spend_cap_and_redacts() -> None:
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import record_live_benchmarks as rlb

    with tempfile.TemporaryDirectory() as tmp:
        rec_dir = Path(tmp) / "recordings"
        rec_dir.mkdir()
        bench = Path(tmp) / "bench.json"
        bench.write_text(
            json.dumps({"questions": [{"question_id": "q1", "question": "What is CLIN 1?"}, {"question_id": "q2", "question": "What is CLIN 2?"}]}),
            encoding="utf-8",
        )
        settings = live_settings(demo_mode="record", recordings_dir=str(rec_dir), enabled_levels=("level1_naive",))

        calls: list[str] = []

        class _StubEngine:
            async def run_query(self, q: str, *, question_id: str | None, solicitation_filter: str | None, emitter):
                calls.append(str(question_id))
                chunk = DocumentChunk(
                    chunk_id="c1", tier_profile="naive", solicitation_id="solicitation_01", doc_name="d.pdf", doc_type="RFP",
                    element_type="narrative_text", section_id="S1", amendment_number=0, effective_date="2026-01-01", amends_section=None,
                    page_number=1, bbox=BoundingBox(page=1, x0=0.0, y0=0.0, x1=1.0, y1=1.0), content="CLIN 1 is $10.", score=0.9,
                )
                return EngineResult(
                    engine_id="level1_naive", engine_label="L3", answer="CLIN 1 is $10 in test-project.",
                    citations=[
                        Citation(
                            chunk_id="c1",
                            doc_name="d.pdf",
                            solicitation_id="solicitation_01",
                            page_number=1,
                            section_id="S1",
                            element_type="narrative_text",
                            bbox=chunk.bbox,
                            excerpt="CLIN 1 is $10.",
                        )
                    ],
                    retrieved_chunks=[chunk], agent_steps=[], latency_ms=100, cost_usd=0.01, stage_log=(), usage=TokenUsage(), flags=EngineFlags(),
                )

        async def _stub_eval(q, result, *, question_id, settings, emitter, models, solicitation_filter=None):
            return EvalOutcome(
                metrics=EvalMetrics(groundedness=1.0, qa_relevance=1.0, context_precision=0.9),
                judge_model=models.judge.id,
                reference_answer_used=True,
                duration_ms=50,
                judge_provider=models.judge.provider,
                judge_cost_usd=0.02,
                rationale="ok",
            )

        out1 = run(
            rlb.record_live_cohort(
                settings=settings,
                engines=("level1_naive",),
                synthesis_id="gemini-4-hydrogen",
                judge_id="claude-opus-5-5",
                benchmark_path=bench,
                max_spend_usd=1.0,
                resume=True,
                write_query_runs=False,
                engine_builder=lambda eid, s, m: _StubEngine(),
                evaluator=_stub_eval,
            )
        )
        assert out1["recorded"] == 2 and out1["skipped_existing"] == 0
        f1 = rec_dir / "level1_naive" / "gemini-4-hydrogen__claude-opus-5-5" / "q1.json"
        assert f1.exists() and stat.S_IMODE(f1.stat().st_mode) == 0o644
        assert "test-project" not in f1.read_text(encoding="utf-8")

        # Second pass with resume=True skips both existing recordings without calling the engine
        out2 = run(
            rlb.record_live_cohort(
                settings=settings,
                engines=("level1_naive",),
                synthesis_id="gemini-4-hydrogen",
                judge_id="claude-opus-5-5",
                benchmark_path=bench,
                max_spend_usd=1.0,
                resume=True,
                write_query_runs=False,
                engine_builder=lambda eid, s, m: _StubEngine(),
                evaluator=_stub_eval,
            )
        )
        assert out2["recorded"] == 0 and out2["skipped_existing"] == 2 and calls == ["q1", "q2"]

        # Spend cap check fails loud when overwrite=True (resume=False) exceeds max_spend_usd
        with pytest.raises(RuntimeError, match="max_spend_usd"):
            run(
                rlb.record_live_cohort(
                    settings=settings,
                    engines=("level1_naive",),
                    synthesis_id="gemini-4-hydrogen",
                    judge_id="claude-opus-5-5",
                    benchmark_path=bench,
                    max_spend_usd=0.02,
                    resume=False,
                    write_query_runs=False,
                    engine_builder=lambda eid, s, m: _StubEngine(),
                    evaluator=_stub_eval,
                )
            )
