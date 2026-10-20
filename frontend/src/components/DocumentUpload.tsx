import React, { useState } from "react";
import { Upload } from "lucide-react";
import { validatePdfCandidate } from "../lib/validation";
import { Pill } from "./ui/Pill";

const UPLOAD_TIMEOUT_MS = 25_000;

interface UploadResult {
  readonly status: string;
  readonly solicitation_id?: string;
  readonly upload?: { readonly session_id: string; readonly filename: string; readonly page_count: number };
  readonly chunks_indexed?: number;
}

export interface DocumentUploadProps {
  readonly onUploadSuccess?: (solicitationId: string) => void;
}

/** Guarded PDF upload (re-tokenized; endpoint availability depends on the backend build). */
export const DocumentUpload: React.FC<DocumentUploadProps> = ({ onUploadSuccess }) => {
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [result, setResult] = useState<UploadResult | null>(null);
  const inputId = "document-upload-input";

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    if (!file) return;
    const validationErr = validatePdfCandidate(file.name, file.size);
    if (validationErr) {
      setErrorMsg(validationErr);
      setResult(null);
      return;
    }
    setErrorMsg(null);
    setIsUploading(true);
    const controller = new AbortController();
    const timerId = window.setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
    try {
      const response = await fetch("/api/v1/ingest/upload", {
        method: "POST",
        headers: { "Content-Type": "application/pdf", "X-Filename": file.name },
        body: await file.arrayBuffer(),
        signal: controller.signal,
      });
      const payload = (await response.json()) as UploadResult & { readonly error?: string };
      if (!response.ok) throw new Error(payload.error || `Upload failed (${response.status})`);
      setResult(payload);
      const solId = payload.solicitation_id || (payload.upload ? `upload_${payload.upload.session_id.slice(0, 8)}` : null);
      if (solId) onUploadSuccess?.(solId);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to upload PDF document");
    } finally {
      window.clearTimeout(timerId);
      setIsUploading(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={inputId} className="inline-flex w-fit cursor-pointer items-center gap-1.5 rounded-md border border-line bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink-1 hover:border-ink-3">
        <Upload className="h-3.5 w-3.5" aria-hidden />
        {isUploading ? "Uploading…" : "Upload PDF (≤ 10 MB)"}
      </label>
      <input id={inputId} type="file" accept=".pdf,application/pdf" disabled={isUploading} onChange={handleFileChange} className="sr-only" />
      {errorMsg && (
        <p className="text-xs text-err" role="alert">
          {errorMsg}
        </p>
      )}
      {result?.upload && (
        <Pill tone="ok" mono>
          {result.upload.filename} · {result.upload.page_count} pages · {result.chunks_indexed ?? 0} chunks
        </Pill>
      )}
    </div>
  );
};
