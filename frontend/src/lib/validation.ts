/** Input validation helpers (query bounds + PDF candidate format check without app size caps). */

export const MAX_QUERY_CHARS = 4000;
/** `question_id` (recording key in replay, reference-answer key for the judge): empty = ad-hoc. */
export const QUESTION_ID_PATTERN = /^[A-Za-z0-9_.-]{0,80}$/;
export const QUESTION_ID_ERROR = "question_id may contain only letters, digits, `_ . -` (max 80).";

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
    return "Only .pdf documents are accepted.";
  }
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return "Selected PDF file is empty.";
  }
  return null;
}
