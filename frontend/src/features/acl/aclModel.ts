/**
 * Pure FCIS Identity-Aware Document ACL & Row-Level Security client model (v1.6.3).
 *
 * Mirrors `backend/app/core/acl.py` 1:1 so persona selection, doc-count summaries,
 * per-architecture capability badges, and replay ACL demo gating work identically
 * online, offline, and in unit tests.
 *
 * Security hygiene: `localStorage` stores ONLY the non-sensitive allow-listed persona ID
 * (`all_access` | `engineering_analyst` | `pricing_finance` | `security_soc2` | `legal_counsel`),
 * never credentials, tokens, or caller headers.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import type {
  AclPersona,
  AclPersonaId,
  AclPersonasResponse,
  ArchitectureAclCapability,
  CorpusAclSummary,
  RecordedAclDemo,
} from "../../types/arena";

export const ALL_ACCESS_PERSONA_ID: AclPersonaId = "all_access";
export const ACL_STORAGE_KEY = "rag-arena.access-persona";
const ACL_EVENT_NAME = "rag-arena:acl-persona";

export const ACL_PERSONA_IDS: readonly AclPersonaId[] = [
  "all_access",
  "engineering_analyst",
  "pricing_finance",
  "security_soc2",
  "legal_counsel",
] as const;

export const DEFAULT_ACL_PERSONAS: readonly AclPersona[] = [
  {
    persona_id: "all_access",
    label: "All Access (Admin)",
    short_label: "All Access",
    description: "Unrestricted executive/procurement administrator across all corpus groups.",
    groups: ["corp_all", "procurement_pricing", "security_soc2", "legal_contracts", "exec_admin"],
    blocked_summary: "None — all 45 benchmark documents authorized",
  },
  {
    persona_id: "engineering_analyst",
    label: "Engineering Analyst",
    short_label: "Engineering",
    description: "Technical architecture, base RFPs, SOWs, and vendor Q&A only.",
    groups: ["corp_all"],
    blocked_summary: "Blocks pricing CLIN rate cards, rate amendments, SOC2/IR matrices, MSA, and CUAD contracts",
  },
  {
    persona_id: "pricing_finance",
    label: "Pricing & Finance",
    short_label: "Pricing & Finance",
    description: "General solicitations plus commercial CLIN pricing schedules and rate amendments.",
    groups: ["corp_all", "procurement_pricing"],
    blocked_summary: "Blocks SOC2/incident-response matrices, MSA, and third-party CUAD contracts",
  },
  {
    persona_id: "security_soc2",
    label: "Security & SOC2",
    short_label: "Security & SOC2",
    description: "General solicitations plus SOC2 compliance matrices and incident response SLAs.",
    groups: ["corp_all", "security_soc2"],
    blocked_summary: "Blocks CLIN pricing schedules, rate amendments, MSA, and third-party CUAD contracts",
  },
  {
    persona_id: "legal_counsel",
    label: "Legal Counsel",
    short_label: "Legal Counsel",
    description: "General solicitations plus Master Services Agreement and third-party CUAD agreements.",
    groups: ["corp_all", "legal_contracts"],
    blocked_summary: "Blocks CLIN pricing schedules, rate amendments, and SOC2/incident-response matrices",
  },
];

export const DEFAULT_ACL_CAPABILITIES: Readonly<Record<string, ArchitectureAclCapability>> = {
  managed_vertex: {
    level_id: "managed_vertex",
    supports_chunk_acl: true,
    enforcement_mode: "vais_filter_expression",
    mechanism_label: "Vertex AI Search filter expression",
    summary: "Filters documents via Discovery Engine structData allowed_groups: ANY(...) (enterprise IdP aclConfig supported).",
    honest_note: "Corpus imports with structured allowed_groups metadata enforce pre-synthesis filtering at search time.",
  },
  rag_engine: {
    level_id: "rag_engine",
    supports_chunk_acl: false,
    enforcement_mode: "corpus_boundary_only",
    mechanism_label: "RagCorpus IAM boundary only",
    summary: "Vertex AI RAG Engine enforces IAM per RagCorpus, not per-document chunk ACLs inside a shared corpus.",
    honest_note:
      "Restricted personas fail closed on a shared RagCorpus; isolate sensitive groups into dedicated corpora or use BigQuery / AlloyDB / VAIS / VVS2.",
  },
  level1_naive: {
    level_id: "level1_naive",
    supports_chunk_acl: true,
    enforcement_mode: "bigquery_parameterized_rls",
    mechanism_label: "BigQuery parameterized RLS (@caller_groups)",
    summary: "Pre-filters document_chunks_v3 inside VECTOR_SEARCH via UNNEST(allowed_groups) IN UNNEST(@caller_groups) + optional ROW ACCESS POLICY.",
    honest_note: "Unauthorized rows never leave BigQuery and never enter the synthesis prompt.",
  },
  level2_hybrid: {
    level_id: "level2_hybrid",
    supports_chunk_acl: true,
    enforcement_mode: "bigquery_parameterized_rls",
    mechanism_label: "BigQuery parameterized RLS (@caller_groups)",
    summary: "Pre-filters both VECTOR_SEARCH and SEARCH() RRF legs inside BigQuery via @caller_groups before Discovery Engine reranking.",
    honest_note: "Unauthorized chunks are excluded before RRF fusion, reranking, and synthesis.",
  },
  cloudsql_pgvector: {
    level_id: "cloudsql_pgvector",
    supports_chunk_acl: true,
    enforcement_mode: "postgres_rls_policy",
    mechanism_label: "PostgreSQL Row-Level Security + pgvector 0.8.5 iterative scan",
    summary: "Enforces PostgreSQL CREATE POLICY (allowed_groups && current_setting('app.caller_groups')) and hnsw.iterative_scan = 'relaxed_order' over halfvec(3072) HNSW + GIN FTS.",
    honest_note: "Database-layer RLS enforces row visibility and iterative scan avoids HNSW top-K under-fetching on selective ACL filters.",
  },
  alloydb_scann: {
    level_id: "alloydb_scann",
    supports_chunk_acl: true,
    enforcement_mode: "postgres_rls_policy",
    mechanism_label: "PostgreSQL Row-Level Security + GIN",
    summary: "Enforces PostgreSQL CREATE POLICY (allowed_groups && current_setting('app.caller_groups')::text[]) and parameterized %(caller_groups)s on ScaNN + FTS legs.",
    honest_note: "Database-layer RLS enforces row visibility even against direct SQL connections.",
  },
  vertex_vector_v2: {
    level_id: "vertex_vector_v2",
    supports_chunk_acl: true,
    enforcement_mode: "vvs2_filter_fields",
    mechanism_label: "Vector Search 2.0 filterFields ($in)",
    summary: "Applies pre-filtered {'allowed_groups': {'$in': [...]}} on both dense ScaNN and sparse RRF legs in dataObjects:batchSearch.",
    honest_note: "Indexed filterFields prune unauthorized DataObjects before RRF scoring.",
  },
  level3_multimodal: {
    level_id: "level3_multimodal",
    supports_chunk_acl: true,
    enforcement_mode: "bigquery_parameterized_rls",
    mechanism_label: "BigQuery parameterized RLS (@caller_groups)",
    summary: "Pre-filters multimodal tables, diagrams, and amendments inside BigQuery VECTOR_SEARCH + SEARCH() via @caller_groups.",
    honest_note: "Unauthorized tables and visual diagrams are excluded before rerank and synthesis.",
  },
  bigquery_graphrag: {
    level_id: "bigquery_graphrag",
    supports_chunk_acl: true,
    enforcement_mode: "bigquery_parameterized_rls",
    mechanism_label: "BigQuery Property Graph GQL + @caller_groups RLS",
    summary: "Pre-filters both multimodal seed retrieval and ISO GQL GRAPH_TABLE amendment/supersession traversal via @caller_groups before reranking.",
    honest_note: "Unauthorized amendment nodes and chunks never enter graph expansion, reranking, or synthesis.",
  },
  level4_agentic: {
    level_id: "level4_agentic",
    supports_chunk_acl: true,
    enforcement_mode: "bigquery_parameterized_rls",
    mechanism_label: "ADK closure-pinned BigQuery RLS",
    summary: "Closure-pins @caller_groups inside search_procurement_corpus and check_amendments so prompt-injected PDFs cannot widen access.",
    honest_note: "Tool arguments cannot override caller_groups; every BigQuery tool query inherits the caller's effective groups.",
  },
};

export const DEFAULT_CORPUS_ACL_SUMMARY: CorpusAclSummary = {
  total_docs: 45,
  authorized_by_group: {
    corp_all: 10,
    procurement_pricing: 5,
    security_soc2: 2,
    legal_contracts: 28,
    exec_admin: 45,
  },
  authorized_by_persona: {
    all_access: 45,
    engineering_analyst: 10,
    pricing_finance: 15,
    security_soc2: 12,
    legal_counsel: 38,
  },
};

export function isValidAclPersonaId(raw: unknown): raw is AclPersonaId {
  return typeof raw === "string" && (ACL_PERSONA_IDS as readonly string[]).includes(raw);
}

/** Parse a stored persona ID strictly against the allow-list (defaults to `all_access`). */
export function parseStoredPersona(raw: string | null | undefined): AclPersonaId {
  return isValidAclPersonaId(raw) ? raw : ALL_ACCESS_PERSONA_ID;
}

