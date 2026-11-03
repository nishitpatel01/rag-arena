/**
 * Pure Retrieval Diff Inspector model (Release 1.3.0 Step 4) — no React, no I/O, no mutable globals.
 * Aligns retrieved chunks across 2–6 engines or ablation slots by either exact `chunk_id` (same corpus
 * store, e.g. reranker / hybrid / top-k ablations) or normalized `doc_page` (`basename::p<page>` across
 * heterogeneous stores like BigQuery vs RAG Engine vs Vertex AI Search), computing rank shifts (`same`,
 * `promoted`, `demoted`, `added`, `dropped`, `absent`) relative to a chosen baseline slot.
 */

import type { Citation, EngineTimeline } from "../../types/arena";

export type DiffAlignMode = "chunk_id" | "doc_page";
export type RankDeltaKind = "same" | "promoted" | "demoted" | "added" | "dropped" | "absent";

export interface DiffEngineColumn {
  readonly engineId: string;
  readonly short: string;
  readonly label: string;
}

export interface NormalizedChunk {
  readonly chunkId: string;
  readonly docName: string;
  readonly docBasename: string;
  readonly pageNumber: number | null;
  readonly sectionId: string;
  readonly elementType: string;
  readonly score: number | null;
  readonly amendmentNumber: number | null;
  readonly excerpt: string;
  readonly rank: number;
  readonly docPageKey: string;
  readonly citation: Citation;
}

export interface SlotRankCell {
  readonly engineId: string;
  readonly rank: number | null;
  readonly score: number | null;
  readonly deltaVsBaseline: number | null;
  readonly kind: RankDeltaKind;
  readonly badgeText: string;
  readonly chunk: NormalizedChunk | null;
}

export interface RetrievalDiffRow {
  readonly key: string;
  readonly chunkId: string;
  readonly docBasename: string;
  readonly pageNumber: number | null;
  readonly sectionId: string;
  readonly elementType: string;
  readonly amendmentNumber: number | null;
  readonly excerpt: string;
  readonly cells: Readonly<Record<string, SlotRankCell>>;
}

export interface EngineRetrievalStats {
  readonly total: number;
  readonly uniqueOnlyHere: number;
  readonly sharedWithBaseline: number;
}

export interface RetrievalDiffSummary {
  readonly baselineEngineId: string;
  readonly alignBy: DiffAlignMode;
  readonly sharedAcrossAllCount: number;
  readonly uniqueKeysTotal: number;
  readonly perEngineCounts: Readonly<Record<string, EngineRetrievalStats>>;
  readonly rows: readonly RetrievalDiffRow[];
}

function docBasename(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "unknown_doc";
  const parts = trimmed.split("/");
  return parts[parts.length - 1] || trimmed;
}

function buildDocPageKey(base: string, pageNumber: number | null, fallbackId: string): string {
  if (base === "unknown_doc" && pageNumber === null) return fallbackId;
  return `${base.toLowerCase()}::p${pageNumber ?? "na"}`;
}

function cleanExcerpt(raw: string): string {
  const flat = raw.replace(/\s+/g, " ").trim();
  return flat.length <= 160 ? flat : `${flat.slice(0, 157)}…`;
}

function makeFallbackCitation(
  chunkId: string,
  docBasenameStr: string,
  solicitationId: string | null,
  pageNumber: number | null,
  sectionId: string | null,
  elementType: string,
  excerpt: string,
): Citation {
  const page = pageNumber ?? 1;
  return {
    chunk_id: chunkId,
    doc_name: docBasenameStr,
    solicitation_id: solicitationId ?? "",
    page_number: page,
    section_id: sectionId,
    element_type: elementType || "prose",
    bbox: { page, x0: 0, y0: 0, x1: 1, y1: 1 },
    excerpt,
    bbox_source: "page",
  };
}

