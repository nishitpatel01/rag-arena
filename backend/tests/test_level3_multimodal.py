"""Level 3 multimodal (offline; httpx / google-genai / BigQuery mocked): Document AI mapping + page split, provisioning
isolation, vision prompt/response + fail-loud, diagram detection, multimodal embed request shape, tier-scoped load,
RRF SQL on embedding_multimodal, authority headers, engine stage payloads, no PyMuPDF fallback, architecture."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.architectures import CANONICAL_INGEST_STAGES, get_architecture_descriptor
from app.core.config import ArenaSettings, get_settings
from app.core.models import BoundingBox, DocumentChunk
from app.core.models_catalog import load_catalog
from app.core.telemetry import RecordingStageEmitter
from app.engines import build_engine
from app.engines.level3_multimodal import (
    Level3MultimodalEngine,
    authority_header,
    authority_order,
    build_authority_context,
    detect_element_intent,
)
from app.ingestion import live_backends as lb
from app.ingestion.bq_indexer import build_hybrid_rrf_sql, build_tables_ddl
from app.ingestion.documentai_client import (
    DocumentAIClient,
    DocumentAIHTTPError,
    ensure_layout_parser,
    find_layout_parser,
    processor_info,
)
from app.ingestion.jobs import IngestBackends, IngestJobRunner
from app.ingestion.layout_documentai import (
    PROCESS_OPTIONS,
    LayoutParser,
    blocks_to_elements,
    normalized_bbox,
    split_pdf_ranges,
)
from app.ingestion.multimodal import EmbedItem, VertexMultimodalEmbedder, group_page_elements
from app.ingestion.pipeline import chunk_to_bigquery_row
from app.ingestion.vision_transcribe import (
    VISION_PROMPT,
    GeminiVisionTranscriber,
    VisionResult,
    VisionTranscriptionError,
    detect_diagram_pages,
    labels_in_text_layer,
    parse_vision_response,
)
from fakes import FakeBigQueryClient, FakeGenaiClient, fake_clients, live_settings, run

_CORPUS = Path(__file__).resolve().parents[2] / "data" / "sample_docs" / "procurement_corpus"
_EXHIBIT = _CORPUS / "solicitation_01_cloud_infrastructure" / "03_Technical_Exhibit_Architecture.pdf"
_PRICING = _CORPUS / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf"
_PROJECT = "test-project"


def _vertices(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"normalizedVertices": [{"x": x0, "y": y0}, {"x": x0, "y": y1}, {"x": x1, "y": y1}, {"x": x1, "y": y0}]}


def _text(bid: str, text: str, kind: str = "paragraph", page: int = 1, children: list | None = None, box: dict | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"blockId": bid, "textBlock": {"text": text, "type": kind, "blocks": children or []}, "pageSpan": {"pageStart": page, "pageEnd": page}}
    if box:
        block["boundingBox"] = box
    return block


def _cell(text: str) -> dict[str, Any]:
    return {"blocks": [_text("c", text)]}


def _table(bid: str, header: list[str] | None, body: list[list[str]], page: int = 1) -> dict[str, Any]:
    tb: dict[str, Any] = {"bodyRows": [{"cells": [_cell(c) for c in row]} for row in body]}
    if header:
        tb["headerRows"] = [{"cells": [_cell(c) for c in header]}]
    return {"blockId": bid, "tableBlock": tb, "pageSpan": {"pageStart": page, "pageEnd": page}}


_DOC_BLOCKS = [
    _text("1", "SOL-2026-CLOUD-01 | 04_Pricing_Schedule_CLINs.pdf", "header"),
    _text("2", "SCHEDULE B: CLIN PRICING MATRIX", "heading-1", children=[
        _text("3", "DOC_TYPE=PRICING_CLIN | SECTION=CLIN-0004-AB | ELEMENT=table | AMENDMENT=0 | EFFECTIVE=2026-01-15 | AMENDS="),
        _table("4", ["CLIN", "Base Rate"], [["CLIN-0004-AB", "$210.00/hr"], ["CLIN-0005-AC", "$195 | hr"]]),
        _text("5", "Note: CLIN-0004-AB base rate is $210/hr.", box=_vertices(0.07, 0.30, 0.9, 0.33)),
        {"blockId": "6", "imageBlock": {"mimeType": "image/png", "imageText": "VECTOR DIAGRAM: topology"}, "pageSpan": {"pageStart": 2, "pageEnd": 2},
         "boundingBox": _vertices(-0.1, 0.5, 1.2, 0.7)},
    ]),
    _text("7", "Page 1 of 1 | Verified Enterprise Procurement Record", "footer"),
]


# ---------------------------------------------------------------- Document AI layout → elements


def test_documentai_blocks_map_to_elements_with_markdown_tables_headings_figures_and_skips() -> None:
    elements, skipped = blocks_to_elements(_DOC_BLOCKS, page_offset=15)
    kinds = [(e.kind, e.page_number) for e in elements]
    assert kinds == [("heading", 16), ("table", 16), ("paragraph", 16), ("figure", 17)]
    assert skipped == {"header_footer": 2, "metadata_banner": 1, "empty": 0}
    table = elements[1]
    assert table.text.splitlines()[:2] == ["| CLIN | Base Rate |", "| --- | --- |"]
    assert "| CLIN-0005-AC | $195 \\| hr |" in table.text  # pipes inside cells are escaped
    assert table.heading == "SCHEDULE B: CLIN PRICING MATRIX" and table.rows == 2 and table.cols == 2 and not table.header_inferred
    assert elements[2].bbox == (0.07, 0.3, 0.9, 0.33) and elements[2].bbox_source == "documentai"
    assert elements[3].bbox == (0.0, 0.5, 1.0, 0.7)  # clamped to the page
    assert elements[1].bbox is None and elements[1].bbox_source == ""  # Gemini layout parser: no box → located later


def test_table_without_header_rows_uses_first_body_row_and_flags_it() -> None:
    elements, _ = blocks_to_elements([_table("t", None, [["Service", "Credit"], ["P1", "10%"]])])
    assert elements[0].header_inferred and elements[0].text.splitlines()[0] == "| Service | Credit |" and elements[0].rows == 1
    assert normalized_bbox({"boundingBox": _vertices(0.5, 0.5, 0.5, 0.6)}) is None  # zero-width box is rejected


def _pdf_with_pages(n: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    return doc.tobytes()


def test_split_pdf_ranges_respects_the_15_page_online_limit_and_30_page_cap() -> None:
    import pymupdf

    kept, ranges = split_pdf_ranges(_pdf_with_pages(40))
    assert kept == 30 and [start for start, _ in ranges] == [1, 16]
    assert [len(pymupdf.open(stream=b, filetype="pdf")) for _, b in ranges] == [15, 15]
    kept_small, small = split_pdf_ranges(_pdf_with_pages(3))
    assert kept_small == 3 and len(small) == 1 and small[0][0] == 1
    with pytest.raises(ValueError, match="online limit"):
        split_pdf_ranges(_pdf_with_pages(3), range_size=16)


class _FakeHTTP:
    """Records Document AI REST calls; answers from a per-URL-substring script."""

    def __init__(self, responses: dict[str, list[httpx.Response]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.responses = responses or {}

    def _answer(self, method: str, url: str, body: dict[str, Any] | None) -> httpx.Response:
        self.calls.append((method, url, body))
        for key, queue in self.responses.items():
            if key in url and queue:
                resp = queue.pop(0) if len(queue) > 1 else queue[0]
                return httpx.Response(resp.status_code, content=resp.content, request=httpx.Request(method, url))
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self._answer("POST", url, kw.get("json"))

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self._answer("GET", url, None)


def _client(http: _FakeHTTP) -> DocumentAIClient:
    return DocumentAIClient(project_id=_PROJECT, location="us", token_provider=lambda: "tok", post=http.post, get=http.get, sleep=lambda s: None)


def test_layout_parser_sends_raw_pdf_ranges_with_layout_config_and_offsets_pages() -> None:
    doc = {"document": {"documentLayout": {"blocks": [_text("1", "Heading", "heading-1", children=[_text("2", "Body text")])]}}}
    http = _FakeHTTP({":process": [httpx.Response(200, json=doc)]})
    parser = LayoutParser(client=_client(http), processor_id="abc123", processor_version="pretrained-layout-parser-v1.5-2025-08-25")
    layout = parser.parse("big.pdf", _pdf_with_pages(20))
    assert layout.requests == 2 and layout.pages == 20 and layout.attempts == 2
    assert [e.page_number for e in layout.elements] == [1, 1, 16, 16]
    method, url, body = http.calls[0]
    assert method == "POST" and url.endswith("/processors/abc123/processorVersions/pretrained-layout-parser-v1.5-2025-08-25:process")
    assert body["rawDocument"]["mimeType"] == "application/pdf" and base64.b64decode(body["rawDocument"]["content"]).startswith(b"%PDF-")
    assert body["processOptions"] == PROCESS_OPTIONS and "chunkingConfig" not in json.dumps(body["processOptions"])
    assert body["processOptions"]["layoutConfig"]["returnBoundingBoxes"] is True
    assert parser.request_summary()["processor_version"].startswith("pretrained-layout-parser-v1.5")


def test_documentai_errors_are_redacted_and_non_retryable_statuses_fail_immediately() -> None:
    body = {"error": {"code": 403, "message": f"Permission denied on projects/{_PROJECT} (projects/123456789012/locations/us)"}}
    http = _FakeHTTP({":process": [httpx.Response(403, json=body)]})
    try:
        _client(http).process_pdf("abc123", _pdf_with_pages(1), PROCESS_OPTIONS)
    except DocumentAIHTTPError as exc:
        text = str(exc)
        assert exc.status_code == 403 and _PROJECT not in text and "123456789012" not in text
        assert "${GCP_PROJECT_ID}" in text and "projects/${GCP_PROJECT_NUMBER}" in text
    else:
        raise AssertionError("403 did not raise DocumentAIHTTPError")
    assert len(http.calls) == 1  # no retry on 403
    flaky = _FakeHTTP({":process": [httpx.Response(503, json={}), httpx.Response(200, json={"document": {"documentLayout": {"blocks": []}}})]})
    doc, attempts = _client(flaky).process_pdf("abc123", _pdf_with_pages(1), PROCESS_OPTIONS)
    assert attempts == 2 and doc == {"documentLayout": {"blocks": []}}


# ---------------------------------------------------------------- provisioning isolation


def _processor(name: str, display: str, kind: str = "LAYOUT_PARSER_PROCESSOR") -> dict[str, Any]:
    return {"name": f"projects/123456789012/locations/us/processors/{name}", "displayName": display, "type": kind, "state": "ENABLED",
            "defaultProcessorVersion": f"projects/123456789012/locations/us/processors/{name}/processorVersions/pretrained-layout-parser-v1.0-2024-06-03"}


_OTHERS = [_processor("aaa111", "national_beverage_splitter", "SPLITTER"), _processor("bbb222", "invoice_test", "INVOICE_PROCESSOR"),
           _processor("ccc333", "rag-arena-layout-parser-old"), _processor("ddd444", "Rag-Arena-Layout-Parser")]


def test_provisioning_creates_only_our_processor_and_never_touches_others() -> None:
    created = _processor("eee555", "rag-arena-layout-parser")
    http = _FakeHTTP({"/processors?": [httpx.Response(200, json={"processors": _OTHERS})], "/processors": [httpx.Response(200, json=created)]})
    info, was_created = ensure_layout_parser(_client(http))
    assert was_created and info.processor_id == "eee555" and info.default_version == "pretrained-layout-parser-v1.0-2024-06-03"
    methods = [(m, u.split("/locations/us")[1]) for m, u, _ in http.calls]
    assert methods == [("GET", "/processors?pageSize=100"), ("POST", "/processors")]
    assert http.calls[1][2] == {"type": "LAYOUT_PARSER_PROCESSOR", "displayName": "rag-arena-layout-parser"}
    assert not any(other["name"].rsplit("/", 1)[1] in u for _, u, _ in http.calls for other in _OTHERS)
    existing = _FakeHTTP({"/processors?": [httpx.Response(200, json={"processors": [*_OTHERS, created]})]})
    info2, was_created2 = ensure_layout_parser(_client(existing))
    assert not was_created2 and info2.processor_id == "eee555" and [m for m, _, _ in existing.calls] == ["GET"]


def test_provisioning_refuses_ambiguous_or_wrong_type_matches() -> None:
    infos = [processor_info(p) for p in _OTHERS]
    assert find_layout_parser(infos) is None
    with pytest.raises(RuntimeError, match="refusing to reuse"):
        find_layout_parser([*infos, processor_info(_processor("x1", "rag-arena-layout-parser", "OCR_PROCESSOR"))])
    with pytest.raises(RuntimeError, match="duplicate"):
        find_layout_parser([processor_info(_processor("x1", "rag-arena-layout-parser")), processor_info(_processor("x2", "rag-arena-layout-parser"))])


# ---------------------------------------------------------------- vision transcription


_VISION_JSON = json.dumps({
    "title": "VECTOR DIAGRAM: Multi-Region VPC & HA VPN Topology",
    "components": [{"id": "C1", "label": "Cloud Armor WAF + Global LB", "shape": "box"}, {"id": "C2", "label": "Cloud HA VPN (BGP ASN 64512)", "shape": "box"}],
    "connections": [{"from": "C1", "to": "C2", "label": "", "style": "line"}],
    "regions": [{"label": "VPC", "contains": ["C1", "C2", "C9"]}],
    "notes": ["Connectors have no arrowheads."],
})


def test_vision_response_parsing_is_strict_and_fails_loud() -> None:
    spec = parse_vision_response("```json\n" + _VISION_JSON + "\n```", "x.pdf p.1")
    assert [c[1] for c in spec.components] == ["Cloud Armor WAF + Global LB", "Cloud HA VPN (BGP ASN 64512)"]
    assert spec.regions == (("VPC", ("C1", "C2")),)  # unknown member ids are dropped, never invented
    text = spec.to_text()
    assert "- Cloud Armor WAF + Global LB -- Cloud HA VPN (BGP ASN 64512)" in text and text.startswith("Diagram: VECTOR DIAGRAM")
    for bad, reason in (("", "empty"), ("not json", "not JSON"), ('{"components": []}', "no labelled components"),
                        (json.dumps({"components": [{"id": "C1", "label": "A"}], "connections": [{"from": "C1", "to": "C7"}]}), "unknown component")):
        try:
            parse_vision_response(bad, "x.pdf p.1")
        except VisionTranscriptionError as exc:
            assert reason in str(exc) and "x.pdf p.1" in str(exc)
        else:
            raise AssertionError(f"no error for {bad!r}")
    assert labels_in_text_layer(spec, "... cloud armor waf + global lb ...") == 0.5


class _VisionClient:
    def __init__(self, text: str, usage: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        meta = SimpleNamespace(prompt_token_count=1700, candidates_token_count=300, thoughts_token_count=500) if usage else None
        self._resp = SimpleNamespace(text=text, usage_metadata=meta)
        self.models = SimpleNamespace(generate_content=self._gen, embed_content=self._embed)
        self.embed_calls: list[dict[str, Any]] = []

    def _gen(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return self._resp

    def _embed(self, **kw: Any) -> Any:
        self.embed_calls.append(kw)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 8)])


def test_vision_transcriber_sends_one_png_part_with_strict_prompt_in_json_mode() -> None:
    from app.ingestion.vision_transcribe import render_page_png

    png = render_page_png(_EXHIBIT, 1)
    client = _VisionClient(_VISION_JSON)
    result = GeminiVisionTranscriber(client=client, model="gemini-4-argon").transcribe(png, 1, "03.pdf p.1")
    call = client.calls[0]
    assert call["model"] == "gemini-4-argon" and call["config"]["response_mime_type"] == "application/json"
    assert call["contents"][1] == VISION_PROMPT and getattr(call["contents"][0], "inline_data").mime_type == "image/png"
    assert result.prompt_tokens == 1700 and result.thinking_tokens == 500 and len(result.spec.components) == 2
    with pytest.raises(VisionTranscriptionError, match="empty response"):
        GeminiVisionTranscriber(client=_VisionClient(""), model="gemini-4-argon").transcribe(png, 1, "03.pdf p.1")
    with pytest.raises(VisionTranscriptionError, match="usage_metadata"):
        GeminiVisionTranscriber(client=_VisionClient(_VISION_JSON, usage=False), model="gemini-4-argon").transcribe(png, 1, "03.pdf p.1")


def test_diagram_page_detection_uses_box_and_connector_heuristic_or_documentai_figures() -> None:
    assert detect_diagram_pages(_EXHIBIT, [1], set()) == {1: "vector_box_diagram"}
    assert detect_diagram_pages(_PRICING, [1], set()) == {}
    assert detect_diagram_pages(_PRICING, [1], {1}) == {1: "documentai_figure"}
    assert detect_diagram_pages(_EXHIBIT, [1], {1}) == {1: "documentai_figure+vector_box_diagram"}


# ---------------------------------------------------------------- multimodal embedding + chunking


def test_multimodal_embedder_sends_one_call_per_item_and_one_content_for_text_plus_image() -> None:
    client = _VisionClient(_VISION_JSON)
    embedder = VertexMultimodalEmbedder(client=client, model="gemini-embedding-2")
    vectors = embedder.embed_items([EmbedItem(text="CLIN table", title="SCHEDULE B"), EmbedItem(text="Diagram", title="EXHIBIT A", image_png=b"\x89PNGfake")])
    assert len(vectors) == 2 and len(client.embed_calls) == 2
    by_kind = {isinstance(c["contents"], str): c for c in client.embed_calls}
    assert by_kind[True]["contents"] == "title: SCHEDULE B | text: CLIN table"
    content = by_kind[False]["contents"]
    assert len(content.parts) == 2 and content.parts[0].text == "title: EXHIBIT A | text: Diagram"
    assert content.parts[1].inline_data.mime_type == "image/png"
    assert all(c["config"].task_type == "RETRIEVAL_DOCUMENT" and c["model"] == "gemini-embedding-2" for c in client.embed_calls)


def test_element_grouping_keeps_tables_atomic_and_groups_paragraphs_by_heading() -> None:
    elements, _ = blocks_to_elements([
        _text("1", "SEC A", "heading-1", children=[_text("2", "para one"), _text("3", "para two"), _table("4", ["a"], [["1"]]), _text("5", "para three")]),
        _text("6", "SEC B", "heading-1", children=[_text("7", "x" * 1900)]),
    ])
    groups = group_page_elements(elements)
    assert [g.kind for g in groups] == ["narrative_text", "table", "narrative_text", "narrative_text"]
    assert groups[0].texts == ("SEC A", "para one", "para two") and groups[0].heading == "SEC A" and groups[1].heading == "SEC A"
    assert groups[2].texts == ("para three",) and groups[3].texts[0] == "SEC B" and len(groups[3].texts[1]) == 1900


def _mm_chunk(cid: str, *, amendment: int = 0, amends: str | None = None, section: str | None = "CLIN-0004-AB", element: str = "table", eff: str = "2026-01-15") -> DocumentChunk:
    return DocumentChunk(
        chunk_id=cid, tier_profile="multimodal", solicitation_id="solicitation_01", doc_name=f"{cid}.pdf",
        doc_type="AMENDMENT" if amendment else "PRICING_CLIN", element_type=element, section_id=section, amendment_number=amendment,
        effective_date=eff, amends_section=amends, page_number=1, bbox=BoundingBox(page=1, x0=0.1, y0=0.2, x1=0.9, y1=0.4),
        content=f"content of {cid}", score=0.9, layout_source="documentai_layout" if element != "diagram" else "gemini_vision",
        bbox_source="pdf_text_layer",
    )


def test_multimodal_rows_fill_both_embedding_columns_and_carry_layout_and_bbox_source() -> None:
    row = chunk_to_bigquery_row(_mm_chunk("c1"), [0.1, 0.2], ingest_run_id="ing_abc123def456", layout_source="pymupdf")
    assert row["embedding"] == row["embedding_multimodal"] == [0.1, 0.2]
    assert row["layout_source"] == "documentai_layout" and row["bbox_source"] == "pdf_text_layer"
    assert lb._validate_chunk_rows([row]) == ("multimodal", "ing_abc123def456")
    naive = dict(row, tier_profile="naive")
    with pytest.raises(ValueError, match="single tier"):
        lb._validate_chunk_rows([row, naive])
    ddl = build_tables_ddl("demo-proj", "rag_arena", "document_chunks_v3")
    assert "ADD COLUMN IF NOT EXISTS embedding_multimodal ARRAY<FLOAT64>" in ddl and "ADD COLUMN IF NOT EXISTS bbox_source STRING" in ddl
    assert "DROP" not in ddl.upper() and "TRUNCATE" not in ddl.upper()


class _IndexBQ:
    """Fake BigQuery client for build_indexes: IVF min-row rejection on embedding_multimodal only."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, job_config: Any = None) -> Any:
        from google.api_core.exceptions import BadRequest

        self.queries.append(sql)
        if "CREATE VECTOR INDEX" in sql and "(embedding_multimodal)" in sql:
            raise BadRequest("Query error: Total rows 1480 is smaller than min allowed 5000 for CREATE VECTOR INDEX query with the IVF index type.")
        return SimpleNamespace(result=lambda timeout=None: [], job_id="j")

    def get_table(self, table: Any) -> Any:
        return SimpleNamespace(num_bytes=900_000_000, num_rows=7100)