export function isPersonaRestricted(personaId: string | null | undefined): boolean {
  return Boolean(personaId && personaId !== ALL_ACCESS_PERSONA_ID);
}

export function personaById(
  personas: readonly AclPersona[],
  personaId: string | null | undefined
): AclPersona {
  const target = parseStoredPersona(personaId);
  return personas.find((p) => p.persona_id === target) ?? DEFAULT_ACL_PERSONAS[0];
}

export function personaLabel(
  personaId: string | null | undefined,
  personas: readonly AclPersona[] = DEFAULT_ACL_PERSONAS
): string {
  return personaById(personas, personaId).short_label;
}

export function capabilityForLevel(
  capabilities: Readonly<Record<string, ArchitectureAclCapability>>,
  levelId: string | null | undefined
): ArchitectureAclCapability | null {
  if (!levelId) return null;
  return capabilities[levelId] ?? DEFAULT_ACL_CAPABILITIES[levelId] ?? null;
}

export interface AclReplayLookup {
  readonly engine: string;
  readonly synthesisModel: string;
  readonly judgeModel: string;
  readonly questionId: string;
  readonly personaId: string;
}

/** Find an exact recorded ACL demo matching `(engine, synthesis, judge, question_id, persona_id)`. */
export function findRecordedAclDemo(
  demos: readonly RecordedAclDemo[],
  query: AclReplayLookup
): RecordedAclDemo | null {
  const q = query.questionId.trim();
  if (!q || !isPersonaRestricted(query.personaId)) return null;
  return (
    demos.find(
      (d) =>
        d.engine === query.engine &&
        d.synthesis_model === query.synthesisModel &&
        d.judge_model === query.judgeModel &&
        d.question_id === q &&
        d.access_persona === query.personaId
    ) ?? null
  );
}

