"""Level 2 hybrid engine + Ranking API client (offline, mocked SDK/REST): stage emission, RRF SQL shape, rerank
REST contract, fail-loud rerank, acronym expansion, model selection, hybrid tier replace, architecture coverage."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.architectures import get_architecture_descriptor
from app.core.models import EngineFlags
from app.core.models_catalog import load_catalog
from app.core.pricing import cost_from_usage, rank_billed_counts, rank_cost_usd
from app.core.ranking import (
    MAX_RECORD_CONTENT_CHARS,
    RankingClient,
    RankingHTTPError,
    RankingResponseError,
    make_record,
    ranking_url,
)
from app.core.telemetry import RecordingStageEmitter
from app.engines import build_engine
from app.engines.level2_hybrid import ACRONYM_EXPANSIONS, Level2HybridEngine, _index_usage, prepare_query
from app.ingestion import live_backends as lb
from app.ingestion.bq_indexer import LEXICAL_STOPWORDS, build_hybrid_rrf_sql, build_lexical_terms
from app.ingestion.parsers import _infer_doc_type_from_filename, chunk_pages
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run

_QUERY = "What is the hourly rate for CLIN-0004-AB under the SLA?"


def _hybrid_row(idx: int, *, vector_rank: int | None, lexical_rank: int | None, rrf: float) -> dict[str, Any]:
    return {
        "chunk_id": f"solicitation_01_doc{idx}_p{idx}_hybrid", "solicitation_id": "solicitation_01",
        "doc_name": f"0{idx}_Doc.pdf", "doc_type": "PRICING_CLIN", "element_type": "table",
        "section_id": f"CLIN-000{idx}-AB", "amendment_number": 0, "effective_date": "2026-01-15",
        "amends_section": None, "page_number": idx, "bbox": {"page": idx, "x0": 0.06, "y0": 0.14, "x1": 0.94, "y1": 0.86},
        "content": f"[Contextual Lineage: Solicitation=solicitation_01 | Document=0{idx}_Doc.pdf] CLIN-000{idx}-AB rate ${200 + idx}.00/hr",
        "vector_rank": vector_rank, "lexical_rank": lexical_rank,
        "vector_distance": None if vector_rank is None else 0.1 * vector_rank, "lexical_score": None if lexical_rank is None else 4,
        "rrf_score": rrf,
    }


_ROWS = [
    _hybrid_row(1, vector_rank=1, lexical_rank=2, rrf=1 / 61 + 1 / 62),
    _hybrid_row(2, vector_rank=None, lexical_rank=1, rrf=1 / 61),
    _hybrid_row(3, vector_rank=2, lexical_rank=None, rrf=1 / 62),
]


class FakeRankPost:
    """Stands in for ``httpx.post`` against ``rankingConfigs.rank``; scores = reversed RRF order unless given."""

    def __init__(self, statuses: list[int] | None = None, scores: dict[str, float] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._statuses = list(statuses or [])
        self._scores = scores

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        request = httpx.Request("POST", url)
        status = self._statuses.pop(0) if self._statuses else 200
        if status != 200:
            body = {"error": {"code": status, "message": "Permission denied on projects/test-project", "status": "FAKE"}}
            return httpx.Response(status, text=json.dumps(body), request=request)
        ids = [r["id"] for r in kwargs["json"]["records"]]
        scores = self._scores or {rid: round(0.2 + 0.3 * i, 2) for i, rid in enumerate(ids)}
        scored = [rid for rid in ids if rid in scores]  # a partial `scores` map simulates a short API answer
        ranked = sorted(scored, key=lambda rid: scores[rid], reverse=True)[: kwargs["json"]["topN"]]
        return httpx.Response(200, json={"records": [{"id": rid, "score": scores[rid]} for rid in ranked]}, request=request)


def _client(post: FakeRankPost, sleeps: list[float] | None = None) -> RankingClient:
    return RankingClient(
        project_id="test-project", model="semantic-ranker-default@latest", attempt_timeout_seconds=5.0,
        budget_seconds=20.0, token_provider=lambda: "fake-token", sleep=(sleeps if sleeps is not None else []).append, post=post,
    )


def _run_level2(post: FakeRankPost | None = None, synthesis: str | None = None, rows: list[dict[str, Any]] | None = None):
    settings = live_settings(enabled_levels=("level1_naive", "level2_hybrid"))
    emitter = RecordingStageEmitter(run_id="run_l2", engine="level2_hybrid")
    rank_post = post or FakeRankPost()
    spec = load_catalog(settings).get(synthesis) if synthesis else None
    with fake_clients(FakeGenaiClient(), FakeBigQueryClient(rows=_ROWS if rows is None else rows)) as (genai, bq):
        engine = Level2HybridEngine(settings=settings, synthesis_model=spec, ranking_client=_client(rank_post))
        coro = engine.run_query(_QUERY, question_id="q_test", solicitation_filter=None, emitter=emitter)
        try:
            result = run(coro)
        except Exception as exc:  # returned to the caller for fail-loud assertions
            return exc, emitter, genai, bq, rank_post
    return result, emitter, genai, bq, rank_post


# ---------------------------------------------------------------- RRF SQL + lexical sanitizer


def test_rrf_sql_is_one_parameterized_statement_with_tier_filter_both_ctes_and_rrf() -> None:
    sql = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", top_k=50)
    assert sql.count(";") == 1 and "VECTOR_SEARCH(" in sql and "SEARCH(content, REGEXP_REPLACE(@lexical_query" in sql
    assert "WITH vector_results AS" in sql and "lexical_results AS" in sql and "FULL OUTER JOIN lexical_results" in sql
    assert sql.count("tier_profile = 'hybrid'") == 3  # vector base table, lexical scan, final join
    assert "COALESCE(1.0 / (60 + v.vector_rank), 0.0) + COALESCE(1.0 / (60 + l.lexical_rank), 0.0) AS rrf_score" in sql
    assert "top_k => 50" in sql and "WHERE lexical_rank <= 50" in sql and sql.rstrip().endswith("LIMIT 50;")
    assert "ORDER BY f.rrf_score DESC" in sql and "distance_type => 'COSINE'" in sql
    for param in ("@query_embedding", "@lexical_query", "@lexical_terms"):
        assert param in sql
    assert "@solicitation_id" not in sql and "@tier_profile" not in sql
    filtered = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", include_solicitation_filter=True)
    assert filtered.count("solicitation_id = @solicitation_id") == 2
    hostile = "CLIN-0004-AB'; DELETE FROM x; --"
    assert hostile not in build_hybrid_rrf_sql("demo-proj", "rag_arena", "t") and "DELETE" not in sql.upper()
    with pytest.raises(ValueError, match="tier_profile"):
        build_hybrid_rrf_sql("demo-proj", "rag_arena", "t", tier_profile="naive' OR '1'='1")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        build_hybrid_rrf_sql("demo-proj", "rag_arena", "t`; DROP")
    with pytest.raises(ValueError, match="top_k"):
        build_hybrid_rrf_sql("demo-proj", "rag_arena", "t", top_k=0)


def test_lexical_terms_keep_hyphenated_clin_and_sec_ids_whole_and_drop_stopwords() -> None:
    terms = build_lexical_terms("What is the amended rate for CLIN-0004-AB (Cloud Architect) under SEC-4.2.1? DATA-SLA-3.1!")
    assert terms == ("amended", "rate", "clin-0004-ab", "cloud", "architect", "sec-4.2.1", "data-sla-3.1")
    assert not set(terms) & LEXICAL_STOPWORDS and all(re.fullmatch(r"[a-z0-9.-]+", t) for t in terms)
    assert build_lexical_terms("Q12 of A1 'x'; DROP TABLE t; --") == ("q12", "a1", "drop", "table")
    with pytest.raises(ValueError):
        build_lexical_terms("what is the of")


# ---------------------------------------------------------------- query_prep


def test_acronym_expansion_is_data_driven_case_sensitive_and_listed_in_payload() -> None:
    prepared = prepare_query("Compare the  SLA credits and RTO/RPO for CLIN-0004-AB under the QASP")
    assert [t for t, _ in prepared.expansions] == ["CLIN", "SLA", "RTO", "RPO", "QASP"]
    assert prepared.prepared.endswith(
        "(CLIN = Contract Line Item Number; SLA = Service Level Agreement; RTO = Recovery Time Objective; "
        "RPO = Recovery Point Objective; QASP = Quality Assurance Surveillance Plan)"
    )
    assert prepared.payload()["expansions"][0] == {"term": "CLIN", "expansion": "Contract Line Item Number"}
    assert prepared.original == "Compare the SLA credits and RTO/RPO for CLIN-0004-AB under the QASP"
    assert prepare_query("the pep talk about slas").expansions == ()
    assert {t for t, _ in ACRONYM_EXPANSIONS} >= {"CLIN", "SLA", "RTO", "RPO", "QASP", "PEP", "SOW", "RFP"}


# ---------------------------------------------------------------- Ranking API REST contract


def test_rank_request_headers_body_bounds_and_response_order() -> None:
    post = FakeRankPost()
    records = [make_record("a", "Doc A SEC-1", "alpha"), make_record("b", "Doc B", "x" * (MAX_RECORD_CONTENT_CHARS + 50))]
    assert records[1].content_truncated and len(records[1].content) == MAX_RECORD_CONTENT_CHARS
    response = _client(post).rank("CLIN-0004-AB rate", records, top_n=2)
    url, kwargs = post.calls[0]
    assert url == ranking_url("test-project")
    assert url.endswith("/v1/projects/test-project/locations/global/rankingConfigs/default_ranking_config:rank")
    assert url.startswith("https://discoveryengine.googleapis.com/")
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token" and kwargs["headers"]["x-goog-user-project"] == "test-project"
    body = kwargs["json"]
    assert body["model"] == "semantic-ranker-default@latest" and body["query"] == "CLIN-0004-AB rate" and body["topN"] == 2
    assert body["records"][0] == {"id": "a", "title": "Doc A SEC-1", "content": "alpha"}
    assert body["ignoreRecordDetailsInResponse"] is True and 0 < kwargs["timeout"] <= 5.0
    assert response.scores == (("b", 0.5), ("a", 0.2)) and response.attempts == 1
    with pytest.raises(ValueError, match="top_n"):
        _client(post).rank("q", records, top_n=3)
    with pytest.raises(ValueError, match="unique"):
        _client(post).rank("q", [records[0], records[0]], top_n=1)


def test_rank_retries_only_transient_statuses_and_redacts_project_in_errors() -> None:
    sleeps: list[float] = []
    post = FakeRankPost(statuses=[429, 503])
    assert _client(post, sleeps).rank("q", [make_record("a", "t", "c")], top_n=1).attempts == 3
    assert sleeps == [0.5, 1.0]
    forbidden = FakeRankPost(statuses=[403])
    try:
        _client(forbidden).rank("q", [make_record("a", "t", "c")], top_n=1)
    except RankingHTTPError as exc:
        assert exc.status_code == 403 and "test-project" not in str(exc) and "${GCP_PROJECT_ID}" in str(exc)
    else:
        raise AssertionError("403 must raise RankingHTTPError")
    assert len(forbidden.calls) == 1
    bad = FakeRankPost(scores={"a": 0.9})
    bad_client = _client(bad)
    with pytest.raises(RankingResponseError):
        bad_client.rank("q", [make_record("a", "t", "c"), make_record("b", "t", "c")], top_n=2)


# ---------------------------------------------------------------- engine stages


def test_level2_emits_canonical_stages_with_contract_payloads() -> None:
    result, emitter, genai, bq, post = _run_level2()
    kinds = [(e["event"], e.get("stage")) for e in emitter.events]
    stages = ("query_prep", "embed_query", "retrieve", "rerank", "synthesize")
    assert kinds == [(k, s) for s in stages for k in ("stage_started", "stage_completed")]
    done = {e["stage"]: e for e in emitter.events if e["event"] == "stage_completed"}
    started = {e["stage"]: e for e in emitter.events if e["event"] == "stage_started"}
    assert [x["term"] for x in done["query_prep"]["payload"]["expansions"]] == ["CLIN", "SLA"]
    assert genai.calls[0][1]["contents"].endswith("(CLIN = Contract Line Item Number; SLA = Service Level Agreement)")
    assert set(done["embed_query"]["payload"]) == {"model", "dimensions", "token_count", "task_type"}
    req = started["retrieve"]["request"]
    assert set(req) == {"sql", "params", "candidate_k"} and req["candidate_k"] == 50
    assert req["params"]["query_embedding"]["dimensions"] == 3072 and len(req["params"]["query_embedding"]["preview"]) == 4
    assert req["params"]["lexical_terms"] == ["hourly", "rate", "clin-0004-ab", "sla"]
    retrieve = done["retrieve"]["payload"]
    assert retrieve["rows_returned"] == 3 and retrieve["rrf_k"] == 60 and retrieve["with_both_ranks"] == 1
    assert {"bytes_processed", "slot_millis", "cache_hit", "job_id"} <= set(retrieve)
    first = retrieve["candidates"][0]
    assert set(first) == {"chunk_id", "doc_name", "page_number", "section_id", "vector_rank", "lexical_rank", "vector_distance", "rrf_score"}
    assert retrieve["candidates"][1]["vector_rank"] is None and retrieve["candidates"][2]["lexical_rank"] is None
    rerank_req = started["rerank"]["request"]
    assert rerank_req["service"] == "Discovery Engine Ranking API" and rerank_req["records_sent"] == 3 and rerank_req["top_n"] == 3
    rerank = done["rerank"]["payload"]
    assert rerank["service_used"] == "Discovery Engine Ranking API" and rerank["candidates_in"] == 3 and rerank["candidates_out"] == 3
    assert [r["rerank_rank"] for r in rerank["results"]] == [1, 2, 3]
    assert [r["rrf_rank"] for r in rerank["results"]] == [3, 2, 1]  # fake ranker reverses the RRF order
    assert rerank["results"][0]["rerank_score"] >= rerank["results"][-1]["rerank_score"] and rerank["billed_counts"] == 1
    assert isinstance(rerank["rank_api_ms"], int)
    sent = post.calls[0][1]["json"]
    assert sent["query"] == _QUERY.replace("  ", " ") and sent["records"][0]["title"] == "01_Doc.pdf CLIN-0001-AB"


def test_level2_result_flags_cost_and_citations_follow_rerank_order() -> None:
    result, emitter, *_ = _run_level2()
    assert result.flags == EngineFlags(reranker_used=True, adk_runner_used=False, layout_source="pymupdf")
    assert [c.chunk_id for c in result.retrieved_chunks] == [_ROWS[2]["chunk_id"], _ROWS[1]["chunk_id"], _ROWS[0]["chunk_id"]]
    assert result.citations[0].chunk_id == _ROWS[2]["chunk_id"] and result.retrieved_chunks[0].tier_profile == "hybrid"
    expected = round(cost_from_usage(result.usage, "gemini-4-argon") + rank_cost_usd(rank_billed_counts(3)), 8)
    assert result.cost_usd == expected and rank_cost_usd(1) == 0.001
    assert rank_billed_counts(50) == 1 and rank_billed_counts(132) == 2 and rank_billed_counts(401) == 5
    assert [s.stage for s in result.stage_log] == ["query_prep", "embed_query", "retrieve", "rerank", "synthesize"]


def test_rerank_failure_is_stage_failed_and_raises_without_any_heuristic_fallback() -> None:
    exc, emitter, genai, _, post = _run_level2(post=FakeRankPost(statuses=[403]))
    assert isinstance(exc, RankingHTTPError) and exc.status_code == 403
    kinds = [(e["event"], e.get("stage")) for e in emitter.events]
    assert kinds[-2:] == [("stage_started", "rerank"), ("stage_failed", "rerank")]
    assert emitter.events[-1]["error_type"] == "RankingHTTPError" and "test-project" not in emitter.events[-1]["error"]
    assert [c[0] for c in genai.calls] == ["embed_content"]  # synthesis never ran on unranked candidates
    import app.engines.level2_hybrid as l2

    assert not hasattr(l2, "rerank_chunks_heuristic_or_api")
    source = open(l2.__file__, encoding="utf-8").read()
    assert "heuristic" not in source.split('"""', 2)[2].lower().replace("no heuristic", "")