def test_build_indexes_creates_the_multimodal_vector_index_and_reports_ivf_rejection_verbatim() -> None:
    bq = _IndexBQ()
    loader = lb.BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=bq, sleep=lambda s: None, monotonic=iter(range(0, 10_000, 5)).__next__)
    states = loader.build_indexes(("embedding", "embedding_multimodal"))
    assert any("ON `test-project.rag_arena.document_chunks_v3`(embedding_multimodal)" in q for q in bq.queries)
    mm = states["document_chunks_v3_mm_vec_idx"]
    assert mm.startswith("NOT CREATED (1480 rows on embedding_multimodal; BigQuery IVF vector index needs >= 5000")
    assert "Total rows 1480 is smaller than min allowed 5000 for CREATE VECTOR INDEX" in mm and "brute-force" in mm
    assert set(states) == {"document_chunks_v3_vec_idx", "document_chunks_v3_mm_vec_idx", "document_chunks_v3_search_idx"}


def test_rrf_sql_targets_embedding_multimodal_on_the_multimodal_tier_and_selects_provenance() -> None:
    sql = build_hybrid_rrf_sql("demo-proj", "rag_arena", "document_chunks_v3", embedding_column="embedding_multimodal", tier_profile="multimodal", include_solicitation_filter=True)
    assert "(SELECT chunk_id, embedding_multimodal FROM `demo-proj.rag_arena.document_chunks_v3` WHERE tier_profile = 'multimodal' AND solicitation_id = @solicitation_id)" in sql
    assert "'embedding_multimodal'," in sql and "(SELECT @query_embedding AS embedding_multimodal)" in sql
    assert sql.count("tier_profile = 'multimodal'") == 3 and "c.layout_source" in sql and "c.bbox_source" in sql


