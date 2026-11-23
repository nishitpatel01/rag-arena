"""Offline TDD tests for v1.6.3 Identity-Aware Document ACLs & Row-Level Security."""

from __future__ import annotations

from pathlib import Path

from app.core.acl import (
    ALL_ACCESS_PERSONA_ID,
    ARCHITECTURE_ACL_CAPABILITIES,
    VALID_ACL_GROUPS,
    AclEnforcementUnsupportedError,
    AclValidationError,
    CallerAccessContext,
    acl_recording_question_id,
    acl_refusal_answer,
    allowed_groups_for_doc,
    corpus_acl_summary,
    is_acl_recording_question_id,
    is_doc_authorized,
    list_acl_personas,
    resolve_caller_access,
)
from app.core.config import ALL_LEVEL_IDS


def test_acl_personas_and_narrowing_invariant() -> None:
    personas = list_acl_personas()
    ids = [p.persona_id for p in personas]
    assert ids == [
        "all_access",
        "engineering_analyst",
        "pricing_finance",
        "security_soc2",
        "legal_counsel",
    ]

    # Default resolves to all_access (unrestricted)
    default_ctx = resolve_caller_access(None)
    assert isinstance(default_ctx, CallerAccessContext)
    assert default_ctx.persona_id == ALL_ACCESS_PERSONA_ID
    assert default_ctx.is_restricted is False
    assert set(default_ctx.effective_groups) == set(VALID_ACL_GROUPS)

    # Strict narrowing: client requesting all_access or pricing_finance cannot widen past ceiling_groups
    narrowed = resolve_caller_access("all_access", ceiling_groups=("corp_all",))
    assert narrowed.effective_groups == ("corp_all",)
    assert narrowed.is_restricted is True

    narrowed_pricing = resolve_caller_access("pricing_finance", ceiling_groups=("corp_all",))
    assert narrowed_pricing.effective_groups == ("corp_all",)
    assert "procurement_pricing" not in narrowed_pricing.effective_groups

    # Unknown / injection persona rejected fail-closed
    for bad in ("admin; DROP TABLE", "unknown_role", "  ", "../etc/passwd"):
        try:
            resolve_caller_access(bad)
            raise AssertionError(f"Expected AclValidationError for {bad!r}")
        except AclValidationError:
            pass


def test_corpus_45_pdf_acl_classification_and_counts() -> None:
    corpus_root = Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
    pdfs = sorted(corpus_root.rglob("*.pdf"))
    assert len(pdfs) == 45

    eng = resolve_caller_access("engineering_analyst")
    fin = resolve_caller_access("pricing_finance")
    sec = resolve_caller_access("security_soc2")
    leg = resolve_caller_access("legal_counsel")
    adm = resolve_caller_access("all_access")

    assert sum(1 for p in pdfs if is_doc_authorized(p.name, adm)) == 45
    assert sum(1 for p in pdfs if is_doc_authorized(p.name, eng)) == 10
    assert sum(1 for p in pdfs if is_doc_authorized(p.name, fin)) == 15
    assert sum(1 for p in pdfs if is_doc_authorized(p.name, sec)) == 12
    assert sum(1 for p in pdfs if is_doc_authorized(p.name, leg)) == 38

    # Spot-check critical security boundaries
    assert is_doc_authorized("03_Technical_Exhibit_Architecture.pdf", eng) is True
    assert is_doc_authorized("04_Pricing_Schedule_CLINs.pdf", eng) is False
    assert is_doc_authorized("06_Amendment_0001_Scope_Update.pdf", eng) is False
    assert is_doc_authorized("04_Compliance_and_SOC2_Matrix.pdf", eng) is False
    assert is_doc_authorized("03_Incident_Response_SLAs.pdf", eng) is False
    assert is_doc_authorized("05_Master_Services_Agreement_MSA.pdf", eng) is False

    assert is_doc_authorized("04_Pricing_Schedule_CLINs.pdf", fin) is True
    assert is_doc_authorized("06_Amendment_0001_Scope_Update.pdf", fin) is True
    assert is_doc_authorized("04_Compliance_and_SOC2_Matrix.pdf", fin) is False
    assert is_doc_authorized("04_Compliance_and_SOC2_Matrix.pdf", sec) is True
    assert is_doc_authorized("03_Incident_Response_SLAs.pdf", sec) is True
    assert is_doc_authorized("05_Master_Services_Agreement_MSA.pdf", leg) is True

    # Every doc grants exec_admin so all_access is always a superset
    for p in pdfs:
        groups = allowed_groups_for_doc(p.name)
        assert "exec_admin" in groups
        assert set(groups).issubset(set(VALID_ACL_GROUPS))

    summary = corpus_acl_summary([p.name for p in pdfs])
    assert summary["total_docs"] == 45
    assert summary["authorized_by_persona"]["all_access"] == 45
    assert summary["authorized_by_persona"]["engineering_analyst"] == 10