def test_empty_hybrid_tier_fails_loud_in_retrieve() -> None:
    exc, emitter, *_ = _run_level2(rows=[])
    assert isinstance(exc, RuntimeError) and "Level 2 ingestion" in str(exc)
    assert (emitter.events[-1]["event"], emitter.events[-1]["stage"]) == ("stage_failed", "retrieve")


def test_level2_accepts_any_catalog_synthesis_model() -> None:
    settings = live_settings(enabled_levels=("level1_naive", "level2_hybrid"))
    for synth, judge in (("gemini-3.8-flash", "claude-opus-5-5"), ("claude-fable-5-1", "gemini-4-argon")):
        engine = build_engine("level2_hybrid", settings, load_catalog(settings).select(synth, judge))
        assert isinstance(engine, Level2HybridEngine) and engine.synthesis_model.id == synth
    result, *_ = _run_level2(synthesis="gemini-3.8-flash")
    assert result.usage.model == "gemini-3.8-flash"


# ---------------------------------------------------------------- ingestion + architecture


def test_hybrid_chunker_one_lineage_chunk_per_page_and_cuad_doc_type() -> None:
    pages = [["DOC_TYPE=PRICING_CLIN | SECTION=CLIN-0004-AB | ELEMENT=table | AMENDMENT=0 | EFFECTIVE=2026-01-15 | AMENDS=NONE", "| CLIN-0004-AB | $210.00/hr |"], ["page two text"]]
    chunks = chunk_pages(pages, pdf_name="04_Pricing_Schedule_CLINs.pdf", solicitation_id="solicitation_01", tier_profile="hybrid")
    assert [c.chunk_id for c in chunks] == ["solicitation_01_04_pricing_schedule_clins_p1_hybrid", "solicitation_01_04_pricing_schedule_clins_p2_hybrid"]
    assert chunks[0].content.startswith("[Contextual Lineage: Solicitation=solicitation_01 | Document=04_Pricing_Schedule_CLINs.pdf | DocType=PRICING_CLIN | Section=CLIN-0004-AB]")
    assert chunks[0].element_type == "table" and all(c.tier_profile == "hybrid" for c in chunks)
    assert _infer_doc_type_from_filename("CoherusBiosciencesInc_20200227_10-K_EX-10.29_12021376_EX-10.29_Development Agreement.pdf") == ("COMMERCIAL_AGREEMENT", 0)
    assert _infer_doc_type_from_filename("07_Amendment_0002_Vendor_QA.pdf") == ("AMENDMENT", 2)
    assert _infer_doc_type_from_filename("05_Amendments_and_Addenda.pdf") == ("AMENDMENT", 1)


