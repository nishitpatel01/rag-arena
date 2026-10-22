#!/usr/bin/env python3
"""Deterministic Procurement Corpus Compiler for The RAG Arena.

Generates 18 interconnected multi-page PDF 1.4 documents across 3 enterprise
solicitations containing narrative clauses, multi-column CLIN tables, SLA
penalty matrices, embedded vector architecture diagrams, and superseding
contract amendments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class PageSpec:
    """Specification for a single page inside a generated solicitation PDF."""

    heading: str
    section_id: str
    element_type: str
    lines: Sequence[str]
    include_diagram: bool = False
    diagram_title: str = ""
    diagram_nodes: Sequence[str] = ()


@dataclass(frozen=True)
class DocumentSpec:
    """Specification for a multi-page procurement PDF document."""

    solicitation_folder: str
    filename: str
    solicitation_id: str
    doc_type: str
    amendment_number: int
    effective_date: str
    amends_section: str
    pages: Sequence[PageSpec]


def _escape_pdf_text(raw: str) -> str:
    """Escape PDF literal string special characters."""
    if not isinstance(raw, str):
        raise TypeError("PDF text input must be a string")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_diagram_commands(title: str, nodes: Sequence[str]) -> list[str]:
    """Build deterministic PDF vector drawing operators for architecture diagrams."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Diagram title must be a non-empty string")
    if len(nodes) > 12:
        raise ValueError("Diagram node count exceeds bounded limit of 12")

    cmds = [
        "q",
        "0.93 0.96 0.99 rg",
        "48 210 516 175 re f",
        "0.18 0.36 0.62 RG",
        "1.5 w",
        "48 210 516 175 re S",
        # Dark ink for text: without it the title/labels inherit the near-white box fill and are invisible when rendered.
        "0.10 0.16 0.28 rg",
        "BT /F2 10 Tf 60 368 Td (" + _escape_pdf_text(f"VECTOR DIAGRAM: {title}") + ") Tj ET",
    ]
    for idx, node in enumerate(nodes[:6]):
        col = idx % 3
        row = idx // 3
        x = 64 + col * 165
        y = 305 - row * 68
        cmds.extend([
            "0.98 0.99 1.00 rg",
            f"{x} {y} 145 42 re f",
            "0.22 0.45 0.72 RG",
            "1.0 w",
            f"{x} {y} 145 42 re S",
            "0.10 0.16 0.28 rg",
            f"BT /F1 8 Tf {x + 8} {y + 18} Td ({_escape_pdf_text(node)}) Tj ET",
        ])
        if col < 2 and idx + 1 < len(nodes):
            line_x0 = x + 145
            line_y = y + 21
            cmds.append(f"{line_x0} {line_y} m {line_x0 + 20} {line_y} l S")
    cmds.append("Q")
    return cmds


def _build_page_stream(doc: DocumentSpec, page: PageSpec, page_idx: int, total_pages: int) -> bytes:
    """Compile a single PDF page content stream with bounded line count."""
    if page_idx < 1 or total_pages < 1 or page_idx > total_pages:
        raise ValueError(f"Invalid page bounds: {page_idx}/{total_pages}")
    if len(page.lines) > 60:
        raise ValueError("Page line count exceeds maximum of 60 lines")

    ops: list[str] = [
        "q",
        "0.12 0.22 0.38 rg",
        "36 742 540 28 re f",
        "1 1 1 rg",
        f"BT /F2 11 Tf 46 752 Td ({_escape_pdf_text(doc.solicitation_id + ' | ' + doc.filename)}) Tj ET",
        "0 0 0 rg",
        f"BT /F2 12 Tf 46 718 Td ({_escape_pdf_text(page.heading)}) Tj ET",
        (
            "BT /F3 8 Tf 46 702 Td ("
            + _escape_pdf_text(
                f"DOC_TYPE={doc.doc_type} | SECTION={page.section_id} | ELEMENT={page.element_type} "
                f"| AMENDMENT={doc.amendment_number} | EFFECTIVE={doc.effective_date} "
                f"| AMENDS={doc.amends_section or 'NONE'}"
            )
            + ") Tj ET"
        ),
        "0.75 0.78 0.82 RG 0.8 w 46 694 m 566 694 l S",
    ]

    y_cursor = 676
    for line in page.lines:
        font = "/F3" if "|" in line or line.startswith("CLIN-") else "/F1"
        size = 8 if font == "/F3" else 9
        ops.append(f"BT {font} {size} Tf 46 {y_cursor} Td ({_escape_pdf_text(line)}) Tj ET")
        y_cursor -= 14

    if page.include_diagram:
        ops.extend(_build_diagram_commands(page.diagram_title, page.diagram_nodes))

    footer = f"Page {page_idx} of {total_pages} | {doc.solicitation_id} | Verified Enterprise Procurement Record"
    ops.append(f"BT /F1 8 Tf 46 32 Td ({_escape_pdf_text(footer)}) Tj ET")
    ops.append("Q")
    return "\n".join(ops).encode("latin-1", errors="replace")


