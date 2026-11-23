import React from "react";
import { Lock, ShieldAlert, ShieldCheck } from "lucide-react";
import { Pill } from "../../components/ui/Pill";
import type { AclPersonaId } from "../../types/arena";
import {
  capabilityForLevel,
  isPersonaRestricted,
  parseStoredPersona,
  type AccessPersonaState,
} from "./aclModel";

export interface AccessPersonaSelectProps {
  readonly id?: string;
  readonly acl: AccessPersonaState;
  readonly levelId?: string | null;
  readonly demoMode?: string | null;
  readonly disabled?: boolean;
}

const selectClass =
  "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:cursor-not-allowed disabled:opacity-60";

/**
 * Identity-Aware Document ACL & Row-Level Security persona switcher (v1.6.3).
 * Displays the active persona's effective group pills, corpus doc visibility ratio,
 * per-architecture enforcement mechanism, and honest RagCorpus corpus-boundary warnings.
 */
export const AccessPersonaSelect: React.FC<AccessPersonaSelectProps> = ({
  id = "access-persona-select",
  acl,
  levelId = null,
  demoMode = null,
  disabled = false,
}) => {
  const restricted = isPersonaRestricted(acl.personaId);
  const cap = capabilityForLevel(acl.capabilities, levelId);
  const totalDocs = acl.corpusSummary.total_docs;
  const authDocs = acl.corpusSummary.authorized_by_persona[acl.personaId];
  const docCountText =
    typeof authDocs === "number" && totalDocs > 0 ? `${authDocs}/${totalDocs} docs` : null;

  const relevantDemos =
    demoMode === "replay" && restricted
      ? acl.recordedDemos.filter(
          (d) => d.access_persona === acl.personaId && (!levelId || d.engine === levelId)
        )
      : [];
  const sampleQids = Array.from(new Set(relevantDemos.map((d) => d.question_id)));

  return (
    <div
      className={`flex flex-col gap-1.5 rounded-md border p-2.5 ${
        restricted ? "border-ochre bg-surface-1" : "border-line bg-surface-1"
      }`}
      data-testid="access-persona-box"
    >
      <div className="flex flex-wrap items-center justify-between gap-1">
        <label htmlFor={id} className="inline-flex items-center gap-1.5 text-xs font-medium text-ink-2">
          <Lock className={`h-3.5 w-3.5 ${restricted ? "text-ochre" : "text-moss"}`} aria-hidden />
          <span>Access persona (Document ACL)</span>
        </label>
        {docCountText && (
          <Pill tone={restricted ? "warn" : "ok"} mono data-testid="acl-doc-count">
            {docCountText}
          </Pill>
        )}
      </div>

      <select
        id={id}
        data-testid="access-persona-select"
        value={acl.personaId}
        disabled={disabled}
        onChange={(e) => acl.setPersonaId(parseStoredPersona(e.target.value) as AclPersonaId)}
        className={selectClass}
      >
        {acl.personas.map((p) => {
          const count = acl.corpusSummary.authorized_by_persona[p.persona_id];
          const suffix = typeof count === "number" && totalDocs > 0 ? ` (${count}/${totalDocs} docs)` : "";
          return (
            <option key={p.persona_id} value={p.persona_id}>
              {p.label}
              {suffix}
            </option>
          );
        })}
      </select>

      <div className="flex flex-wrap items-center gap-1">
        {acl.persona.groups.map((g) => (
          <Pill key={g} tone={restricted ? "warn" : "neutral"} mono>
            {g}
          </Pill>
        ))}
        {cap && (
          <Pill
            tone={cap.supports_chunk_acl ? "info" : "warn"}
            title={cap.summary}
            data-testid="acl-mechanism-pill"
          >
            {cap.mechanism_label}
          </Pill>
        )}
      </div>

      {restricted && (
        <p className="text-xs leading-snug text-ink-2" data-testid="acl-blocked-summary">
          {acl.persona.blocked_summary}.
        </p>
      )}

      {restricted && cap && !cap.supports_chunk_acl && (
        <div
          role="status"
          data-testid="acl-rag-engine-warning"
          className="flex items-start gap-1.5 rounded border border-clay bg-surface-0 p-2 text-xs text-ink-1"
        >
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-clay" aria-hidden />
          <span>
            <strong className="font-semibold">Corpus-boundary IAM only:</strong> {cap.honest_note}
          </span>
        </div>
      )}

      {restricted && cap?.supports_chunk_acl && demoMode === "replay" && sampleQids.length > 0 && (
        <div
          role="status"
          data-testid="acl-replay-demo-hint"
          className="flex items-start gap-1.5 rounded border border-moss bg-surface-0 p-2 text-xs text-ink-2"
        >
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden />
          <span>
            Recorded ACL refusal demo{sampleQids.length === 1 ? "" : "s"}:{" "}
            <span className="font-mono text-ink-1">{sampleQids.join(", ")}</span>{" "}
            ({relevantDemos[0].synthesis_model} + {relevantDemos[0].judge_model}).
          </span>
        </div>
      )}
    </div>
  );
};