# ---------------------------------------------------------------- authority headers


def test_authority_headers_put_the_latest_amendment_first_and_mark_superseded_base_sections() -> None:
    base = _mm_chunk("base_clin")
    amd1 = _mm_chunk("amd1", amendment=1, amends="CLIN-0004-AB,SEC-4.2", section="AMD-0001", element="narrative_text", eff="2026-02-01")
    amd2 = _mm_chunk("amd2", amendment=2, amends="SEC-4.2", section="AMD-0002", element="narrative_text", eff="2026-03-01")
    diagram = _mm_chunk("diag", section="ARCH-TOPOLOGY-01", element="diagram")
    assert [c.chunk_id for c in authority_order([base, amd1, diagram, amd2])] == ["amd2", "amd1", "base_clin", "diag"]
    assert authority_header(amd1) == "[AMENDMENT 0001 · effective 2026-02-01 · SUPERSEDES CLIN-0004-AB,SEC-4.2] [TEXT] amd1.pdf · p.1 · AMD-0001"
    context, headers, mix = build_authority_context([base, amd1, diagram, amd2])
    assert context.index("[AMENDMENT 0002") < context.index("[AMENDMENT 0001") < context.index("[BASE · PRICING_CLIN")
    assert headers[2]["header"].startswith("[BASE · PRICING_CLIN · effective 2026-01-15 · SUPERSEDED BY AMENDMENT 0001] [TABLE] base_clin.pdf")
    assert headers[2]["superseded_by"] == [1] and headers[0]["supersedes"] == ["SEC-4.2"]
    assert "[DIAGRAM transcription] diag.pdf" in headers[3]["header"] and mix == {"narrative_text": 2, "table": 1, "diagram": 1}