def test_hybrid_ingest_tier_replace_is_scoped_to_hybrid_only() -> None:
    class _Job:
        def __init__(self, job_id: str, statement_type: str | None = None, inserted: int = 0) -> None:
            self.job_id, self.statement_type, self.errors, self.output_rows = job_id, statement_type, None, inserted
            self.dml_stats = SimpleNamespace(inserted_row_count=inserted) if statement_type == "INSERT" else None

        def result(self, timeout: float | None = None) -> list[Any]:
            return []

    class _BQ:
        def __init__(self) -> None:
            self.queries: list[tuple[str, Any]] = []
            self.deleted: list[str] = []

        def create_table(self, table: Any, exists_ok: bool = False) -> None: ...

        def load_table_from_json(self, rows: list[dict[str, Any]], dest: str, job_config: Any = None) -> _Job:
            return _Job("load_1", inserted=len(rows))

        def query(self, sql: str, job_config: Any = None) -> _Job:
            self.queries.append((sql, job_config))
            return _Job("txn_1")

        def list_jobs(self, parent_job: str, max_results: int) -> list[_Job]:
            return [_Job("c1", "DELETE"), _Job("c2", "INSERT", inserted=2)]

        def delete_table(self, fqn: str, not_found_ok: bool = False) -> None:
            self.deleted.append(fqn)

    bq = _BQ()
    loader = lb.BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=bq)
    names = [n for n, _, _ in lb._CHUNK_SCHEMA]
    rows = [{**{n: None for n in names}, "chunk_id": f"c{i}_hybrid", "tier_profile": "hybrid", "ingest_run_id": "ing_abc123", "content": "x"} for i in range(2)]
    assert loader.load_rows(rows) == 2
    script, config = bq.queries[-1]
    assert "DELETE FROM `test-project.rag_arena.document_chunks_v3` WHERE tier_profile = @tier;" in script
    assert [(p.name, p.value) for p in config.query_parameters] == [("tier", "hybrid")]
    assert "document_chunks`" not in script.replace("document_chunks_v3`", "") and "TRUNCATE" not in script.upper()
    assert bq.deleted == ["test-project.rag_arena._staging_ing_abc123"]


