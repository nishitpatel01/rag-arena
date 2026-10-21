import React from "react";
import type { ModelRole, ModelsResponse, RecordedPair } from "../../types/arena";
import {
  findModel,
  formatModelPrice,
  modelLabel,
  optionStates,
  providerLabel,
  safeHttpUrl,
  type ModelSelection,
  type ReplayGate,
} from "./modelSelection";

export interface ModelPickerProps {
  readonly catalog: ModelsResponse | null;
  readonly loading: boolean;
  /** Verbatim GET /api/v1/models error; when set the picker is disabled and the server defaults apply. */
  readonly error: string | null;
  readonly selection: ModelSelection | null;
  readonly onChange: (sel: ModelSelection) => void;
  readonly disabled: boolean;
  readonly validationError: string | null;
  readonly replay: ReplayGate | null;
}

const fieldClass = "w-full rounded-md border border-line bg-surface-0 px-2 py-1.5 text-xs text-ink-1 disabled:opacity-60";

interface RoleSelectProps {
  readonly role: ModelRole;
  readonly label: string;
  readonly catalog: ModelsResponse;
  readonly value: string;
  readonly other: string;
  readonly disabled: boolean;
  readonly onSelect: (id: string) => void;
}

const RoleSelect: React.FC<RoleSelectProps> = ({ role, label, catalog, value, other, disabled, onSelect }) => {
  const id = `model-${role}`;
  const options = optionStates(catalog, role, other);
  const selected = findModel(catalog, value);
  const priceUrl = safeHttpUrl(selected?.price_source);
  const blocked = options.filter((o) => o.disabled);
  const known = options.some((o) => o.entry.id === value);
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-ink-2">{label}</label>
      <select id={id} value={value} onChange={(e) => onSelect(e.target.value)} disabled={disabled} className={fieldClass} data-role={role}>
        {!known && <option value={value}>{value} (not offered for this role)</option>}
        {options.map((o) => (
          <option key={o.entry.id} value={o.entry.id} disabled={o.disabled} title={o.reason ?? undefined}>
            {o.entry.label} · {providerLabel(o.entry.provider)} · {o.entry.location}
            {o.unavailable ? " — unavailable" : o.disabled ? (role === "judge" ? " — used for synthesis" : " — used as judge") : ""}
          </option>
        ))}
      </select>
      {selected && (
        <p className="text-xs text-ink-3" data-testid={`price-${role}`}>
          <span className="font-mono">{formatModelPrice(selected)}</span>
          {priceUrl && (
            <>
              {" · "}
              <a href={priceUrl} target="_blank" rel="noreferrer" className="text-moss underline">price source</a>
            </>
          )}
        </p>
      )}
      {blocked.length > 0 && (
        <ul className="text-xs text-ink-3" aria-label={`${label}: options that cannot be chosen`}>
          {blocked.map((o) => (
            <li key={o.entry.id} className="break-words">
              <span className="text-ink-2">{o.entry.label}</span>: {o.reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

const pairText = (catalog: ModelsResponse, p: RecordedPair): string =>
  `${modelLabel(catalog, p.synthesis_model)} → judge ${modelLabel(catalog, p.judge_model)}`;

/**
 * Synthesis + judge model selects (spec §7.2). Options, availability, prices and replay recordings
 * all come from GET /api/v1/models; the judge must differ from the synthesis model.
 */
export const ModelPicker: React.FC<ModelPickerProps> = ({ catalog, loading, error, selection, onChange, disabled, validationError, replay }) => {
  if (error || !catalog || !selection) {
    return (
      <fieldset className="flex flex-col gap-1 rounded-md border border-line p-2" disabled aria-busy={loading}>
        <legend className="px-1 text-xs font-medium text-ink-2">Models</legend>
        <select aria-label="Synthesis model" disabled className={fieldClass}><option>server default</option></select>
        <select aria-label="Judge model" disabled className={fieldClass}><option>server default</option></select>
        {loading ? (
          <p className="text-xs text-ink-3">Loading model catalog…</p>
        ) : (
          <p className="text-xs text-clay" role="alert">
            Model catalog unavailable — runs use the server defaults. {error ?? "No catalog returned."}
          </p>
        )}
      </fieldset>
    );
  }
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-line p-2" data-testid="model-picker">
      <legend className="px-1 text-xs font-medium text-ink-2">Models</legend>
      <RoleSelect role="synthesis" label="Synthesis model" catalog={catalog} value={selection.synthesis} other={selection.judge} disabled={disabled}
        onSelect={(id) => onChange({ ...selection, synthesis: id })} />
      <RoleSelect role="judge" label="Judge model" catalog={catalog} value={selection.judge} other={selection.synthesis} disabled={disabled}
        onSelect={(id) => onChange({ ...selection, judge: id })} />
      {validationError && <p className="text-xs text-err" role="alert">{validationError}</p>}
      {replay?.replay && (
        <div className="rounded-md border border-line bg-surface-1 p-2 text-xs text-ink-2" role="status" data-testid="replay-pairs">
          <p className="font-medium text-ink-1">Replay mode: only recorded model pairs can play.</p>
          {replay.pairs.length === 0 ? (
            <p>No recorded pairs for this question.</p>
          ) : (
            <ul className="mt-1 flex flex-col gap-0.5">
              {replay.pairs.map((p) => {
                const current = p.synthesis_model === selection.synthesis && p.judge_model === selection.judge;
                return (
                  <li key={`${p.synthesis_model}|${p.judge_model}`}>
                    {current ? (
                      <span className="font-medium text-ok">{pairText(catalog, p)} (selected)</span>
                    ) : (
                      <button type="button" disabled={disabled} className="text-left text-moss underline disabled:opacity-60"
                        onClick={() => onChange({ synthesis: p.synthesis_model, judge: p.judge_model })}>
                        Use {pairText(catalog, p)}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {replay.blockedReason && <p className="mt-1 text-clay" role="alert">Run disabled: {replay.blockedReason}</p>}
        </div>
      )}
    </fieldset>
  );
};
