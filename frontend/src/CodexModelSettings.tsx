import { useEffect, useRef, useState } from 'react';
import { api, errorMessage, type Configuration } from './workspace-api';

type Model = {
  model: string;
  displayName: string;
  defaultReasoningEffort: string;
  supportedReasoningEfforts: { reasoningEffort: string; description: string }[];
};
type Props = { organization: string; host: string; configuration: Configuration; onChange: (configuration: Configuration) => void };

export function CodexModelSettings(props: Props) {
  return <AccountModels key={`${props.organization}/${props.host}/${props.configuration.profile_id}`} {...props} />;
}

function AccountModels({ organization, host, configuration, onChange }: Props) {
  const [catalog, setCatalog] = useState<Model[]>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const effortSelect = useRef<HTMLSelectElement>(null);
  const profile = configuration.profile_id;
  useEffect(() => {
    if (!host || !profile) return;
    const controller = new AbortController();
    void api<{ data: Model[] }>(`/organizations/${organization}/hosts/${host}/profiles/${profile}/codex/models`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (!Array.isArray(result.data)) throw new Error('Model discovery returned an invalid response.');
        setCatalog(result.data);
      })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organization, host, profile, attempt]);
  const selected = catalog?.find((item) => item.model === configuration.model);
  const effort = configuration.reasoning_effort ?? '';
  const unsupported = Boolean(effort && selected && !selected.supportedReasoningEfforts.some((item) => item.reasoningEffort === effort));
  useEffect(() => {
    effortSelect.current?.setCustomValidity(unsupported ? 'Choose a supported thinking level or Model default.' : '');
  }, [unsupported]);
  return <>
    <label>Model<select className="app-select" value={configuration.model} disabled={!catalog} onChange={(event) => {
      const model = catalog?.find((item) => item.model === event.target.value);
      onChange({ ...configuration, model: event.target.value, reasoning_effort: model?.supportedReasoningEfforts.some((item) => item.reasoningEffort === effort) ? effort : null });
    }}>
      {!selected && <option value={configuration.model}>{configuration.model} (current selection)</option>}
      {catalog?.map((item) => <option key={item.model} value={item.model}>{item.displayName}</option>)}
    </select></label>
    <label>Default thinking level<select aria-label="Default thinking level" ref={effortSelect} className="app-select" value={effort} disabled={!selected} onChange={(event) => onChange({ ...configuration, reasoning_effort: event.target.value || null })}>
      <option value="">Model default{selected ? ` (${selected.defaultReasoningEffort})` : ''}</option>
      {effort && !selected?.supportedReasoningEfforts.some((item) => item.reasoningEffort === effort) && <option value={effort}>{effort} (current selection)</option>}
      {selected?.supportedReasoningEfforts.map((item) => <option key={item.reasoningEffort} value={item.reasoningEffort}>{item.reasoningEffort} — {item.description}</option>)}
    </select><small className="muted">Applies on subsequent turns in new and existing conversations.</small></label>
    {!host || !profile ? <p className="muted">Choose a home host and authenticated credential profile to load account models.</p> : error ? <p className="app-error" role="alert">Could not load account models: {error} <button className="app-button" type="button" onClick={() => { setError(''); setCatalog(undefined); setAttempt(attempt + 1); }}>Retry model discovery</button></p> : !catalog ? <p className="muted" role="status">Loading account models…</p> : !selected ? <p className="muted">The current model is not in this account’s catalog. Choose an available model before applying execution changes.</p> : null}
    {unsupported && <p className="app-error" role="alert">Choose a supported thinking level or Model default.</p>}
  </>;
}