def test_architecture_capabilities_and_recording_key_helpers() -> None:
    assert set(ARCHITECTURE_ACL_CAPABILITIES.keys()) == set(ALL_LEVEL_IDS)
    assert ARCHITECTURE_ACL_CAPABILITIES["rag_engine"].supports_chunk_acl is False
    assert ARCHITECTURE_ACL_CAPABILITIES["rag_engine"].enforcement_mode == "corpus_boundary_only"
    for lid in ALL_LEVEL_IDS:
        if lid != "rag_engine":
            assert ARCHITECTURE_ACL_CAPABILITIES[lid].supports_chunk_acl is True

    adm = resolve_caller_access("all_access")
    eng = resolve_caller_access("engineering_analyst")
    assert acl_recording_question_id("q_exact_clin_lookup", None) == "q_exact_clin_lookup"
    assert acl_recording_question_id("q_exact_clin_lookup", adm) == "q_exact_clin_lookup"
    assert acl_recording_question_id("q_exact_clin_lookup", eng) == "q_exact_clin_lookup__acl_engineering_analyst"
    assert is_acl_recording_question_id("q_exact_clin_lookup") is False
    assert is_acl_recording_question_id("q_exact_clin_lookup__acl_engineering_analyst") is True

    refusal = acl_refusal_answer(eng, "What is CLIN-0004-AB rate?")
    assert "Engineering Analyst" in refusal
    assert "corp_all" in refusal
    assert issubclass(AclEnforcementUnsupportedError, RuntimeError)


def test_bigquery_ddl_rls_backfill_and_parameterized_acl_sql() -> None:
    from app.engines.level1_naive import build_vector_search_sql
    from app.ingestion.bq_indexer import (
        build_acl_backfill_sql,
        build_amendment_lookup_sql,
        build_hybrid_rrf_sql,
        build_lexical_search_sql,
        build_row_access_policy_ddl,
        build_tables_ddl,
    )

    ddl = build_tables_ddl("demo_proj", "rag_arena", "document_chunks_v3")
    assert "allowed_groups ARRAY<STRING>" in ddl
    assert "ADD COLUMN IF NOT EXISTS allowed_groups ARRAY<STRING>" in ddl

    rap = build_row_access_policy_ddl(
        "demo_proj",
        "rag_arena",
        "document_chunks_v3",
        policy_name="rag_arena_pricing_policy",
        group_email="procurement-pricing@example.com",
        allowed_group="procurement_pricing",
    )
    assert "CREATE OR REPLACE ROW ACCESS POLICY `rag_arena_pricing_policy`" in rap
    assert "GRANT TO ('group:procurement-pricing@example.com')" in rap
    assert "'procurement_pricing' IN UNNEST(allowed_groups)" in rap

    backfill = build_acl_backfill_sql("demo_proj", "rag_arena", "document_chunks_v3")
    assert "UPDATE `demo_proj.rag_arena.document_chunks_v3`" in backfill
    assert "'procurement_pricing'" in backfill
    assert "'security_soc2'" in backfill
    assert "'legal_contracts'" in backfill
    assert "'corp_all'" in backfill

    # Default SQL (include_acl_filter=False) remains unchanged — no @caller_groups
    assert "@caller_groups" not in build_vector_search_sql("demo_proj", "rag_arena", "document_chunks_v3")
    assert "@caller_groups" not in build_hybrid_rrf_sql("demo_proj", "rag_arena", "document_chunks_v3")
    assert "@caller_groups" not in build_lexical_search_sql("demo_proj", "rag_arena", "document_chunks_v3")
    assert "@caller_groups" not in build_amendment_lookup_sql("demo_proj", "rag_arena", "document_chunks_v3")

    # Restricted SQL uses parameterized UNNEST(@caller_groups) with zero string interpolation
    vec_acl = build_vector_search_sql("demo_proj", "rag_arena", "document_chunks_v3", include_acl_filter=True)
    hyb_acl = build_hybrid_rrf_sql("demo_proj", "rag_arena", "document_chunks_v3", include_acl_filter=True)
    lex_acl = build_lexical_search_sql("demo_proj", "rag_arena", "document_chunks_v3", include_acl_filter=True)
    amd_acl = build_amendment_lookup_sql("demo_proj", "rag_arena", "document_chunks_v3", include_acl_filter=True)
    for sql in (vec_acl, hyb_acl, lex_acl, amd_acl):
        assert "UNNEST(allowed_groups)" in sql
        assert "IN UNNEST(@caller_groups)" in sql


