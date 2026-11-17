/**
 * Industry Benchmark Packs & Live Corpus Templates (Slice #6).
 *
 * Pure deterministic model (no React, no DOM, no network):
 * - Maps all 8 canonical recorded golden benchmarks in `data/golden_benchmarks/procurement_benchmark.json`
 *   without altering any `question_id` or `135` official recordings.
 * - Provides curated Live Templates grounded in the real CUAD / FSI / Healthcare PDF contracts
 *   shipped under `data/sample_docs/procurement_corpus/`.
 * - Explicitly distinguishes `Recorded (Replay + Live)` vs `Live Template` so `DEMO_MODE=replay`
 *   never misleads users into running unrecorded custom prompts.
 */

import type { BenchmarkQuestion } from "../../types/arena";

export const INDUSTRY_PACK_IDS = [
  "all",
  "federal_procurement",
  "sla_financial",
  "security_soc",
  "legal_cuad",
] as const;

export type IndustryPackId = (typeof INDUSTRY_PACK_IDS)[number];
export type SpecificIndustryPackId = Exclude<IndustryPackId, "all">;

export const GOLDEN_BADGE_LABEL = "Recorded (Replay + Live)" as const;
export const LIVE_TEMPLATE_BADGE_LABEL = "Live Template" as const;

export const REPLAY_LIVE_TEMPLATE_REASON =
  "Live Templates query the 27 CUAD legal, FSI, and healthcare contracts in data/sample_docs without pre-recorded replay fixtures. Switch DEMO_MODE=live to run live custom templates, or choose a Recorded (Replay + Live) benchmark.";

export type TemplateVertical = "Legal M&A" | "FSI / Banking" | "Healthcare / Biopharma";

export interface IndustryPackDefinition {
  readonly id: IndustryPackId;
  readonly label: string;
  readonly shortLabel: string;
  readonly description: string;
}

export interface LivePromptTemplate {
  readonly id: string;
  readonly title: string;
  readonly vertical: TemplateVertical;
  readonly packIds: readonly SpecificIndustryPackId[];
  readonly solicitationDir:
    | "solicitation_01_cloud_infrastructure"
    | "solicitation_02_data_analytics_platform"
    | "solicitation_03_cybersecurity_operations";
  readonly sourceDoc: string;
  readonly prompt: string;
}

export const INDUSTRY_PACKS: readonly IndustryPackDefinition[] = [
  {
    id: "all",
    label: "All Benchmarks",
    shortLabel: "All",
    description: "All 8 recorded golden benchmarks plus curated CUAD Legal M&A, FSI, and Healthcare live templates.",
  },
  {
    id: "federal_procurement",
    label: "Federal & Enterprise Procurement",
    shortLabel: "Federal RFP",
    description: "CLIN labor schedules, Multi-Region VPC failover diagrams, and BigQuery Medallion addenda.",
  },
  {
    id: "sla_financial",
    label: "Financial & SLA Math",
    shortLabel: "SLA & Finance",
    description: "Multi-hop amendment rate supersession, SLA availability credit math, and FSI custody/fund contracts.",
  },
  {
    id: "security_soc",
    label: "Cybersecurity & SOC Matrix",
    shortLabel: "Cyber & SOC2",
    description: "NIST SP 800-207 Zero Trust PEP schematics, P1/P2 incident SLAs, SOC2/HIPAA, and GMP QA controls.",
  },
  {
    id: "legal_cuad",
    label: "Legal M&A Contracts",
    shortLabel: "Legal & CUAD",
    description: "Live templates grounded in the real SEC/CUAD M&A IP licenses, banking escrow, and biopharma contracts.",
  },
];

/** Canonical 1-to-1 mapping of the 8 recorded golden benchmark IDs to their primary Industry Pack. */
export const GOLDEN_QUESTION_PACK_BY_ID: Readonly<Record<string, SpecificIndustryPackId>> = {
  q_exact_clin_lookup: "federal_procurement",
  q_vpc_failover_diagram: "federal_procurement",
  q_data_lakehouse_addendum: "federal_procurement",
  q_amendment_rate_comparison: "sla_financial",
  q_sla_credit_calculation: "sla_financial",
  q_cross_solicitation_total_cost: "sla_financial",
  q_zero_trust_pep_diagram: "security_soc",
  q_soc_p1_response_matrix: "security_soc",
};

