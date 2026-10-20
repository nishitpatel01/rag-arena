#!/usr/bin/env bash
# ==============================================================================
# The RAG Arena: Automated Procurement Corpus Downloader
# Fetches open, authentic enterprise procurement contracts, RFPs, and SLAs
# (CUAD open-access CC-BY 4.0 & public domain government solicitations)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CORPUS_DIR="${PROJECT_ROOT}/data/sample_docs/procurement_corpus"

mkdir -p "${CORPUS_DIR}/solicitation_01_cloud_infrastructure"
mkdir -p "${CORPUS_DIR}/solicitation_02_data_analytics_platform"
mkdir -p "${CORPUS_DIR}/solicitation_03_cybersecurity_operations"

echo "================================================================="
echo "The RAG Arena: Preparing Enterprise Procurement Corpus"
echo "Target directory: ${CORPUS_DIR}"
echo "================================================================="

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "${TEMP_DIR}"' EXIT

ZIP_PATH="${TEMP_DIR}/CUAD_v1.zip"

echo "--> [1/4] Downloading open commercial procurement agreements from Zenodo (CUAD)..."
curl -fsSL --connect-timeout 15 --max-time 300 "https://zenodo.org/records/4595826/files/CUAD_v1.zip" -o "${ZIP_PATH}"

echo "--> [2/4] Extracting authentic procurement PDFs into solicitation categories..."
python3 - <<EOF
import zipfile, os, shutil

dest_base = "${CORPUS_DIR}"
sol_1 = os.path.join(dest_base, "solicitation_01_cloud_infrastructure")
sol_2 = os.path.join(dest_base, "solicitation_02_data_analytics_platform")
sol_3 = os.path.join(dest_base, "solicitation_03_cybersecurity_operations")

with zipfile.ZipFile("${ZIP_PATH}") as z:
    for n in z.namelist():
        if not n.endswith(".pdf"):
            continue
        filename = os.path.basename(n)
        parts = n.split("/")
        if len(parts) <= 3:
            continue
        cat = parts[3]
        target = None
        if cat in ["Hosting", "Maintenance"] and len(os.listdir(sol_1)) < 9:
            target = sol_1
        elif cat in ["Service", "Development"] and len(os.listdir(sol_2)) < 9:
            target = sol_2
        elif cat in ["Outsourcing", "License_Agreements", "Supply"] and len(os.listdir(sol_3)) < 9:
            target = sol_3
        
        if target:
            with z.open(n) as src, open(os.path.join(target, filename), "wb") as dst:
                shutil.copyfileobj(src, dst)
EOF

echo "--> [3/4] Compiling deterministic interconnected RFP, CLIN, Diagram, and Amendment PDFs..."
python3 "${SCRIPT_DIR}/build_procurement_corpus.py"

echo "--> [4/4] Procurement corpus status:"
echo "Solicitation 01 (Cloud Infrastructure): $(ls -1 "${CORPUS_DIR}/solicitation_01_cloud_infrastructure" | wc -l) PDFs"
echo "Solicitation 02 (Data Analytics Platform): $(ls -1 "${CORPUS_DIR}/solicitation_02_data_analytics_platform" | wc -l) PDFs"
echo "Solicitation 03 (Cybersecurity Operations): $(ls -1 "${CORPUS_DIR}/solicitation_03_cybersecurity_operations" | wc -l) PDFs"

echo "================================================================="
echo "Corpus ready for BigQuery Vector Search and RAG Arena benchmarks!"
echo "================================================================="