def test_element_intent_detects_table_diagram_and_amendment_keywords() -> None:
    intents, matches = detect_element_intent("Compare the current amended hourly rate for CLIN-0004-AB vs the failover topology diagram")
    assert intents == ("table", "diagram", "amendment")
    assert dict(matches)["table"] == ("rate", "clin", "hourly") and "amended" in dict(matches)["amendment"]
    assert detect_element_intent("Who signed the agreement?") == ((), ())


# ---------------------------------------------------------------- engine stage payloads


def _l3_row(idx: int, *, element: str, amendment: int = 0, amends: str | None = None, layout: str = "documentai_layout") -> dict[str, Any]:
    return {
        "chunk_id": f"sol01_doc{idx}_p1_e{idx}_multimodal", "solicitation_id": "solicitation_01", "doc_name": f"0{idx}_Doc.pdf",
        "doc_type": "AMENDMENT" if amendment else "PRICING_CLIN", "element_type": element, "section_id": "CLIN-0004-AB" if not amendment else "AMD-0001",
        "amendment_number": amendment, "effective_date": "2026-02-01" if amendment else "2026-01-15", "amends_section": amends,
        "page_number": 1, "bbox": {"page": 1, "x0": 0.07, "y0": 0.15, "x1": 0.76, "y1": 0.32}, "content": f"| CLIN | Rate |\n| CLIN-0004-AB | ${200 + idx}/hr |",
        "layout_source": layout, "bbox_source": "pdf_text_layer", "vector_rank": idx, "vector_distance": 0.1 * idx,
        "lexical_rank": idx if idx % 2 else None, "lexical_score": 3 if idx % 2 else None, "rrf_score": round(1 / (60 + idx), 6),
    }


