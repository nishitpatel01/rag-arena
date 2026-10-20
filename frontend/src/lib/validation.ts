/** Input validation helpers (salvaged from v2 QueryBar / DocumentUpload). */

export const MAX_QUERY_CHARS = 4000;
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export function validateQueryInput(query: string, engines: readonly string[]): string | null {
  if (typeof query !== "string" || query.trim().length === 0) {
    return "Enter a non-empty procurement question.";
  }
  if (query.trim().length > MAX_QUERY_CHARS) {
    return `Question exceeds the ${MAX_QUERY_CHARS} character limit.`;
  }
  if (!Array.isArray(engines) || engines.length === 0) {
    return "Select at least one level to run.";
  }
  return null;
}

export function validatePdfCandidate(fileName: string, sizeBytes: number): string | null {
  if (typeof fileName !== "string" || !fileName.toLowerCase().endsWith(".pdf")) {
    return "Only .pdf procurement documents are accepted.";
  }
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return "Selected PDF file is empty.";
  }
  if (sizeBytes > MAX_UPLOAD_BYTES) {
    return "PDF exceeds maximum size limit of 10 MB.";
  }
  return null;
}
