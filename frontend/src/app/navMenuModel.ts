import type { LevelDescriptor } from "../types/arena";

export const SHORT_LEVEL_LABEL: Record<string, string> = {
  managed_vertex: "1 · Vertex Search",
  rag_engine: "2 · RAG Engine",
  level1_naive: "3 · BQ Vector",
  level2_hybrid: "4 · BQ Hybrid",
  cloudsql_pgvector: "5 · Cloud SQL pgvector",
  alloydb_scann: "6 · AlloyDB ScaNN",
  vertex_vector_v2: "7 · Vector Search 2.0",
  level3_multimodal: "8 · Multimodal",
  bigquery_graphrag: "9 · BQ GraphRAG (GQL)",
  level4_agentic: "10 · Agentic ADK",
};

export function lockedTitle(level: LevelDescriptor, all: readonly LevelDescriptor[]): string {
  const prev = all.find((l) => l.order === level.order - 1);
  return prev ? `Locked — pending Level ${prev.order} sign-off` : "Locked";
}

export function isArchitectureRoute(pathname: string): boolean {
  return pathname.startsWith("/levels/");
}

export function architectureMenuLabel(pathname: string, ordered: readonly LevelDescriptor[]): string {
  if (!isArchitectureRoute(pathname)) return "Architectures";
  const levelId = pathname.slice("/levels/".length).split("/")[0]?.trim() ?? "";
  if (!levelId) return "Architectures";
  if (SHORT_LEVEL_LABEL[levelId]) return SHORT_LEVEL_LABEL[levelId];
  const found = ordered.find((l) => l.level_id === levelId);
  return found?.label ?? "Architectures";
}