/**
 * Pure replay-mode gate for restricted personas.
 * Returns `null` when runnable (or when unrestricted / live mode), else an honest explanation.
 */
export function aclReplayBlockedReason(args: {
  readonly demoMode: string | null | undefined;
  readonly levelId: string;
  readonly personaId: string | null | undefined;
  readonly questionId: string;
  readonly synthesisModel: string | null | undefined;
  readonly judgeModel: string | null | undefined;
  readonly demos: readonly RecordedAclDemo[];
  readonly capabilities?: Readonly<Record<string, ArchitectureAclCapability>>;
}): string | null {
  if (!isPersonaRestricted(args.personaId)) return null;
  const cap = capabilityForLevel(args.capabilities ?? DEFAULT_ACL_CAPABILITIES, args.levelId);
  if (cap && !cap.supports_chunk_acl) {
    return `${cap.mechanism_label}: restricted personas fail closed on a shared RagCorpus (no per-document chunk ACLs).`;
  }
  if (args.demoMode !== "replay") return null;
  const q = args.questionId.trim();
  if (!q) {
    return "Replay mode plays recorded ACL demos only: choose a golden question with an ACL recording.";
  }
  if (!args.synthesisModel || !args.judgeModel) return null;
  const match = findRecordedAclDemo(args.demos, {
    engine: args.levelId,
    synthesisModel: args.synthesisModel,
    judgeModel: args.judgeModel,
    questionId: q,
    personaId: args.personaId ?? ALL_ACCESS_PERSONA_ID,
  });
  if (match) return null;
  const engineDemos = args.demos.filter((d) => d.engine === args.levelId);
  if (engineDemos.length > 0) {
    const sampleQids = Array.from(new Set(engineDemos.map((d) => d.question_id))).join(", ");
    const samplePersonas = Array.from(new Set(engineDemos.map((d) => d.access_persona))).join(", ");
    const samplePair = `${engineDemos[0].synthesis_model} + ${engineDemos[0].judge_model}`;
    return `No recorded ACL replay for "${q}" as ${args.personaId}. Recorded on this level: ${sampleQids} (${samplePersonas} · ${samplePair}).`;
  }
  return `No recorded ACL demo for ${args.levelId} as ${args.personaId} in replay mode (switch to All Access or run live).`;
}

