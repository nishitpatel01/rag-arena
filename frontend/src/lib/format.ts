/** Pure formatting helpers (no React). */

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value < 0.01 ? `$${value.toFixed(5)}` : `$${value.toFixed(4)}`;
}

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  const trim = (v: number, digits: number): string => String(Number(v.toFixed(digits)));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${trim(bytes / 1024, 1)} KB`;
  if (bytes < 1024 ** 3) return `${trim(bytes / 1024 ** 2, 1)} MB`;
  return `${trim(bytes / 1024 ** 3, 2)} GB`;
}

/** Tone for an ingestion run / index state string; exact ACTIVE/completed = ok, anything qualified = info/warn. */
export function statusTone(raw: string): "ok" | "warn" | "err" | "info" | "active" | "neutral" {
  const v = raw.trim().toLowerCase();
  if (/^(completed|complete|succeeded|success|active)$/.test(v)) return "ok";
  if (/(failed|error|cancel)/.test(v)) return "err";
  if (/(disabled|pending|building|not populated)/.test(v)) return "warn";
  if (/^(running|started|in_progress)$/.test(v)) return "active";
  if (/^active\b/.test(v)) return "info";
  return "neutral";
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function parseTs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/** One-line result summary for a completed stage, built only from payload keys that exist. */
export function summarizePayload(payload: Record<string, unknown> | null): string {
  if (!payload) return "";
  const parts: string[] = [];
  const num = (k: string): number | null => (typeof payload[k] === "number" ? (payload[k] as number) : null);
  const rows = num("rows_returned");
  if (rows !== null) parts.push(`rows=${rows.toLocaleString()}`);
  const bytes = num("bytes_processed");
  if (bytes !== null) parts.push(payload["cache_hit"] === true ? `cache hit · ${formatBytes(bytes)} scanned` : `${formatBytes(bytes)} scanned`);
  const cin = num("candidates_in");
  const cout = num("candidates_out");
  if (cin !== null && cout !== null) parts.push(`${cin}→${cout} candidates`);
  const tok = num("token_count") ?? num("output_tokens") ?? num("prompt_tokens");
  if (tok !== null) parts.push(`tokens=${tok.toLocaleString()}`);
  const dims = num("dimensions");
  if (dims !== null) parts.push(`${dims}-d`);
  if (typeof payload["service_used"] === "string") parts.push(String(payload["service_used"]));
  if (typeof payload["model"] === "string" && parts.length < 3) parts.push(String(payload["model"]));
  return parts.slice(0, 3).join(" · ");
}