/**
 * Curated live custom prompt templates backed by real PDFs in `data/sample_docs/procurement_corpus/`.
 * These run against real GCP retrieval + synthesis in `live`/`record` mode (ad-hoc `question_id: ""`).
 */
export const LIVE_PROMPT_TEMPLATES: readonly LivePromptTemplate[] = [
  {
    id: "tpl_cuad_arconic_spinoff_ip",
    title: "M&A Spin-Off Trademark License & IP Indemnification (Arconic)",
    vertical: "Legal M&A",
    packIds: ["legal_cuad"],
    solicitationDir: "solicitation_03_cybersecurity_operations",
    sourceDoc: "ArconicRolledProductsCorp_20191217_10-12B_EX-2.7_11923804_EX-2.7_Trademark License Agreement.pdf",
    prompt:
      "Under the Arconic Rolled Products Trademark License Agreement (Exhibit 2.7 to the Separation and Distribution Agreement), what royalty-free license rights apply to inventory during the Transition Period, how is third-party trademark infringement enforced, and what are Licensee's indemnification obligations?",
  },
  {
    id: "tpl_cuad_ferroglobe_outsourcing",
    title: "Enterprise IT Outsourcing Governance & Liability Caps (Ferroglobe)",
    vertical: "Legal M&A",
    packIds: ["legal_cuad", "federal_procurement"],
    solicitationDir: "solicitation_03_cybersecurity_operations",
    sourceDoc: "FerroglobePlc_20150624_F-4A_EX-10.20_9154746_EX-10.20_Outsourcing Agreement.pdf",
    prompt:
      "In the Ferroglobe Outsourcing Agreement (Exhibit 10.20), summarize the service governance structure, change-control procedures, termination rights, and limitation-of-liability carve-outs.",
  },
  {
    id: "tpl_fsi_bofa_cardlytics",
    title: "Banking Software Source-Code Custody & Maintenance (Bank of America / Cardlytics)",
    vertical: "FSI / Banking",
    packIds: ["legal_cuad", "sla_financial"],
    solicitationDir: "solicitation_01_cloud_infrastructure",
    sourceDoc: "CardlyticsInc_20180112_S-1_EX-10.16_11002987_EX-10.16_Maintenance Agreement1.pdf",
    prompt:
      "In the Software License, Customization and Maintenance Agreement (CW251207) between Bank of America, N.A. and Cardlytics, Inc., what are the source code custody/escrow provisions, customization ownership rules, and termination obligations?",
  },
  {
    id: "tpl_fsi_arca_treasury_fund",
    title: "1940 Act Treasury Interval Fund & Digital Custody Expenses (Arca U.S. Treasury Fund)",
    vertical: "FSI / Banking",
    packIds: ["legal_cuad", "sla_financial"],
    solicitationDir: "solicitation_02_data_analytics_platform",
    sourceDoc: "ArcaUsTreasuryFund_20200207_N-2_EX-99.K5_11971930_EX-99.K5_Development Agreement.pdf",
    prompt:
      "Under the Blockchain Administration and Development Agreement between Arca U.S. Treasury Fund (Rule 23c-3 interval fund) and Arca Capital Management, LLC, which operational, custodian, transfer-agent, and NAV accounting expenses are borne by the Fund versus the Blockchain Administrator?",
  },
  {
    id: "tpl_health_paratek_gmp_supply",
    title: "Biopharma API Manufacturing QA, Stability Testing & Supply Failure (Paratek)",
    vertical: "Healthcare / Biopharma",
    packIds: ["legal_cuad", "security_soc"],
    solicitationDir: "solicitation_03_cybersecurity_operations",
    sourceDoc: "ParatekPharmaceuticalsInc_20170505_10-KA_EX-10.29_10323872_EX-10.29_Outsourcing Agreement.pdf",
    prompt:
      "In the Outsourcing Agreement between Paratek Pharmaceuticals, Inc. and CARBOGEN AMCIS AG, what quality controls govern facility monitoring, subcontracting, release and stability testing, retention samples, and material failure of supply?",
  },
  {
    id: "tpl_health_aimmune_clinical_dev",
    title: "Clinical Biologics Development & Regulatory Governance (Aimmune Therapeutics)",
    vertical: "Healthcare / Biopharma",
    packIds: ["legal_cuad"],
    solicitationDir: "solicitation_02_data_analytics_platform",
    sourceDoc: "AimmuneTherapeuticsInc_20200205_8-K_EX-10.3_11967170_EX-10.3_Development Agreement.pdf",
    prompt:
      "In the Aimmune Therapeutics Development Agreement (Exhibit 10.3), how are clinical development responsibilities, intellectual property rights, and regulatory reporting obligations allocated between the parties?",
  },
];