_L3_ROWS = [_l3_row(1, element="table"), _l3_row(2, element="narrative_text", amendment=1, amends="CLIN-0004-AB"), _l3_row(3, element="diagram", layout="gemini_vision")]


def _run_l3(synthesis: str | None = None, rows: list[dict[str, Any]] | None = None):
    from app.core.ranking import RankingClient

    settings = live_settings(enabled_levels=("level3_multimodal",))
    emitter = RecordingStageEmitter(run_id="run_l3", engine="level3_multimodal")

    def rank_post(url: str, **kw: Any) -> httpx.Response:
        ids = [r["id"] for r in kw["json"]["records"]]
        return httpx.Response(200, json={"records": [{"id": rid, "score": 0.9 - 0.1 * i} for i, rid in enumerate(ids)]}, request=httpx.Request("POST", url))

    ranking = RankingClient(project_id=_PROJECT, model="semantic-ranker-default@latest", attempt_timeout_seconds=5.0, budget_seconds=10.0,
                            token_provider=lambda: "tok", sleep=lambda s: None, post=rank_post)
    spec = load_catalog(settings).get(synthesis) if synthesis else None
    genai, bq = FakeGenaiClient(), FakeBigQueryClient(rows=_L3_ROWS if rows is None else rows)
    from unittest.mock import patch

    with fake_clients(genai, bq), patch("app.engines.level3_multimodal.get_genai_client", lambda s, location=None: genai), \
            patch("app.engines.level3_multimodal.get_bigquery_client", lambda s: bq):
        engine = Level3MultimodalEngine(settings=settings, synthesis_model=spec, ranking_client=ranking)
        try:
            result = run(engine.run_query("What is the current amended hourly rate for CLIN-0004-AB?", question_id="q", solicitation_filter=None, emitter=emitter))
        except Exception as exc:  # returned for fail-loud assertions
            return exc, emitter, genai, bq
    return result, emitter, genai, bq