def test_level2_architecture_descriptor_covers_every_stage_with_honest_services() -> None:
    desc = get_architecture_descriptor("level2_hybrid")
    by_stage = {s: n for n in desc.nodes for s in n.stage_ids}
    assert set(Level2HybridEngine.stages) <= set(by_stage)
    assert by_stage["rerank"].service == "Discovery Engine Ranking API" and by_stage["rerank"].region == "global"
    assert "VECTOR_SEARCH + SEARCH()" in by_stage["retrieve"].service and "RRF" in by_stage["retrieve"].service
    assert all(n.code_ref.startswith("app.") for n in desc.nodes if n.id != "judge")
    ingest = {s: n for n in desc.ingest_nodes for s in n.stage_ids}
    assert ingest["discover_docs"].service.startswith("Local corpus") and "lineage" in ingest["chunk"].label.lower()


def test_index_usage_is_reported_verbatim_without_the_project_echo() -> None:
    reason = {"code": "OTHER_REASON", "message": "The search index cannot be used when a vector search index is used.",
              "baseTable": {"projectId": "test-project", "datasetId": "rag_arena", "tableId": "t"}, "indexName": "t_search_idx"}
    job = SimpleNamespace(_properties={"statistics": {"query": {
        "searchStatistics": {"indexUsageMode": "UNUSED", "indexUnusedReasons": [reason]},
        "vectorSearchStatistics": {"indexUsageMode": "PARTIALLY_USED"}}}})
    usage = _index_usage(job)
    assert usage["search"] == {"index_usage_mode": "UNUSED", "index_unused_reasons": [
        {"code": "OTHER_REASON", "message": reason["message"], "indexName": "t_search_idx"}]}
    assert usage["vector_search"] == {"index_usage_mode": "PARTIALLY_USED", "index_unused_reasons": []}
    assert "test-project" not in json.dumps(usage)
    assert _index_usage(SimpleNamespace()) == {"search": None, "vector_search": None}


