import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "data" / "sample_docs" / "procurement_corpus"
BENCHMARK_FILE = PROJECT_ROOT / "data" / "golden_benchmarks" / "procurement_benchmark.json"
REPLAY_FILE = PROJECT_ROOT / "data" / "golden_benchmarks" / "precomputed_runs.json"


def test_interconnected_solicitation_pdfs_exist():
    expected_pdfs = [
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "01_RFP_Base_Solicitation.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "02_Statement_of_Work_SOW.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "03_Technical_Exhibit_Architecture.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "04_Pricing_Schedule_CLINs.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "05_SLA_and_QASP_Requirements.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "06_Amendment_0001_Scope_Update.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "07_Amendment_0002_Vendor_QA.pdf",
        CORPUS_DIR / "solicitation_01_cloud_infrastructure" / "08_Vendor_A_Proposal_Redacted.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "01_RFP_Data_Platform.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "02_Architecture_Requirements.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "03_Pricing_and_Volume_Tiers.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "04_Compliance_and_SOC2_Matrix.pdf",
        CORPUS_DIR / "solicitation_02_data_analytics_platform" / "05_Amendments_and_Addenda.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "01_RFP_Security_Operations.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "02_Zero_Trust_Architecture_Specs.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "03_Incident_Response_SLAs.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "04_Pricing_Labor_Categories.pdf",
        CORPUS_DIR / "solicitation_03_cybersecurity_operations" / "05_Master_Services_Agreement_MSA.pdf",
    ]
    for pdf_path in expected_pdfs:
        assert pdf_path.exists(), f"Missing required solicitation PDF: {pdf_path}"
        raw = pdf_path.read_bytes()
        assert raw.startswith(b"%PDF-"), f"Invalid PDF header in {pdf_path}"
        assert len(raw) > 500, f"PDF too small: {pdf_path}"


def test_golden_benchmark_schema_and_categories():
    assert BENCHMARK_FILE.exists(), "Missing procurement_benchmark.json"
    data = json.loads(BENCHMARK_FILE.read_text())
    assert "questions" in data
    questions = data["questions"]
    assert len(questions) >= 8
    categories = {q["failure_mode_category"] for q in questions}
    assert {
        "exact_clause_and_clin",
        "table_matrix_extraction",
        "diagram_topology",
        "multi_hop_amendment_and_math",
    }.issubset(categories)