def test_level3_engine_emits_every_stage_with_multimodal_payload_contract() -> None:
    result, emitter, genai, bq = _run_l3()
    started = [e["stage"] for e in emitter.events if e["event"] == "stage_started"]
    assert started == ["query_prep", "embed_query", "retrieve", "rerank", "synthesize"]
    done = {e["stage"]: e for e in emitter.events if e["event"] == "stage_completed"}
    assert done["query_prep"]["payload"]["element_intent"] == ["table", "amendment"]
    assert done["embed_query"]["payload"]["task_type"] == "RETRIEVAL_QUERY"
    retrieve = done["retrieve"]["payload"]
    assert retrieve["embedding_column"] == "embedding_multimodal" and retrieve["element_types"] == {"table": 1, "narrative_text": 1, "diagram": 1}
    cand = retrieve["candidates"][1]
    assert {k: cand[k] for k in ("element_type", "layout_source", "amendment_number", "amends_section")} == {
        "element_type": "narrative_text", "layout_source": "documentai_layout", "amendment_number": 1, "amends_section": "CLIN-0004-AB"}
    assert "VECTOR_SEARCH(\n    (SELECT chunk_id, embedding_multimodal" in bq.queries[0][0]
    synth = done["synthesize"]["payload"]
    assert synth["authority_headers"][0]["header"].startswith("[AMENDMENT 0001 · effective 2026-02-01 · SUPERSEDES CLIN-0004-AB]")
    assert synth["element_mix"] == {"narrative_text": 1, "table": 1, "diagram": 1}
    prompt = next(kw for name, kw in genai.calls if name == "generate_content")["contents"]
    assert "[DIAGRAM transcription]" in prompt and "SUPERSEDED BY AMENDMENT 0001" in prompt and "the amendment's terms govern" in prompt
    cites = [c.to_dict() for c in result.citations]
    assert {c["layout_source"] for c in cites} == {"documentai_layout", "gemini_vision"} and all(c["bbox_source"] == "pdf_text_layer" for c in cites)
    assert result.flags.layout_source == "documentai_layout+gemini_vision" and result.flags.reranker_used


def test_level3_accepts_any_catalog_synthesis_model_and_fails_loud_on_empty_tier() -> None:
    settings = live_settings(enabled_levels=("level3_multimodal",))
    models = load_catalog(settings).select("claude-opus-5-5", "gemini-4-argon")
    assert build_engine("level3_multimodal", settings, models).synthesis_model.id == "claude-opus-5-5"
    exc, emitter, _, _ = _run_l3(rows=[])
    assert isinstance(exc, RuntimeError) and "run the Level 3 ingestion job" in str(exc)
    assert [e["event"] for e in emitter.events if e.get("stage") == "retrieve"] == ["stage_started", "stage_failed"]


# ---------------------------------------------------------------- ingestion job (fail loud, payload contract)


class _FakeLoader:
    table_fqn = "test-project.rag_arena.document_chunks_v3"

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.index_columns: tuple[str, ...] | None = None
        self.runs: list[dict[str, Any]] = []

    def ensure_schema(self) -> None: ...

    def load_rows(self, rows: list[dict[str, Any]]) -> int:
        self.rows = rows
        return len(rows)

    def build_indexes(self, embedding_columns: tuple[str, ...] = ("embedding",)) -> dict[str, str]:
        self.index_columns = embedding_columns
        return {"document_chunks_v3_mm_vec_idx": "NOT CREATED (fake)"}

    def count_by_tier(self) -> dict[str, int]:
        return {"multimodal": len(self.rows)}

    def index_states(self) -> dict[str, str]:
        return {}

    def write_ingest_run(self, row: dict[str, Any]) -> None:
        self.runs.append(row)

    def recent_ingest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return []