/** Extracts ordered `NormalizedChunk[]` from an `EngineTimeline` (with replay citation fallback). */
export function extractEngineChunks(timeline: EngineTimeline | undefined): readonly NormalizedChunk[] {
  const ans = timeline?.answer;
  if (!ans) return [];
  const citeById = new Map<string, Citation>();
  for (const c of ans.citations) {
    if (c.chunk_id && !citeById.has(c.chunk_id)) citeById.set(c.chunk_id, c);
  }
  if (ans.retrievedChunkDetails && ans.retrievedChunkDetails.length > 0) {
    return ans.retrievedChunkDetails.map((d, idx) => {
      const cite = citeById.get(d.chunk_id);
      const docName = d.doc_name || cite?.doc_name || "";
      const pageNumber = d.page_number ?? cite?.page_number ?? null;
      const base = docBasename(docName);
      const excerpt = cleanExcerpt(d.content || cite?.excerpt || "");
      return {
        chunkId: d.chunk_id,
        docName,
        docBasename: base,
        pageNumber,
        sectionId: d.section_id || cite?.section_id || "",
        elementType: d.element_type || cite?.element_type || "prose",
        score: d.score,
        amendmentNumber: d.amendment_number,
        excerpt,
        rank: idx + 1,
        docPageKey: buildDocPageKey(base, pageNumber, d.chunk_id),
        citation:
          cite ??
          makeFallbackCitation(
            d.chunk_id,
            base,
            d.solicitation_id,
            pageNumber,
            d.section_id || null,
            d.element_type || "prose",
            excerpt,
          ),
      };
    });
  }
  if (ans.retrievedChunks.length > 0) {
    return ans.retrievedChunks.map((rc, idx) => {
      const cite = citeById.get(rc.chunk_id);
      const docName = cite?.doc_name ?? "";
      const pageNumber = cite?.page_number ?? null;
      const base = docBasename(docName);
      const excerpt = cleanExcerpt(rc.content || cite?.excerpt || "");
      return {
        chunkId: rc.chunk_id,
        docName,
        docBasename: base,
        pageNumber,
        sectionId: cite?.section_id ?? "",
        elementType: cite?.element_type ?? "prose",
        score: null,
        amendmentNumber: null,
        excerpt,
        rank: idx + 1,
        docPageKey: buildDocPageKey(base, pageNumber, rc.chunk_id),
        citation: cite ?? makeFallbackCitation(rc.chunk_id, base, null, pageNumber, null, "prose", excerpt),
      };
    });
  }
  return ans.citations.map((c, idx) => {
    const cid = c.chunk_id || `${c.doc_name}_p${c.page_number}_${idx}`;
    const base = docBasename(c.doc_name);
    return {
      chunkId: cid,
      docName: c.doc_name,
      docBasename: base,
      pageNumber: c.page_number,
      sectionId: c.section_id ?? "",
      elementType: c.element_type || "prose",
      score: null,
      amendmentNumber: null,
      excerpt: cleanExcerpt(c.excerpt),
      rank: idx + 1,
      docPageKey: buildDocPageKey(base, c.page_number, cid),
      citation: c,
    };
  });
}

/** Auto-detects `"chunk_id"` when chunk IDs overlap across slots, or `"doc_page"` across disjoint stores. */
export function defaultAlignmentFor(
  columns: readonly DiffEngineColumn[],
  timelines: Readonly<Record<string, EngineTimeline>>,
): DiffAlignMode {
  const seenIds = new Set<string>();
  const seenPages = new Set<string>();
  let idOverlap = false;
  let pageOverlap = false;
  for (const col of columns) {
    const chunks = extractEngineChunks(timelines[col.engineId]);
    const localIds = new Set<string>();
    const localPages = new Set<string>();
    for (const c of chunks) {
      if (!localIds.has(c.chunkId)) {
        if (seenIds.has(c.chunkId)) idOverlap = true;
        seenIds.add(c.chunkId);
        localIds.add(c.chunkId);
      }
      if (!localPages.has(c.docPageKey)) {
        if (seenPages.has(c.docPageKey)) pageOverlap = true;
        seenPages.add(c.docPageKey);
        localPages.add(c.docPageKey);
      }
    }
  }
  if (idOverlap) return "chunk_id";
  return pageOverlap ? "doc_page" : "chunk_id";
}

function buildCell(
  engineId: string,
  isBaseline: boolean,
  baselineChunk: NormalizedChunk | undefined,
  currentChunk: NormalizedChunk | undefined,
): SlotRankCell {
  if (isBaseline) {
    return currentChunk
      ? {
          engineId,
          rank: currentChunk.rank,
          score: currentChunk.score,
          deltaVsBaseline: 0,
          kind: "same",
          badgeText: `#${currentChunk.rank} (baseline)`,
          chunk: currentChunk,
        }
      : { engineId, rank: null, score: null, deltaVsBaseline: null, kind: "absent", badgeText: "—", chunk: null };
  }
  if (baselineChunk && currentChunk) {
    const delta = baselineChunk.rank - currentChunk.rank;
    if (delta === 0) {
      return { engineId, rank: currentChunk.rank, score: currentChunk.score, deltaVsBaseline: 0, kind: "same", badgeText: `SAME #${currentChunk.rank}`, chunk: currentChunk };
    }
    if (delta > 0) {
      return {
        engineId,
        rank: currentChunk.rank,
        score: currentChunk.score,
        deltaVsBaseline: delta,
        kind: "promoted",
        badgeText: `↑ +${delta} (#${baselineChunk.rank} → #${currentChunk.rank})`,
        chunk: currentChunk,
      };
    }
    return {
      engineId,
      rank: currentChunk.rank,
      score: currentChunk.score,
      deltaVsBaseline: delta,
      kind: "demoted",
      badgeText: `↓ ${delta} (#${baselineChunk.rank} → #${currentChunk.rank})`,
      chunk: currentChunk,
    };
  }
  if (!baselineChunk && currentChunk) {
    return { engineId, rank: currentChunk.rank, score: currentChunk.score, deltaVsBaseline: null, kind: "added", badgeText: `NEW #${currentChunk.rank}`, chunk: currentChunk };
  }
  if (baselineChunk && !currentChunk) {
    return { engineId, rank: null, score: null, deltaVsBaseline: null, kind: "dropped", badgeText: `DROPPED (was #${baselineChunk.rank})`, chunk: null };
  }
  return { engineId, rank: null, score: null, deltaVsBaseline: null, kind: "absent", badgeText: "—", chunk: null };
}