def render_pdf_bytes(doc: DocumentSpec) -> bytes:
    """Assemble a deterministic, standards-compliant PDF 1.4 binary with xref table."""
    if not doc.pages or len(doc.pages) > 30:
        raise ValueError("Document must have between 1 and 30 pages")
    if not doc.filename.endswith(".pdf"):
        raise ValueError(f"Document filename must end with .pdf: {doc.filename}")

    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_f1 = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_f2 = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    font_f3 = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    form_xobj = add_obj(
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Length 25 >>\nstream\n0 0 100 100 re S\nendstream"
    )

    page_obj_ids: list[int] = []
    content_obj_ids: list[int] = []
    total_pages = len(doc.pages)

    for idx, page in enumerate(doc.pages, start=1):
        stream_data = _build_page_stream(doc, page, idx, total_pages)
        header = f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
        content_id = add_obj(header + stream_data + b"\nendstream")
        content_obj_ids.append(content_id)

    pages_tree_id = len(objects) + total_pages + 1
    for content_id in content_obj_ids:
        page_dict = (
            f"<< /Type /Page /Parent {pages_tree_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_f1} 0 R /F2 {font_f2} 0 R /F3 {font_f3} 0 R >> "
            f"/XObject << /DiagForm {form_xobj} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        page_obj_ids.append(add_obj(page_dict))

    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    actual_pages_id = add_obj(
        f"<< /Type /Pages /Kids [{kids_str}] /Count {total_pages} >>".encode("ascii")
    )
    catalog_id = add_obj(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>".encode("ascii"))

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for obj_num, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    buf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(buf)


def _solicitation_01_specs() -> list[DocumentSpec]:
    """Return the 8 interconnected PDF specs for Solicitation 01 (Cloud Infrastructure)."""
    folder = "solicitation_01_cloud_infrastructure"
    sol_id = "SOL-2026-CLOUD-01"
    return [
        DocumentSpec(
            solicitation_folder=folder,
            filename="01_RFP_Base_Solicitation.pdf",
            solicitation_id=sol_id,
            doc_type="BASE_RFP",
            amendment_number=0,
            effective_date="2026-01-15",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SECTION 1 & 4: BASE RFP SCOPE, DISASTER RECOVERY & CLIN REFERENCES",
                    section_id="SEC-4.2.1",
                    element_type="narrative_text",
                    lines=[
                        "Solicitation ID: SOL-2026-CLOUD-01 (Enterprise Cloud Hosting & Migration)",
                        "Issuing Office: Enterprise IT Procurement Division | Base Effective Date: 2026-01-15",
                        "Clause SEC-4.2.1 (Disaster Recovery & Continuity Baseline):",
                        "The Contractor shall maintain multi-region cloud infrastructure with an original",
                        "Recovery Time Objective (RTO) of 4 hours (240 minutes) and a Recovery Point Objective",
                        "(RPO) of 1 hour (60 minutes) across Primary Region us-central1 and Failover us-east4.",
                        "Clause SEC-4.3.2 (Key Personnel & Labor Reference):",
                        "Lead architecture oversight is billed under CLIN-0004-AB (Cloud Security Architect)",
                        "at the base solicitation hourly rate of $210/hr for an estimated 2,000 base hours",
                        "($420,000.00 Year 1 ceiling), subject to formal contract amendments.",
                        "All proposals must comply with FedRAMP High and NIST SP 800-53 Rev 5 baselines.",
                    ],
                ),
                PageSpec(
                    heading="SECTION 5: EVALUATION FACTORS & PROPOSAL SUBMISSION INSTRUCTIONS",
                    section_id="SEC-5.1",
                    element_type="narrative_text",
                    lines=[
                        "Factor 1 (Technical Architecture - 40%): Verified via Technical Exhibit 03 diagram.",
                        "Factor 2 (Pricing & CLIN Realism - 35%): Evaluated against Schedule 04 CLIN tables.",
                        "Factor 3 (SLA Commitments & QASP - 25%): Governed by Exhibit 05 penalty matrices.",
                        "Offerors must acknowledge all formal Amendments (0001 and 0002) prior to award.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="02_Statement_of_Work_SOW.pdf",
            solicitation_id=sol_id,
            doc_type="SOW",
            amendment_number=0,
            effective_date="2026-01-15",
            amends_section="",
            pages=[
                PageSpec(
                    heading="STATEMENT OF WORK (SOW): CLOUD MIGRATION WAVES & MILESTONES",
                    section_id="SOW-2.1",
                    element_type="table",
                    lines=[
                        "SOW Section 2.1: Phased Workload Migration & Deliverable Payment Schedule",
                        "| Wave | Target Workloads | Completion | Milestone Payment | Primary CLIN |",
                        "| Wave 1 | Landing Zone, Shared VPC, Cloud HA VPN | Month 3 | $650,000 | CLIN-0001-AA |",
                        "| Wave 2 | Core ERP & 45 Microservices on GKE | Month 7 | $1,200,000 | CLIN-0002-AA |",
                        "| Wave 3 | DR Automation & Security Hardening | Month 12 | $550,000 | CLIN-0004-AB |",
                        "SOW Section 3.2: Contractor shall provide weekly burn-rate telemetry in BigQuery.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="03_Technical_Exhibit_Architecture.pdf",
            solicitation_id=sol_id,
            doc_type="TECHNICAL_EXHIBIT",
            amendment_number=0,
            effective_date="2026-01-15",
            amends_section="",
            pages=[
                PageSpec(
                    heading="TECHNICAL EXHIBIT A: MULTI-REGION VPC & FAILOVER TOPOLOGY",
                    section_id="ARCH-TOPOLOGY-01",
                    element_type="diagram",
                    lines=[
                        "Architecture Specification ARCH-TOPOLOGY-01 (Multi-Region Cloud Network):",
                        "1. Ingress Layer: Global External Application Load Balancer protected by Cloud Armor WAF",
                        "   enforcing OWASP Top 10 preconfigured rules and adaptive DDoS protection.",
                        "2. Hybrid Connectivity: Redundant Cloud HA VPN tunnels (99.99% SLA) terminating on",
                        "   Cloud Router with dynamic BGP ASN 64512 (On-Premises ASN 65001) and MED failover.",
                        "3. Regional Compute: Primary Region us-central1 (GKE Autopilot + Cloud SQL HA) with",
                        "   cross-region replica and automated failover to Secondary Region us-east4.",
                        "4. Data & AI Plane: Private Service Connect (PSC) endpoints in both regions routing",
                        "   privately to BigQuery and Vertex AI without traversing the public internet.",
                    ],
                    include_diagram=True,
                    diagram_title="SOL-2026-CLOUD-01 Multi-Region VPC & HA VPN Topology",
                    diagram_nodes=[
                        "Cloud Armor WAF + Global LB",
                        "Cloud HA VPN (BGP ASN 64512)",
                        "Primary VPC: us-central1",
                        "Failover VPC: us-east4",
                        "Private Service Connect (PSC)",
                        "BigQuery + Vertex AI Endpoints",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="04_Pricing_Schedule_CLINs.pdf",
            solicitation_id=sol_id,
            doc_type="PRICING_CLIN",
            amendment_number=0,
            effective_date="2026-01-15",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SCHEDULE B: CONTRACT LINE ITEM NUMBER (CLIN) PRICING MATRIX",
                    section_id="CLIN-0004-AB",
                    element_type="table",
                    lines=[
                        "Base Solicitation Rate Card (Prior to Amendment 0001 Adjustments):",
                        "| CLIN | Description | Unit | Qty | Base Rate | Year 1 Total |",
                        "| CLIN-0001-AA | Shared VPC & Landing Zone Core | Monthly | 12 | $45,000/mo | $540,000 |",
                        "| CLIN-0002-AA | GKE Enterprise Managed Cluster | Node-Mo | 240 | $1,250/mo | $300,000 |",
                        "| CLIN-0003-AA | Cloud SQL Enterprise Plus HA | Instance | 24 | $3,800/mo | $91,200 |",
                        "| CLIN-0004-AB | Cloud Security Architect Labor | Hour | 2,000 | $210.00/hr | $420,000 |",
                        "| CLIN-0005-AC | Principal SRE / Migration Eng | Hour | 3,000 | $195.00/hr | $585,000 |",
                        "| CLIN-0006-AD | Cloud HA VPN & Interconnect | Monthly | 12 | $8,500/mo | $102,000 |",
                        "| CLIN-0007-AE | Cloud Armor Enterprise Tier | Monthly | 12 | $4,200/mo | $50,400 |",
                        "| CLIN-0008-AD | DR Failover Automation Suite | Annual | 1 | $115,000/yr | $115,000 |",
                        "Note: CLIN-0004-AB base rate is $210/hr for 2,000 hours ($420,000 base Year 1).",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="05_SLA_and_QASP_Requirements.pdf",
            solicitation_id=sol_id,
            doc_type="SLA_MATRIX",
            amendment_number=0,
            effective_date="2026-01-15",
            amends_section="",
            pages=[
                PageSpec(
                    heading="EXHIBIT C: QUALITY ASSURANCE SURVEILLANCE PLAN (QASP) & SLA CREDITS",
                    section_id="SLA-4.2",
                    element_type="table",
                    lines=[
                        "Clause SLA-4.2: Monthly Infrastructure Availability & Service Credit Formula:",
                        "| Tier | Monthly Uptime Percentage | Response Target | Invoice Service Credit |",
                        "| P1 Critical | 99.99% Target (<99.99% to 99.50%) | <= 15 minutes | 10% of Monthly CLIN Fee |",
                        "| P1 Severe Breach | < 99.50% Monthly Availability | <= 15 minutes | 25% of Monthly CLIN Fee |",
                        "| P2 High | < 99.90% Non-Core Services | <= 30 minutes | 5% of Monthly CLIN Fee |",
                        "| P3 Medium | Minor Control Plane Degradation | <= 2 hours | 2% of Monthly CLIN Fee |",
                        "| P4 Low | Cosmetic / Reporting Delay | <= 24 hours | 0% (Corrective Action) |",
                        "Formula: Credit_USD = Monthly_CLIN_Invoice * Credit_Percentage.",
                        "Example: If CLIN-0001-AA ($45,000/mo) drops to 99.40% uptime (<99.50%), the",
                        "applicable P1 Severe Breach credit is 25% ($45,000 * 0.25 = $11,250.00 credit).",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="06_Amendment_0001_Scope_Update.pdf",
            solicitation_id=sol_id,
            doc_type="AMENDMENT",
            amendment_number=1,
            effective_date="2026-03-15",
            amends_section="SEC-4.2.1,CLIN-0004-AB",
            pages=[
                PageSpec(
                    heading="AMENDMENT 0001: MANDATORY RTO/RPO TIGHTENING & CLIN-0004-AB RATE REVISION",
                    section_id="AMD-0001",
                    element_type="narrative_text",
                    lines=[
                        "[AMENDMENT 0001 - SUPERSEDES SEC-4.2.1 AND CLIN-0004-AB]",
                        "Solicitation: SOL-2026-CLOUD-01 | Amendment Number: 0001 | Effective Date: 2026-03-15",
                        "1. SUPERSESSION OF CLAUSE SEC-4.2.1 (DISASTER RECOVERY RTO/RPO):",
                        "   Section SEC-4.2.1 of 01_RFP_Base_Solicitation.pdf is hereby superseded in full.",
                        "   The required Recovery Time Objective (RTO) is tightened from 4 hours to 15 minutes,",
                        "   and the Recovery Point Objective (RPO) is tightened from 1 hour to 0 minutes",
                        "   (synchronous zero-data-loss cross-region replication between us-central1 and us-east4).",
                        "2. SUPERSESSION OF CLIN-0004-AB (CLOUD SECURITY ARCHITECT LABOR RATE):",
                        "   Due to added FedRAMP High Rev 5 zero-data-loss cryptographic verification scope,",
                        "   the CLIN-0004-AB Cloud Security Architect hourly rate in 04_Pricing_Schedule_CLINs.pdf",
                        "   is increased from $210.00/hr to $245.00/hr.",
                        "3. VOLUME DISCOUNT FORMULA FOR CLIN-0004-AB:",
                        "   When contracted CLIN-0004-AB hours exceed 1,500 hours in a contract year, a 5%",
                        "   volume discount applies to the entire CLIN-0004-AB order:",
                        "   For 2,000 base hours: 2,000 * $245.00 * (1 - 0.05) = $465,500.00 net total.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="07_Amendment_0002_Vendor_QA.pdf",
            solicitation_id=sol_id,
            doc_type="AMENDMENT",
            amendment_number=2,
            effective_date="2026-04-02",
            amends_section="SEC-4.3.5",
            pages=[
                PageSpec(
                    heading="AMENDMENT 0002: OFFICIAL VENDOR Q&A AND KMS KEY ROTATION CLARIFICATION",
                    section_id="AMD-0002-QA",
                    element_type="narrative_text",
                    lines=[
                        "[AMENDMENT 0002 - VENDOR QUESTIONS AND ANSWERS (QA-01 TO QA-25)]",
                        "Solicitation: SOL-2026-CLOUD-01 | Amendment Number: 0002 | Effective Date: 2026-04-02",
                        "Q12: What is the mandatory Cloud KMS Customer-Managed Encryption Key (CMEK) rotation",
                        "     period for BigQuery and Cloud SQL volumes under FedRAMP High?",
                        "A12: All CMEK keys in Cloud HSM must enforce an automated 90-day rotation schedule",
                        "     (or 30-day rotation for Tier-0 identity signing keys).",
                        "Q19: Does Amendment 0001 modify the 2,000 base hours on CLIN-0004-AB?",
                        "A19: No. Base quantity remains 2,000 hours; the amended rate is $245/hr with the",
                        "     5% volume discount (>1,500 hours), yielding $465,500.00 for Year 1.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="08_Vendor_A_Proposal_Redacted.pdf",
            solicitation_id=sol_id,
            doc_type="VENDOR_PROPOSAL",
            amendment_number=0,
            effective_date="2026-04-10",
            amends_section="",
            pages=[
                PageSpec(
                    heading="VENDOR A TECHNICAL & PRICE PROPOSAL (REDACTED SUBMISSION)",
                    section_id="PROP-EXEC-01",
                    element_type="narrative_text",
                    lines=[
                        "Submitted in Response to Solicitation SOL-2026-CLOUD-01 (Includes Amendments 0001 & 0002)",
                        "Vendor A proposes a dual-region us-central1 / us-east4 landing zone meeting the",
                        "amended 15-minute RTO and 0-minute RPO mandate via Cloud Spanner & Cloud SQL HA.",
                        "Pricing Acknowledgment: CLIN-0004-AB priced at $245/hr less 5% volume discount",
                        "for 2,000 hours ($465,500.00), matching Amendment 0001.",
                    ],
                ),
            ],
        ),
    ]


def _solicitation_02_specs() -> list[DocumentSpec]:
    """Return the 5 interconnected PDF specs for Solicitation 02 (Data Analytics Platform)."""
    folder = "solicitation_02_data_analytics_platform"
    sol_id = "SOL-2026-DATA-02"
    return [
        DocumentSpec(
            solicitation_folder=folder,
            filename="01_RFP_Data_Platform.pdf",
            solicitation_id=sol_id,
            doc_type="BASE_RFP",
            amendment_number=0,
            effective_date="2026-02-01",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SOLICITATION SOL-2026-DATA-02: ENTERPRISE DATA LAKEHOUSE & AI PLATFORM",
                    section_id="DATA-SLA-3.1",
                    element_type="narrative_text",
                    lines=[
                        "Solicitation ID: SOL-2026-DATA-02 | Base Effective Date: 2026-02-01",
                        "Clause DATA-GOV-1.1: Enterprise BigQuery Lakehouse must enforce Dataplex Universal",
                        "Catalog governance, column-level policy tags, and fine-grained row-level security.",
                        "Clause DATA-SLA-3.1 (Base Streaming Ingestion Latency):",
                        "In the base RFP, end-to-end streaming ingestion latency from Cloud Pub/Sub through",
                        "Apache Beam / Dataflow into BigQuery Silver tables shall not exceed 30 seconds (p99).",
                        "(Note: See 05_Amendments_and_Addenda.pdf Addendum 0001 for updated 5-second SLA).",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="02_Architecture_Requirements.pdf",
            solicitation_id=sol_id,
            doc_type="TECHNICAL_EXHIBIT",
            amendment_number=0,
            effective_date="2026-02-01",
            amends_section="",
            pages=[
                PageSpec(
                    heading="EXHIBIT A: STREAMING PUB/SUB -> DATAFLOW -> BIGQUERY MEDALLION TOPOLOGY",
                    section_id="ARCH-MEDALLION-02",
                    element_type="diagram",
                    lines=[
                        "Architecture Specification ARCH-MEDALLION-02 (Lakehouse Medallion Pipeline):",
                        "1. Ingestion Layer: Global Cloud Pub/Sub topics with schema validation & dead-letter queues.",
                        "2. Stream Processing: Dataflow Streaming Engine (Apache Beam) performing windowing,",
                        "   PII tokenization via Cloud DLP, and Storage Write API exactly-once commits.",
                        "3. Medallion Storage in BigQuery + BigLake Iceberg:",
                        "   - Bronze Layer: Raw append-only change-data-capture (CDC) tables.",
                        "   - Silver Layer: Deduplicated, schema-enforced enterprise entity tables.",
                        "   - Gold Layer: Governed semantic marts & Vertex AI Feature Store embeddings.",
                    ],
                    include_diagram=True,
                    diagram_title="SOL-2026-DATA-02 Pub/Sub -> Dataflow -> BigQuery Medallion Architecture",
                    diagram_nodes=[
                        "Cloud Pub/Sub Ingest",
                        "Dataflow + Cloud DLP",
                        "BigQuery Bronze (Raw)",
                        "BigQuery Silver (Curated)",
                        "BigQuery Gold (Governed)",
                        "Vertex AI Feature Store",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="03_Pricing_and_Volume_Tiers.pdf",
            solicitation_id=sol_id,
            doc_type="PRICING_CLIN",
            amendment_number=0,
            effective_date="2026-02-01",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SCHEDULE B: LAKEHOUSE COMPUTE SLOTS & STORAGE VOLUME TIERS (CLIN-1001 TO 1006)",
                    section_id="CLIN-1001",
                    element_type="table",
                    lines=[
                        "| CLIN | Service Component | Unit | Monthly Qty | Base Unit Rate | Annual Total |",
                        "| CLIN-1001 | BigQuery Enterprise Plus Baseline | 500 Slots | 12 mos | $26,000/mo | $312,000 |",
                        "| CLIN-1002 | BigQuery Autoscaling Reservation | 1,000 Slots | 12 mos | $48,000/mo | $576,000 |",
                        "| CLIN-1003 | Dataflow Streaming Compute | vCPU-Hr | 50,000 | $0.069/hr | $41,400 |",
                        "| CLIN-1004 | Active Lakehouse Storage (Base) | Terabyte | 6,000 TB | $20.00/TB/mo | $1,440,000 |",
                        "| CLIN-1005 | Dataplex Governance & Lineage | Monthly | 12 mos | $6,500/mo | $78,000 |",
                        "| CLIN-1006 | Lead Data Platform Architect | Hour | 1,500 hrs | $225.00/hr | $337,500 |",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="04_Compliance_and_SOC2_Matrix.pdf",
            solicitation_id=sol_id,
            doc_type="SLA_MATRIX",
            amendment_number=0,
            effective_date="2026-02-01",
            amends_section="",
            pages=[
                PageSpec(
                    heading="EXHIBITB: SOC2 TYPE II, HIPAA & VPC SERVICE CONTROLS COMPLIANCE MATRIX",
                    section_id="CTRL-SOC2-MATRIX",
                    element_type="table",
                    lines=[
                        "| Control ID | Framework | Requirement | GCP Implementation | Mandatory |",
                        "| CTRL-SOC2-CC6.1 | SOC2 Type II | Logical Access Boundary | VPC Service Controls (VPC-SC) | YES |",
                        "| CTRL-SOC2-CC6.7 | SOC2 Type II | Encryption in Transit/Rest | Cloud KMS CMEK + TLS 1.3 | YES |",
                        "| CTRL-HIPAA-164.312 | HIPAA | PHI De-Identification | Cloud DLP Streaming Redaction | YES |",
                        "| CTRL-ISO-A.12.4 | ISO 27001 | Immutable Audit Telemetry | Cloud Audit Logs -> Locked GCS | YES |",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="05_Amendments_and_Addenda.pdf",
            solicitation_id=sol_id,
            doc_type="AMENDMENT",
            amendment_number=1,
            effective_date="2026-03-20",
            amends_section="DATA-SLA-3.1,CLIN-1004",
            pages=[
                PageSpec(
                    heading="ADDENDUM 0001: REAL-TIME 5-SECOND STREAMING SLA & STORAGE VOLUME DISCOUNT",
                    section_id="DATA-AMD-0001",
                    element_type="narrative_text",
                    lines=[
                        "[AMENDMENT 0001 - SUPERSEDES DATA-SLA-3.1 AND CLIN-1004]",
                        "Solicitation: SOL-2026-DATA-02 | Addendum Number: 0001 | Effective Date: 2026-03-20",
                        "1. STREAMING INGESTION LATENCY OVERRIDE (SUPERSEDES DATA-SLA-3.1):",
                        "   To support real-time fraud detection, Clause DATA-SLA-3.1 in 01_RFP_Data_Platform.pdf",
                        "   is amended to reduce maximum end-to-end Pub/Sub -> Dataflow -> BigQuery Silver",
                        "   streaming latency from 30 seconds down to 5 seconds (p99 <= 5.0 seconds) using",
                        "   BigQuery Storage Write API committed streams.",
                        "2. STORAGE TIER VOLUME DISCOUNT (SUPERSEDES CLIN-1004):",
                        "   For Active Lakehouse Storage exceeding 5 Petabytes (5,000 TB), CLIN-1004 receives",
                        "   a 15% volume discount, reducing the effective rate from $20.00/TB/mo to $17.00/TB/mo",
                        "   ($1,224,000 annual total for 6,000 TB instead of $1,440,000).",
                    ],
                ),
            ],
        ),
    ]


def _solicitation_03_specs() -> list[DocumentSpec]:
    """Return the 5 interconnected PDF specs for Solicitation 03 (Cybersecurity Operations)."""
    folder = "solicitation_03_cybersecurity_operations"
    sol_id = "SOL-2026-SEC-03"
    return [
        DocumentSpec(
            solicitation_folder=folder,
            filename="01_RFP_Security_Operations.pdf",
            solicitation_id=sol_id,
            doc_type="BASE_RFP",
            amendment_number=0,
            effective_date="2026-02-10",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SOLICITATION SOL-2026-SEC-03: ZERO TRUST ARCHITECTURE & 24/7 MANAGED SOC",
                    section_id="SEC-OPS-1.1",
                    element_type="narrative_text",
                    lines=[
                        "Solicitation ID: SOL-2026-SEC-03 | Base Effective Date: 2026-02-10",
                        "Scope: 24x7x365 Managed Detection and Response (MDR), Google SecOps (Chronicle SIEM/SOAR),",
                        "and enterprise-wide NIST SP 800-207 Zero Trust Architecture implementation.",
                        "All labor categories in Schedule 04 are subject to the Master Services Agreement (MSA)",
                        "rate cap provisions in Document 05_Master_Services_Agreement_MSA.pdf.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="02_Zero_Trust_Architecture_Specs.pdf",
            solicitation_id=sol_id,
            doc_type="TECHNICAL_EXHIBIT",
            amendment_number=0,
            effective_date="2026-02-10",
            amends_section="",
            pages=[
                PageSpec(
                    heading="EXHIBIT A: NIST SP 800-207 ZERO TRUST CONTROL PLANE & PEP TOPOLOGY",
                    section_id="ZTA-NIST-800-207",
                    element_type="diagram",
                    lines=[
                        "Architecture Specification ZTA-NIST-800-207 (Zero Trust Enforcement Flow):",
                        "1. Control Plane: Policy Engine (PE) evaluates continuous device posture, BeyondCorp",
                        "   context-aware access levels, and Chronicle threat intelligence feeds, signaling",
                        "   the Policy Administrator (PA) to mint short-lived mTLS / OIDC session tokens.",
                        "2. Data Plane Policy Enforcement Point (PEP): Identity-Aware Proxy (IAP) and",
                        "    Envoy Service Mesh ingress gateways terminate all untrusted traffic and permit",
                        "   per-request access to protected enclave resources only upon PA verification.",
                        "3. Continuous Telemetry: Every PEP allow/deny decision streams to Google SecOps SIEM.",
                    ],
                    include_diagram=True,
                    diagram_title="SOL-2026-SEC-03 NIST SP 800-207 Zero Trust Control Plane & PEP",
                    diagram_nodes=[
                        "Untrusted Subject + Device",
                        "Policy Engine (PE) Context",
                        "Policy Administrator (PA)",
                        "Policy Enforcement Point (PEP)",
                        "BeyondCorp IAP + mTLS Mesh",
                        "Protected Enterprise Enclave",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="03_Incident_Response_SLAs.pdf",
            solicitation_id=sol_id,
            doc_type="SLA_MATRIX",
            amendment_number=0,
            effective_date="2026-02-10",
            amends_section="",
            pages=[
                PageSpec(
                    heading="EXHIBIT B: SOC INCIDENT RESPONSE SEVERITY MATRIX & PENALTY CREDITS",
                    section_id="SLA-SEC-3.1",
                    element_type="table",
                    lines=[
                        "Clause SLA-SEC-3.1: Mandatory 24/7 SOC Triage, Containment & Penalty Matrix:",
                        "| Severity | Definition | Initial Triage SLA | Containment SLA | Penalty Credit |",
                        "| P1 Critical | Active Ransomware / Root Breach | <= 15 minutes | <= 60 minutes | 20% Monthly Fee |",
                        "| P2 High | Privileged Account Compromise | <= 30 minutes | <= 4 hours | 10% Monthly Fee |",
                        "| P3 Medium | Isolated Malware / Policy Alert | <= 2 hours | <= 24 hours | 5% Monthly Fee |",
                        "| P4 Low | Informational / Posture Drift | <= 24 hours | <= 72 hours | 2% Monthly Fee |",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="04_Pricing_Labor_Categories.pdf",
            solicitation_id=sol_id,
            doc_type="PRICING_CLIN",
            amendment_number=0,
            effective_date="2026-02-10",
            amends_section="",
            pages=[
                PageSpec(
                    heading="SCHEDULE B: CYBERSECURITY OPERATIONS LABOR RATE CARD (BASE SCHEDULE)",
                    section_id="CLIN-2001",
                    element_type="table",
                    lines=[
                        "Base Labor Rate Schedule (Prior to MSA Amendment 0001 Rate Cap):",
                        "| CLIN | Labor Category | Clearance | Annual Hours | Base Hourly Rate | Extended Total |",
                        "| CLIN-2001 | Cloud Security Architect | Top Secret / SCI | 2,000 | $265.00/hr | $530,000 |",
                        "| CLIN-2002 | SOC Incident Commander | Secret | 2,000 | $240.00/hr | $480,000 |",
                        "| CLIN-2003 | Senior Threat Hunter (SOC L3) | Secret | 4,000 | $185.00/hr | $740,000 |",
                        "| CLIN-2004 | Incident Response Analyst (L2) | Public Trust | 6,000 | $145.00/hr | $870,000 |",
                        "| CLIN-2005 | Tier-1 Security Analyst (L1) | Public Trust | 8,000 | $110.00/hr | $880,000 |",
                        "Note: CLIN-2001 base schedule rate is $265.00/hr before MSA Amendment 0001 rate cap.",
                    ],
                ),
            ],
        ),
        DocumentSpec(
            solicitation_folder=folder,
            filename="05_Master_Services_Agreement_MSA.pdf",
            solicitation_id=sol_id,
            doc_type="MSA",
            amendment_number=1,
            effective_date="2026-03-25",
            amends_section="CLIN-2001",
            pages=[
                PageSpec(
                    heading="MASTER SERVICES AGREEMENT (MSA) & AMENDMENT 0001 RATE CAP OVERRIDE",
                    section_id="MSA-AMD-0001",
                    element_type="narrative_text",
                    lines=[
                        "[AMENDMENT 0001 - SUPERSEDES CLIN-2001 IN 04_Pricing_Labor_Categories.pdf]",
                        "Solicitation: SOL-2026-SEC-03 | MSA Section 9.4 & Amendment 0001 | Effective: 2026-03-25",
                        "1. LIMITATION OF LIABILITY (MSA SECTION 8.1):",
                        "   Aggregate liability under this Agreement is capped at two times (2x) the total",
                        "   annual contract value, excluding willful misconduct or indemnification obligations.",
                        "2. MANDATORY LABOR RATE CAP OVERRIDE (SUPERSEDES CLIN-2001):",
                        "   Notwithstanding the $265.00/hr base rate listed in 04_Pricing_Labor_Categories.pdf,",
                        "   MSA Amendment 0001 establishes a binding enterprise rate ceiling of $250.00/hr for",
                        "   CLIN-2001 (Cloud Security Architect). For 2,000 annual hours, the amended",
                        "   CLIN-2001 total is $500,000.00 (saving $15.00/hr or $30,000.00 annually).",
                    ],
                ),
            ],
        ),
    ]


def build_all_solicitation_pdfs(output_root: Path) -> list[Path]:
    """Compile all 18 interconnected procurement solicitation PDFs into output_root."""
    if not isinstance(output_root, Path):
        raise TypeError("output_root must be a pathlib.Path instance")
    if str(output_root).strip() == "":
        raise ValueError("output_root path cannot be empty")

    specs = [
        *_solicitation_01_specs(),
        *_solicitation_02_specs(),
        *_solicitation_03_specs(),
    ]
    written_paths: list[Path] = []
    for spec in specs:
        target_dir = output_root / spec.solicitation_folder
        target_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = target_dir / spec.filename
        pdf_bytes = render_pdf_bytes(spec)
        pdf_path.write_bytes(pdf_bytes)
        written_paths.append(pdf_path)
    return written_paths


def main() -> int:
    """Entrypoint for generating the deterministic procurement corpus."""
    project_root = Path(__file__).resolve().parents[1]
    corpus_dir = project_root / "data" / "sample_docs" / "procurement_corpus"
    created = build_all_solicitation_pdfs(corpus_dir)
    print(f"Compiled {len(created)} interconnected solicitation PDFs in {corpus_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