def test_alloydb_vvs2_vais_and_terraform_acl_builders() -> None:
    from app.core.vvs2_client import batch_search_body, collection_body, data_object_request, dense_scann_index_body
    from app.engines.alloydb_scann import build_alloydb_hybrid_search_sql, build_alloydb_scann_ddl
    from app.engines.vais_search import build_vais_acl_filter

    # AlloyDB: allowed_groups TEXT[], GIN index, PostgreSQL RLS policy, and parameterized overlap
    ddl_stmts = "\n".join(build_alloydb_scann_ddl(table="document_chunks_scann"))
    assert "allowed_groups TEXT[]" in ddl_stmts
    assert "ENABLE ROW LEVEL SECURITY" in ddl_stmts
    assert "CREATE POLICY" in ddl_stmts
    assert "app.caller_groups" in ddl_stmts

    sql_unrestricted, params_unrestricted = build_alloydb_hybrid_search_sql(
        table="document_chunks_scann", query_vector=[0.1, 0.2], query_text="CLIN-0004-AB"
    )
    assert "caller_groups" not in sql_unrestricted
    assert "caller_groups" not in params_unrestricted

    sql_rls, params_rls = build_alloydb_hybrid_search_sql(
        table="document_chunks_scann",
        query_vector=[0.1, 0.2],
        query_text="CLIN-0004-AB",
        caller_groups=("corp_all",),
    )
    assert "allowed_groups && %(caller_groups)s::text[]" in sql_rls
    assert params_rls["caller_groups"] == ["corp_all"]

    # Vector Search 2.0: schema, filterFields, dataObject, and $in pre-filter on both legs
    col = collection_body(display_name="test-col")
    assert "allowed_groups" in col["dataSchema"]["properties"]
    idx = dense_scann_index_body(display_name="test-idx")
    assert "allowed_groups" in idx["filterFields"]
    obj = data_object_request(
        collection_name="projects/p/locations/us-central1/collections/c",
        chunk_row={"chunk_id": "c1", "content": "pricing rate", "doc_name": "04_Pricing_Schedule_CLINs.pdf"},
        dense_embedding=[0.1, 0.2],
    )
    assert "procurement_pricing" in obj["dataObject"]["data"]["allowed_groups"]
    body = batch_search_body(
        query_vector=[0.1, 0.2],
        query_text="CLIN pricing",
        top_k=10,
        solicitation_filter="solicitation_01",
        caller_groups=("corp_all",),
    )
    for leg in body["searches"]:
        flt = leg["vectorSearch"]["filter"]
        assert "$and" in flt
        assert {"allowed_groups": {"$in": ["corp_all"]}} in flt["$and"]

    # Vertex AI Search: allow-listed filter expression, rejecting injected tokens
    expr = build_vais_acl_filter(("corp_all", "security_soc2"), workspace_id="upl_abcdef012345")
    assert expr == 'workspace_id: ANY("upl_abcdef012345") AND allowed_groups: ANY("corp_all", "security_soc2")'
    try:
        build_vais_acl_filter(('corp_all") OR true --',))
        raise AssertionError("Expected AclValidationError for injected VAIS ACL group")
    except AclValidationError:
        pass

    # Terraform parity: document_chunks_v3 declares REPEATED STRING allowed_groups
    tf_text = (Path(__file__).resolve().parents[2] / "infra" / "main.tf").read_text(encoding="utf-8")
    assert 'name = "allowed_groups"' in tf_text
    assert 'mode = "REPEATED"' in tf_text


