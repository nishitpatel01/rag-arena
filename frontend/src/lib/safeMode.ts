/**
 * Screen-share safe mode: masks GCP identity values in strings rendered from server data.
 *
 * Pure functions only (no React, no DOM) so they can be unit-tested with plain `node`.
 * Replacement is exact-substring (identities are matched literally, never as regexes); the bucket
 * is replaced before the project id because the default bucket name contains the project id.
 */

export const SAFE_MODE_STORAGE_KEY = "rag-arena.safe-mode";
export const MASK_PROJECT_ID = "${GCP_PROJECT_ID}";
export const MASK_BUCKET = "${GCS_CORPUS_BUCKET}";
export const MASK_PROJECT_NUMBER = "${GCP_PROJECT_NUMBER}";

/** Identity source: the `/api/v1/health` response (only these two fields are read). */
export interface IdentitySource {
  readonly project_id?: string | null;
  readonly gcs_bucket?: string | null;
}

const PROJECT_NUMBER_PATTERN = /projects\/\d+/g;
/** Bound for maskDeep recursion; server payloads are shallow (≤ ~10 levels). */
export const MASK_MAX_DEPTH = 64;

/** Replace every exact occurrence of the identities in `text` with their placeholders. */
export function maskIdentity(text: string, health: IdentitySource | null | undefined): string {
  if (typeof text !== "string" || text.length === 0) return text;
  let out = text;
  const bucket = health?.gcs_bucket?.trim() ?? "";
  const project = health?.project_id?.trim() ?? "";
  if (bucket.length > 0) out = out.split(bucket).join(MASK_BUCKET);
  if (project.length > 0) out = out.split(project).join(MASK_PROJECT_ID);
  return out.replace(PROJECT_NUMBER_PATTERN, `projects/${MASK_PROJECT_NUMBER}`);
}

function maskAt(value: unknown, health: IdentitySource | null | undefined, depth: number): unknown {
  if (depth > MASK_MAX_DEPTH) throw new Error(`maskDeep: depth limit ${MASK_MAX_DEPTH} exceeded`);
  if (typeof value === "string") return maskIdentity(value, health);
  if (Array.isArray(value)) return value.map((v) => maskAt(v, health, depth + 1));
  if (value === null || typeof value !== "object") return value;
  if (Object.getPrototypeOf(value) !== Object.prototype) return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    out[maskIdentity(k, health)] = maskAt(v, health, depth + 1);
  }
  return out;
}

/** Deep copy of plain JSON-like data with every string (values and keys) passed through maskIdentity. */
export function maskDeep<T>(value: T, health: IdentitySource | null | undefined): T {
  return maskAt(value, health, 0) as T;
}

/** Initial toggle state: on when the URL has `safe=1` or localStorage holds "1"; off otherwise. */
export function initialSafeMode(search: string, stored: string | null): boolean {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  if (params.get("safe") === "1") return true;
  return stored === "1";
}