export function isIndustryPackId(value: unknown): value is IndustryPackId {
  return typeof value === "string" && (INDUSTRY_PACK_IDS as readonly string[]).includes(value);
}

/** Deterministic pack assignment for any benchmark question (known ID map -> category fallback). */
export function classifyGoldenQuestionPack(
  q: Pick<BenchmarkQuestion, "question_id" | "failure_mode_category">,
): SpecificIndustryPackId {
  const mapped = GOLDEN_QUESTION_PACK_BY_ID[q.question_id];
  if (mapped) return mapped;
  if (q.failure_mode_category === "multi_hop_amendment_and_math") return "sla_financial";
  if (q.failure_mode_category === "table_matrix_extraction") return "security_soc";
  return "federal_procurement";
}

export interface FilteredPackItems {
  readonly pack: IndustryPackDefinition;
  readonly recordedQuestions: readonly BenchmarkQuestion[];
  readonly liveTemplates: readonly LivePromptTemplate[];
}

export function filterPackItems(
  packId: IndustryPackId,
  questions: readonly BenchmarkQuestion[],
): FilteredPackItems {
  const pack = INDUSTRY_PACKS.find((p) => p.id === packId) ?? INDUSTRY_PACKS[0];
  if (pack.id === "all") {
    return {
      pack,
      recordedQuestions: questions,
      liveTemplates: LIVE_PROMPT_TEMPLATES,
    };
  }
  const targetPack: SpecificIndustryPackId = pack.id;
  return {
    pack,
    recordedQuestions: questions.filter((q) => classifyGoldenQuestionPack(q) === targetPack),
    liveTemplates: LIVE_PROMPT_TEMPLATES.filter((t) => t.packIds.includes(targetPack)),
  };
}

export interface PackSelectionResolution {
  readonly kind: "recorded" | "live_template";
  readonly badge: typeof GOLDEN_BADGE_LABEL | typeof LIVE_TEMPLATE_BADGE_LABEL;
  readonly query: string;
  readonly questionId: string;
  readonly blockedInReplay: boolean;
  readonly reason: string | null;
  readonly template: LivePromptTemplate | null;
}

/**
 * Resolves a select option value (`<question_id>`, `golden:<question_id>`, or `tpl:<template_id>`).
 * In `replay` mode, `Live Template` selections return `blockedInReplay: true` with an explicit explanation
 * so replay users never overwrite a runnable golden question with an unrecorded ad-hoc prompt.
 */
export function resolvePackSelection(
  rawValue: string,
  questions: readonly BenchmarkQuestion[],
  demoMode: string | null | undefined,
): PackSelectionResolution | null {
  const trimmed = rawValue.trim();
  if (!trimmed) return null;

  if (trimmed.startsWith("tpl:")) {
    const tplId = trimmed.slice(4);
    const tpl = LIVE_PROMPT_TEMPLATES.find((t) => t.id === tplId);
    if (!tpl) return null;
    const isReplay = demoMode === "replay";
    return {
      kind: "live_template",
      badge: LIVE_TEMPLATE_BADGE_LABEL,
      query: tpl.prompt,
      questionId: "",
      blockedInReplay: isReplay,
      reason: isReplay ? REPLAY_LIVE_TEMPLATE_REASON : null,
      template: tpl,
    };
  }

  const qId = trimmed.startsWith("golden:") ? trimmed.slice(7) : trimmed;
  const q = questions.find((item) => item.question_id === qId);
  if (!q) return null;
  return {
    kind: "recorded",
    badge: GOLDEN_BADGE_LABEL,
    query: q.question,
    questionId: q.question_id,
    blockedInReplay: false,
    reason: null,
    template: null,
  };
}

export function findMatchingLiveTemplate(query: string): LivePromptTemplate | null {
  const normalized = query.trim();
  if (!normalized) return null;
  return LIVE_PROMPT_TEMPLATES.find((t) => t.prompt === normalized) ?? null;
}