def test_engines_adk_rag_fail_closed_and_replay_isolation() -> None:
    import asyncio
    import tempfile
    from unittest.mock import patch

    from app.core.config import ArenaSettings
    from app.core.models import EngineFlags, EngineResult, TokenUsage
    from app.core.telemetry import NullStageEmitter
    from app.engines.agent_ledger import AgentLedger
    from app.engines.agent_tools import ToolDeps, run_check_amendments
    from app.engines.level2_hybrid import Level2HybridEngine
    from app.engines.rag_engine import RagEngine
    from app.engines.replay_engine import ReplayRAGEngine, ReplayUnavailableError
    from app.recording.recorder import build_recording
    from app.recording.store import RecordingStore

    eng_access = resolve_caller_access("engineering_analyst")
    live_settings = ArenaSettings(
        demo_mode="live",
        gcp_project_id="demo_proj",
        bq_dataset="rag_arena",
        bq_table="document_chunks_v3",
        rag_engine_location="us-central1",
        rag_engine_corpus_id="12345",
    )

    # 1. Level 2 Hybrid: 0 authorized rows under restricted persona -> deterministic refusal (no crash, no LLM call)
    captured_params: list[tuple[str, list[str]]] = []

    class _FakeJob:
        job_id = "job_acl_zero"
        total_bytes_processed = 1024
        slot_millis = 12
        cache_hit = False
        _properties: dict = {}

        def result(self, timeout: float = 120) -> list:
            return []

    class _FakeBQ:
        def query(self, sql: str, job_config=None):
            params = getattr(job_config, "query_parameters", []) or []
            for p in params:
                if getattr(p, "name", "") == "caller_groups":
                    captured_params.append((sql, list(p.values)))
            return _FakeJob()

    class _FakeEmbedResp:
        class _Emb:
            values = [0.1, 0.2, 0.3]

        embeddings = [_Emb()]

    class _FakeGenAI:
        class models:
            @staticmethod
            def embed_content(**kwargs):
                return _FakeEmbedResp()

    async def _run_hybrid_zero() -> EngineResult:
        engine = Level2HybridEngine(settings=live_settings)
        emitter = NullStageEmitter(run_id="run_acl_test", engine="level2_hybrid")
        with patch("app.engines.level2_hybrid.get_genai_client", return_value=_FakeGenAI()), patch(
            "app.engines.level2_hybrid.get_bigquery_client", return_value=_FakeBQ()
        ):
            return await engine.run_query(
                "What is the amended CLIN-0004-AB rate?",
                question_id="q_amendment_rate_comparison",
                solicitation_filter=None,
                emitter=emitter,
                caller_access=eng_access,
            )

    res = asyncio.run(_run_hybrid_zero())
    assert res.citations == []
    assert res.retrieved_chunks == []
    assert "Access restricted" in res.answer
    assert "Engineering Analyst" in res.answer
    assert len(captured_params) == 1
    assert "@caller_groups" in captured_params[0][0]
    assert captured_params[0][1] == ["corp_all"]

    # 2. ADK check_amendments closure-pins @caller_groups from ToolDeps
    captured_params.clear()
    deps = ToolDeps(
        settings=live_settings,
        retriever=None,
        genai_client=None,
        bq_client=_FakeBQ(),
        ledger=AgentLedger(run_id="run_adk_acl"),
        caller_access=eng_access,
    )
    out = asyncio.run(run_check_amendments(deps, "solicitation_01", "CLIN-0004-AB"))
    assert out.response["has_amendment"] is False
    assert len(captured_params) == 1
    assert "@caller_groups" in captured_params[0][0]
    assert captured_params[0][1] == ["corp_all"]

    # 3. RagEngine fails closed when restricted persona is requested (honest corpus-boundary)
    async def _run_rag_restricted() -> None:
        rag = RagEngine(settings=live_settings)
        emitter = NullStageEmitter(run_id="run_rag_acl", engine="rag_engine")
        await rag.run_query(
            "What is the amended CLIN-0004-AB rate?",
            question_id="q_amendment_rate_comparison",
            solicitation_filter=None,
            emitter=emitter,
            caller_access=eng_access,
        )

    try:
        asyncio.run(_run_rag_restricted())
        raise AssertionError("Expected AclEnforcementUnsupportedError from RagEngine under restricted persona")
    except AclEnforcementUnsupportedError:
        pass

    # 4. RecordingStore + ReplayRAGEngine isolate __acl_* recordings from baseline leaderboard
    with tempfile.TemporaryDirectory() as tmp:
        store = RecordingStore(Path(tmp))
        rec_settings = ArenaSettings(
            demo_mode="record",
            gcp_project_id="demo_proj",
            recordings_dir=tmp,
            gemini_fast_model="gemini-4-hydrogen",
            judge_model="claude-opus-5-5",
        )
        dummy_res = EngineResult(
            engine_id="level2_hybrid",
            engine_label="Level 4: BigQuery Hybrid + Reranker",
            answer="Full pricing answer",
            citations=[],
            retrieved_chunks=[],
            agent_steps=[],
            latency_ms=100,
            cost_usd=0.01,
            stage_log=(),
            usage=TokenUsage(model="gemini-4-hydrogen"),
            flags=EngineFlags(),
        )
        base_rec = build_recording(
            settings=rec_settings,
            engine_id="level2_hybrid",
            question_id="q_amendment_rate_comparison",
            query="q",
            run_id="r1",
            events=(),
            result=dummy_res,
            evaluation=None,
        )
        acl_rec = build_recording(
            settings=rec_settings,
            engine_id="level2_hybrid",
            question_id=acl_recording_question_id("q_amendment_rate_comparison", eng_access),
            query="q",
            run_id="r2",
            events=(),
            result=EngineResult(
                engine_id="level2_hybrid",
                engine_label="Level 4: BigQuery Hybrid + Reranker",
                answer=acl_refusal_answer(eng_access, "q"),
                citations=[],
                retrieved_chunks=[],
                agent_steps=[],
                latency_ms=80,
                cost_usd=0.0,
                stage_log=(),
                usage=TokenUsage(model="gemini-4-hydrogen"),
                flags=EngineFlags(),
            ),
            evaluation=None,
            caller_access=eng_access,
        )
        store.save(base_rec)
        store.save(acl_rec)

        # Default list_recordings excludes __acl_* so baseline leaderboard stays untouched
        assert len(store.list_recordings()) == 1
        assert len(store.list_recordings(include_acl=True)) == 2

        # Replay with restricted persona loads ACL recording; missing persona raises ReplayUnavailableError
        replay_settings = ArenaSettings(
            demo_mode="replay",
            recordings_dir=tmp,
            gemini_fast_model="gemini-4-hydrogen",
            judge_model="claude-opus-5-5",
            replay_speed=100.0,
        )
        replay = ReplayRAGEngine(
            engine_id="level2_hybrid",
            engine_label="Level 4: BigQuery Hybrid + Reranker",
            stages=("query_prep", "embed_query", "retrieve", "rerank", "synthesize", "evaluate"),
            tier_profile="hybrid",
            settings=replay_settings,
            store=store,
        )
        r_eng = asyncio.run(
            replay.run_query(
                "q",
                question_id="q_amendment_rate_comparison",
                solicitation_filter=None,
                emitter=NullStageEmitter(run_id="rp1", engine="level2_hybrid"),
                caller_access=eng_access,
            )
        )
        assert "Access restricted" in r_eng.answer

        fin_access = resolve_caller_access("pricing_finance")
        try:
            asyncio.run(
                replay.run_query(
                    "q",
                    question_id="q_amendment_rate_comparison",
                    solicitation_filter=None,
                    emitter=NullStageEmitter(run_id="rp2", engine="level2_hybrid"),
                    caller_access=fin_access,
                )
            )
            raise AssertionError("Expected ReplayUnavailableError for unrecorded pricing_finance persona")
        except ReplayUnavailableError:
            pass