/** Builds the complete aligned Retrieval Diff table + overlap statistics across all columns. */
export function buildRetrievalDiff(
  columns: readonly DiffEngineColumn[],
  timelines: Readonly<Record<string, EngineTimeline>>,
  baselineEngineId?: string | null,
  alignBy: DiffAlignMode = "chunk_id",
): RetrievalDiffSummary {
  const baselineId =
    baselineEngineId && columns.some((c) => c.engineId === baselineEngineId)
      ? baselineEngineId
      : (columns[0]?.engineId ?? "");
  const keyOf = (c: NormalizedChunk): string => (alignBy === "doc_page" ? c.docPageKey : c.chunkId);

  const mapsByEngine = new Map<string, Map<string, NormalizedChunk>>();
  const orderedKeys: string[] = [];
  const repByKey = new Map<string, NormalizedChunk>();

  // Baseline keys first (in baseline rank order), then remaining columns' new keys in rank order.
  const orderedCols = [
    ...columns.filter((c) => c.engineId === baselineId),
    ...columns.filter((c) => c.engineId !== baselineId),
  ];
  for (const col of orderedCols) {
    const m = new Map<string, NormalizedChunk>();
    for (const ch of extractEngineChunks(timelines[col.engineId])) {
      const k = keyOf(ch);
      if (!m.has(k)) m.set(k, ch);
      if (!repByKey.has(k)) {
        repByKey.set(k, ch);
        orderedKeys.push(k);
      }
    }
    mapsByEngine.set(col.engineId, m);
  }

  const nonEmptyCols = columns.filter((c) => (mapsByEngine.get(c.engineId)?.size ?? 0) > 0);
  const baseMap = mapsByEngine.get(baselineId) ?? new Map<string, NormalizedChunk>();
  let sharedAcrossAllCount = 0;
  for (const k of orderedKeys) {
    if (nonEmptyCols.length > 0 && nonEmptyCols.every((c) => mapsByEngine.get(c.engineId)?.has(k))) {
      sharedAcrossAllCount += 1;
    }
  }

  const perEngineCounts: Record<string, EngineRetrievalStats> = {};
  for (const col of columns) {
    const m = mapsByEngine.get(col.engineId) ?? new Map<string, NormalizedChunk>();
    let uniqueOnlyHere = 0;
    let sharedWithBaseline = 0;
    for (const k of m.keys()) {
      if (baseMap.has(k)) sharedWithBaseline += 1;
      const othersHave = columns.some((other) => other.engineId !== col.engineId && mapsByEngine.get(other.engineId)?.has(k));
      if (!othersHave) uniqueOnlyHere += 1;
    }
    perEngineCounts[col.engineId] = { total: m.size, uniqueOnlyHere, sharedWithBaseline };
  }

  const rows: RetrievalDiffRow[] = orderedKeys.map((k) => {
    const rep = repByKey.get(k)!;
    const baseChunk = baseMap.get(k);
    const cells: Record<string, SlotRankCell> = {};
    for (const col of columns) {
      cells[col.engineId] = buildCell(col.engineId, col.engineId === baselineId, baseChunk, mapsByEngine.get(col.engineId)?.get(k));
    }
    return {
      key: k,
      chunkId: rep.chunkId,
      docBasename: rep.docBasename,
      pageNumber: rep.pageNumber,
      sectionId: rep.sectionId,
      elementType: rep.elementType,
      amendmentNumber: rep.amendmentNumber,
      excerpt: rep.excerpt,
      cells,
    };
  });

  return {
    baselineEngineId: baselineId,
    alignBy,
    sharedAcrossAllCount,
    uniqueKeysTotal: orderedKeys.length,
    perEngineCounts,
    rows,
  };
}