// ---------------------------------------------------------------------------
// Cross-page state + single-flight catalog hook (Imperative Shell boundary)
// ---------------------------------------------------------------------------

let cachedCatalog: AclPersonasResponse | null = null;
let inflightCatalog: Promise<AclPersonasResponse | null> | null = null;

function loadAclCatalogOnce(): Promise<AclPersonasResponse | null> {
  if (cachedCatalog) return Promise.resolve(cachedCatalog);
  if (!inflightCatalog) {
    inflightCatalog = api
      .aclPersonas()
      .then((res) => {
        cachedCatalog = res;
        return res;
      })
      .catch(() => null)
      .finally(() => {
        inflightCatalog = null;
      });
  }
  return inflightCatalog;
}

export function readStoredPersona(): AclPersonaId {
  if (typeof window === "undefined") return ALL_ACCESS_PERSONA_ID;
  try {
    return parseStoredPersona(window.localStorage.getItem(ACL_STORAGE_KEY));
  } catch {
    return ALL_ACCESS_PERSONA_ID;
  }
}

export function writeStoredPersona(next: AclPersonaId): void {
  const safe = parseStoredPersona(next);
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ACL_STORAGE_KEY, safe);
  } catch {
    // Storage disabled/quota full — fall back to in-memory event sync.
  }
  window.dispatchEvent(new CustomEvent<AclPersonaId>(ACL_EVENT_NAME, { detail: safe }));
}

export interface AccessPersonaState {
  readonly personaId: AclPersonaId;
  readonly persona: AclPersona;
  readonly isRestricted: boolean;
  readonly personas: readonly AclPersona[];
  readonly capabilities: Readonly<Record<string, ArchitectureAclCapability>>;
  readonly corpusSummary: CorpusAclSummary;
  readonly recordedDemos: readonly RecordedAclDemo[];
  readonly setPersonaId: (next: AclPersonaId) => void;
}

/** Shared cross-page access persona state synced across Level, Compare, Workbench, and Document Viewer. */
export function useAccessPersona(): AccessPersonaState {
  const [personaId, setLocalPersonaId] = useState<AclPersonaId>(readStoredPersona);
  const [catalog, setCatalog] = useState<AclPersonasResponse | null>(cachedCatalog);

  useEffect(() => {
    let cancelled = false;
    void loadAclCatalogOnce().then((res) => {
      if (!cancelled && res) setCatalog(res);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onCustom = (ev: Event): void => {
      const detail = (ev as CustomEvent<unknown>).detail;
      setLocalPersonaId(parseStoredPersona(typeof detail === "string" ? detail : null));
    };
    const onStorage = (ev: StorageEvent): void => {
      if (ev.key === ACL_STORAGE_KEY) {
        setLocalPersonaId(parseStoredPersona(ev.newValue));
      }
    };
    window.addEventListener(ACL_EVENT_NAME, onCustom);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(ACL_EVENT_NAME, onCustom);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setPersonaId = useCallback((next: AclPersonaId) => {
    const safe = parseStoredPersona(next);
    setLocalPersonaId(safe);
    writeStoredPersona(safe);
  }, []);

  const personas = catalog?.personas?.length ? catalog.personas : DEFAULT_ACL_PERSONAS;
  const capabilities = catalog?.architecture_capabilities ?? DEFAULT_ACL_CAPABILITIES;
  const corpusSummary =
    catalog?.corpus_summary && catalog.corpus_summary.total_docs > 0
      ? catalog.corpus_summary
      : DEFAULT_CORPUS_ACL_SUMMARY;
  const recordedDemos = catalog?.recorded_acl_demos ?? [];
  const persona = personaById(personas, personaId);

  return {
    personaId,
    persona,
    isRestricted: isPersonaRestricted(personaId),
    personas,
    capabilities,
    corpusSummary,
    recordedDemos,
    setPersonaId,
  };
}