class _FakeLayoutParser:
    processor_id = "abc123"
    processor_version = "pretrained-layout-parser-v1.5-2025-08-25"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.docs: list[str] = []

    def request_summary(self) -> dict[str, Any]:
        return {"processor_id": self.processor_id}

    def parse(self, doc_name: str, data: bytes) -> Any:
        from app.ingestion.layout_documentai import DocLayout, page_stats

        self.docs.append(doc_name)
        if self.fail:
            raise DocumentAIHTTPError(503, "Document AI POST failed after 3 attempts: HTTP 503")
        elements, skipped = blocks_to_elements([_text("1", "EXHIBIT", "heading-1", children=[
            _text("2", "Architecture Specification ARCH-TOPOLOGY-01 (Multi-Region Cloud Network):"),
            _table("3", ["CLIN", "Rate"], [["CLIN-0004-AB", "$210.00/hr"]]),
            {"blockId": "4", "imageBlock": {"imageText": "VECTOR DIAGRAM"}, "pageSpan": {"pageStart": 1, "pageEnd": 1}},
        ])])
        return DocLayout(doc_name=doc_name, pages=1, elements=tuple(elements), page_stats=page_stats(elements, 1), requests=1, attempts=1, docai_ms=42, skipped_blocks=skipped)


class _FakeVision:
    model = "gemini-4-argon"

    def __init__(self, answer: str = _VISION_JSON) -> None:
        self.answer = answer

    def request_summary(self) -> dict[str, Any]:
        return {"model": self.model}

    def transcribe(self, png: bytes, page: int, ref: str) -> VisionResult:
        assert png.startswith(b"\x89PNG")
        spec = parse_vision_response(self.answer, ref)
        return VisionResult(page_number=page, spec=spec, model=self.model, prompt_tokens=1700,
                            output_tokens=300, thinking_tokens=500, duration_ms=7, attempts=1, image_bytes=len(png))


class _FakeMMEmbedder:
    model = "gemini-embedding-2"
    task_type = "RETRIEVAL_DOCUMENT"
    input_format = "fake"

    def __init__(self) -> None:
        self.items: list[EmbedItem] = []

    def embed_items(self, items: list[EmbedItem]) -> list[list[float]]:
        self.items.extend(items)
        return [[0.01] * 8 for _ in items]


class _TextEmbedder:
    model = "gemini-embedding-2"

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("the multimodal tier must not use the text embedder")


def _temp_corpus() -> Path:
    root = Path(tempfile.mkdtemp(prefix="rag_arena_l3_"))
    sol = root / "solicitation_01_cloud_infrastructure"
    sol.mkdir()
    shutil.copy(_EXHIBIT, sol / _EXHIBIT.name)
    return root


def _run_job(backends: IngestBackends) -> tuple[Any, list[dict[str, Any]]]:
    settings = live_settings(enabled_levels=("level3_multimodal",))
    runner = IngestJobRunner(settings, backend_factory=lambda s: backends, corpus_dir=_temp_corpus())

    async def scenario():
        job = runner.start("level3_multimodal")
        await runner._tasks[job.job_id]
        return job

    job = run(scenario())
    return job, job.events


def test_multimodal_job_without_documentai_fails_at_parse_and_never_falls_back_to_pymupdf() -> None:
    loader = _FakeLoader()
    job, events = _run_job(IngestBackends(embedder=_TextEmbedder(), loader=loader))
    failed = [e for e in events if e["event"] in ("stage_failed", "job_failed")]
    assert job.status == "failed" and failed[0]["stage"] == "parse" and "DOCAI_PROCESSOR_ID" in failed[0]["error"]
    assert "no PyMuPDF fallback" in failed[0]["error"] and not loader.rows
    assert not any(e["event"] == "stage_started" and e["stage"] == "chunk" for e in events)
    job2, events2 = _run_job(IngestBackends(embedder=_TextEmbedder(), loader=_FakeLoader(), layout_parser=_FakeLayoutParser(fail=True), vision=_FakeVision(), mm_embedder=_FakeMMEmbedder()))
    assert job2.status == "failed" and job2.failed_stage == "parse" and "HTTP 503" in job2.error


def test_multimodal_job_payloads_cover_parse_chunk_embed_load_and_indexes() -> None:
    loader, embedder = _FakeLoader(), _FakeMMEmbedder()
    job, events = _run_job(IngestBackends(embedder=_TextEmbedder(), loader=loader, layout_parser=_FakeLayoutParser(), vision=_FakeVision(), mm_embedder=embedder))
    assert job.status == "completed", job.error
    done = {e["stage"]: e["payload"] for e in events if e["event"] == "stage_completed"}
    parse = done["parse"]
    for key in ("pages", "tables", "diagrams", "vision_pages", "layout_source_counts", "docai_ms", "vision_ms", "processor_version", "processor_version_channel", "diagram_heuristic", "cost_usd"):
        assert key in parse, key
    assert parse["tables"] == 1 and parse["diagrams"] == 1 and parse["processor_version_channel"] == "release candidate (preview)"
    assert parse["vision_pages"][0]["reason"] == "documentai_figure+vector_box_diagram" and parse["vision_pages"][0]["labels_in_text_layer"] == 1.0
    assert parse["layout_source_counts"] == {"documentai_layout": 4, "gemini_vision": 1}
    assert done["chunk"]["element_types"] == {"narrative_text": 1, "table": 1, "diagram": 1} and done["chunk"]["image_items"] == 1
    assert done["embed"]["column"] == "embedding_multimodal" and done["embed"]["image_items"] == 1
    assert loader.index_columns == ("embedding", "embedding_multimodal")
    rows = {r["element_type"]: r for r in loader.rows}
    assert rows["diagram"]["layout_source"] == "gemini_vision" and rows["table"]["layout_source"] == "documentai_layout"
    assert rows["table"]["section_id"] == "ARCH-TOPOLOGY-01" and rows["narrative_text"]["bbox_source"] == "pdf_text_layer"
    assert rows["table"]["bbox_source"] == "page"  # the fake table text is not on the real page → never a guessed box
    assert done["chunk"]["bbox_sources"] == {"pdf_text_layer": 1, "page": 2}
    assert rows["diagram"]["content"].startswith("EXHIBIT\n[DIAGRAM transcription · gemini-4-argon]")
    assert sum(1 for i in embedder.items if i.image_png) == 1
    assert loader.runs[0]["layout_source"] == "documentai_layout(pretrained-layout-parser-v1.5-2025-08-25)+gemini_vision(gemini-4-argon)"
    assert any(e["event"] == "doc_progress" and e.get("step") == "documentai" and e.get("page_counts") for e in events)