def test_judge_and_synthesis_see_exactly_the_reranked_top_n_that_citations_come_from() -> None:
    from app.evaluation.evaluator import build_judge_user_prompt

    settings = live_settings(enabled_levels=("level1_naive", "level2_hybrid"), rerank_top_n=2)
    emitter = RecordingStageEmitter(run_id="run_l2_topn", engine="level2_hybrid")
    with fake_clients(FakeGenaiClient(), FakeBigQueryClient(rows=_ROWS)) as (genai, _):
        engine = Level2HybridEngine(settings=settings, ranking_client=_client(FakeRankPost()))
        result = run(engine.run_query(_QUERY, question_id="q", solicitation_filter=None, emitter=emitter))
    top_ids = [_ROWS[2]["chunk_id"], _ROWS[1]["chunk_id"]]  # fake ranker reverses RRF order; top-2 cutoff
    assert [c.chunk_id for c in result.retrieved_chunks] == top_ids == [c.chunk_id for c in result.citations]
    judge_prompt = build_judge_user_prompt(_QUERY, result, None)
    assert _ROWS[2]["content"] in judge_prompt and _ROWS[1]["content"] in judge_prompt
    assert _ROWS[0]["content"] not in judge_prompt  # rank-3 candidate is neither judged nor synthesized
    synth_prompt = next(c for c in genai.calls if c[0] == "generate_content")[1]["contents"]
    assert _ROWS[0]["content"] not in synth_prompt and _ROWS[2]["content"] in synth_prompt
    done = {e["stage"]: e for e in emitter.events if e["event"] == "stage_completed"}
    assert done["rerank"]["payload"]["candidates_out"] == 2 and done["synthesize"]["payload"]["model"] == "gemini-4-argon"