def test_api_acl_personas_document_403_and_tco_strict_acls() -> None:
    from starlette.testclient import TestClient

    from app.core.config import ArenaSettings
    from app.core.tco import COMPLIANCE_POSTURE_OPTIONS, ENTERPRISE_SCENARIO_PRESETS, TcoWorkload, estimate_enterprise_tco
    from app.main import create_app

    app = create_app(settings=ArenaSettings(demo_mode="replay"))
    client = TestClient(app)

    # 1. GET /api/v1/acl/personas
    resp = client.get("/api/v1/acl/personas")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["personas"]) == 5
    assert body["corpus_summary"]["total_docs"] == 45
    assert body["architecture_capabilities"]["rag_engine"]["supports_chunk_acl"] is False
    assert body["architecture_capabilities"]["level2_hybrid"]["supports_chunk_acl"] is True
    assert len(body["recorded_acl_demos"]) >= 6
    assert all(
        {"engine", "synthesis_model", "judge_model", "question_id", "access_persona"} <= set(d.keys())
        for d in body["recorded_acl_demos"]
    )

    # 2. Invalid access_persona on /api/v1/battle/stream -> 400
    bad_battle = client.post(
        "/api/v1/battle/stream",
        json={"query": "What is CLIN-0004-AB?", "engines": ["level2_hybrid"], "access_persona": "root; DROP"},
    )
    assert bad_battle.status_code == 400

    # 3. Document Viewer enforces ACLs server-side (403 on restricted pricing PDF, 200 on technical exhibit)
    denied_page = client.get(
        "/api/v1/documents/page",
        params={
            "solicitation_id": "solicitation_01",
            "doc_name": "04_Pricing_Schedule_CLINs.pdf",
            "page": "1",
            "persona": "engineering_analyst",
        },
    )
    assert denied_page.status_code == 403
    assert denied_page.json().get("code") == "acl_forbidden"

    denied_hl = client.get(
        "/api/v1/documents/highlights",
        params={
            "solicitation_id": "solicitation_01",
            "doc_name": "04_Pricing_Schedule_CLINs.pdf",
            "page": "1",
            "text": "CLIN-0004-AB",
            "persona": "engineering_analyst",
        },
    )
    assert denied_hl.status_code == 403

    allowed_page = client.get(
        "/api/v1/documents/page",
        params={
            "solicitation_id": "solicitation_01",
            "doc_name": "03_Technical_Exhibit_Architecture.pdf",
            "page": "1",
            "persona": "engineering_analyst",
        },
    )
    assert allowed_page.status_code == 200
    assert allowed_page.headers["content-type"] == "image/png"

    # 4. TCO Advisor supports strict_document_acls and flags RagEngine corpus boundary honestly
    assert "strict_document_acls" in COMPLIANCE_POSTURE_OPTIONS
    assert any(p.preset_id == "enterprise_document_acls" for p in ENTERPRISE_SCENARIO_PRESETS)
    est = estimate_enterprise_tco(
        TcoWorkload(
            data_gravity="bigquery_warehouse",
            monthly_queries=25_000,
            corpus_pages=5_000,
            latency_sla="interactive_rag",
            compliance_posture="strict_document_acls",
            doc_complexity="plain_text",
            synthesis_model="gemini-4-hydrogen",
        )
    )
    by_id = {row.level_id: row for row in est.architectures}
    assert by_id["level2_hybrid"].fit_score > by_id["rag_engine"].fit_score
    assert any("RagCorpus" in t or "corpus" in t.lower() for t in by_id["rag_engine"].tradeoffs)