# ---------------------------------------------------------------- architecture + config


def test_level3_architecture_maps_every_stage_and_names_documentai_and_vision() -> None:
    settings = ArenaSettings(demo_mode="replay", docai_location="eu", vision_model="gemini-3.8-flash")
    desc = get_architecture_descriptor("level3_multimodal", settings)
    mapped = {s for n in desc.ingest_nodes for s in n.stage_ids}
    assert set(CANONICAL_INGEST_STAGES) <= mapped
    parse_nodes = {n.id: n for n in desc.ingest_nodes if "parse" in n.stage_ids}
    assert set(parse_nodes) == {"docai", "vision"} and parse_nodes["docai"].region == "eu"
    assert parse_nodes["vision"].service == "Vertex AI · gemini-3.8-flash (vision)"
    assert {s for n in desc.nodes for s in n.stage_ids} >= set(Level3MultimodalEngine.stages)
    dumped = json.dumps(desc.to_dict())
    assert "{DOCAI_LOCATION}" not in dumped and "{VISION_MODEL}" not in dumped and "embedding_multimodal" in dumped


def test_level3_settings_parse_and_validate_docai_keys() -> None:
    s = get_settings({"DOCAI_LOCATION": "us", "DOCAI_PROCESSOR_ID": "973abc", "DOCAI_PROCESSOR_VERSION": "pretrained-layout-parser-v1.5-2025-08-25", "VISION_MODEL": "gemini-4-argon"})
    assert (s.docai_location, s.docai_processor_id, s.docai_timeout_seconds) == ("us", "973abc", 120)
    for bad in ({"DOCAI_PROCESSOR_ID": "projects/1/locations/us/processors/x"}, {"DOCAI_LOCATION": "US EAST"}, {"VISION_MODEL": "claude-opus-5-5"}):
        with pytest.raises(ValueError):
            get_settings(bad)


def test_an_unusable_vision_answer_fails_that_page_loudly_without_inventing_a_diagram_chunk() -> None:
    loader = _FakeLoader()
    no_diagram = json.dumps({"components": [], "notes": ["Only a company logo; no diagram."]})
    job, events = _run_job(IngestBackends(embedder=_TextEmbedder(), loader=loader, layout_parser=_FakeLayoutParser(),
                                          vision=_FakeVision(no_diagram), mm_embedder=_FakeMMEmbedder()))
    assert job.status == "completed", job.error
    parse = next(e["payload"] for e in events if e["event"] == "stage_completed" and e["stage"] == "parse")
    failed = parse["vision_failed_pages"]
    assert parse["diagrams"] == 0 and parse["vision_attempted_pages"] == 1 and len(failed) == 1
    assert failed[0]["error_type"] == "VisionTranscriptionError" and "no labelled components" in failed[0]["error"]
    assert "Only a company logo" in failed[0]["error"]
    assert any(e["event"] == "doc_progress" and e.get("step") == "gemini_vision" and e.get("status") == "failed" for e in events)
    assert not any(r["element_type"] == "diagram" for r in loader.rows)


def test_second_vector_index_conflict_is_reported_verbatim_without_leaking_the_project_id() -> None:
    from google.api_core.exceptions import Conflict

    class _OneIndexBQ(_IndexBQ):
        def query(self, sql: str, job_config: Any = None) -> Any:
            if "CREATE VECTOR INDEX" in sql and "(embedding_multimodal)" in sql:
                self.queries.append(sql)
                raise Conflict("Already Exists: VectorIndex Table 'test-project:rag_arena.document_chunks_v3' already has a vector index on it, so can't create a new vector index.")
            return super().query(sql, job_config)

    loader = lb.BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=_OneIndexBQ(), sleep=lambda s: None, monotonic=iter(range(0, 10_000, 5)).__next__)
    mm = loader.build_indexes(("embedding", "embedding_multimodal"))["document_chunks_v3_mm_vec_idx"]
    assert mm.startswith("NOT CREATED (BigQuery allows one vector index per table") and "brute-force" in mm
    assert "already has a vector index on it, so can't create a new vector index." in mm and "test-project" not in mm

    class _FirstIndexConflictBQ(_IndexBQ):
        def query(self, sql: str, job_config: Any = None) -> Any:
            if "CREATE VECTOR INDEX" in sql:
                raise Conflict("Already Exists: VectorIndex Table 'x' already has a vector index on it, so can't create a new vector index.")
            return super().query(sql, job_config)

    with pytest.raises(Conflict):
        lb.BigQueryLiveLoader(live_settings(bq_table="document_chunks_v3"), client=_FirstIndexConflictBQ(), sleep=lambda s: None).build_indexes()
