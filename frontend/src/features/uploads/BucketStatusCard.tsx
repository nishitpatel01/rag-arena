import React from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { useSafeMode } from "../../app/SafeMode";
import { Button } from "../../components/ui/Button";
import { CodeBlock } from "../../components/ui/CodeBlock";
import { Pill, type PillTone } from "../../components/ui/Pill";
import type { BucketStatusResponse } from "./uploadModel";

export interface BucketStatusCardProps {
  readonly bucket: BucketStatusResponse | null;
  readonly loading: boolean;
  readonly ensuring: boolean;
  readonly error: string | null;
  readonly onEnsure: () => void;
  readonly onRefresh: () => void;
}

function bucketTone(bucket: BucketStatusResponse | null): PillTone {
  if (!bucket) return "neutral";
  if (bucket.usable) return "ok";
  if (bucket.state === "not_found") return "warn";
  return "err";
}

function bucketLabel(bucket: BucketStatusResponse | null, loading: boolean): string {
  if (!bucket) return loading ? "checking bucket…" : "not checked";
  return bucket.usable ? `private ✓ · ${bucket.state}` : bucket.state;
}

export const BucketStatusCard: React.FC<BucketStatusCardProps> = ({
  bucket,
  loading,
  ensuring,
  error,
  onEnsure,
  onRefresh,
}) => {
  const { mask } = useSafeMode();
  return (
    <div className="flex flex-col gap-2 rounded-md border border-line bg-surface-1 p-3" data-testid="bucket-status-card">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-ink-1">Private upload bucket</span>
          <Pill tone={bucketTone(bucket)}>{bucketLabel(bucket, loading)}</Pill>
          {bucket?.bucket && (
            <span className="font-mono text-xs text-ink-2">{mask(bucket.bucket)}</span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={bucket?.usable ? "ghost" : "primary"}
            disabled={ensuring}
            onClick={onEnsure}
            data-testid="ensure-upload-bucket"
            leadingIcon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
          >
            {ensuring ? "Verifying…" : bucket?.usable ? "Re-verify privacy" : "Create / verify bucket"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={loading || ensuring}
            onClick={onRefresh}
            leadingIcon={<RefreshCw className="h-3.5 w-3.5" aria-hidden />}
          >
            Check
          </Button>
        </div>
      </div>
      {error && (
        <p className="text-xs text-err" role="alert">
          Bucket check error: {mask(error)}
        </p>
      )}
      {bucket?.message && (
        <p className={`text-xs ${bucket.usable ? "text-ink-2" : "text-clay"}`} role={bucket.usable ? undefined : "alert"}>
          {mask(bucket.message)}
        </p>
      )}
      {bucket && bucket.problems.length > 0 && (
        <ul className="list-inside list-disc text-xs text-err" role="alert">
          {bucket.problems.map((prob) => (
            <li key={prob}>{mask(prob)}</li>
          ))}
        </ul>
      )}
      {bucket?.remedy && (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium text-ink-2">Admin remedy</span>
          <CodeBlock code={mask(bucket.remedy)} language="bash" />
        </div>
      )}
    </div>
  );
};