def test_adk_restricted_acl_abstention_and_replay_rag_engine_fail_closed() -> None:
    """ADK accepts honest empty-citation abstention under restricted ACLs; Replay rag_engine fails closed."""
    import asyncio
    from app.core.config import ArenaSettings
    from app.core.telemetry import NullStageEmitter
    from app.engines.agent_ledger import AgentLedger
    from app.engines.agent_runtime import AgentProtocolError
    from app.engines.level4_agentic import resolve_citations
    from app.engines.replay_engine import ReplayRAGEngine

    ledger = AgentLedger(run_id="run_acl_abstain")
    abstention = "The authorized context does not contain the SOC2 P1 incident response matrix.\nCITED_CHUNK_IDS: none"

    # Unrestricted runs still enforce at least one valid tool citation
    try:
        resolve_citations(abstention, ledger)
    except AgentProtocolError:
        pass
    else:
        raise AssertionError("unrestricted ADK run must reject CITED_CHUNK_IDS: none")

    # Restricted ACL runs allow honest zero-citation abstention
    resolved = resolve_citations(abstention, ledger, allow_empty_abstention=True)
    assert resolved.chunks == ()
    assert resolved.source == "acl_abstention"
    assert "does not contain" in resolved.answer

    # Replay rag_engine fails closed on restricted personas (honest corpus-boundary)
    eng_access = resolve_caller_access("engineering_analyst")
    replay_rag = ReplayRAGEngine(
        engine_id="rag_engine",
        engine_label="Level 2: Vertex AI RAG Engine",
        stages=("query_prep", "retrieve", "synthesize", "evaluate"),
        tier_profile="rag_engine",
        settings=ArenaSettings(demo_mode="replay"),
    )
    emitter = NullStageEmitter(run_id="run_replay_rag_acl", engine="rag_engine")
    try:
        asyncio.run(
            replay_rag.run_query(
                "What is the amended CLIN-0004-AB rate?",
                question_id="q_amendment_rate_comparison",
                solicitation_filter=None,
                emitter=emitter,
                caller_access=eng_access,
            )
        )
    except AclEnforcementUnsupportedError as exc:
        assert "RagCorpus" in str(exc)
    else:
        raise AssertionError("ReplayRAGEngine(rag_engine) must fail closed on restricted personas")



