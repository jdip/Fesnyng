import { useEffect, useEffectEvent, useState } from 'react';
import { api, errorMessage } from './workspace-api';
type Preference = { thread_list_page_size: number };
function pageSize(value: Preference) {
  if (!Number.isInteger(value.thread_list_page_size) || value.thread_list_page_size < 1 || value.thread_list_page_size > 100) throw new Error('Could not read your thread list preference.');
  return value.thread_list_page_size;
}
export function ThreadPagePreference({ organization, csrf, onChange }: { organization: string; csrf: string; onChange: (size: number) => void }) {
  const [draft, setDraft] = useState('6');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  const changed = useEffectEvent(onChange);
  const path = `/organizations/${organization}/workspace-preferences`;
  useEffect(() => {
    const controller = new AbortController();
    void api<Preference>(path, { signal: controller.signal }).then((value) => {
      const size = pageSize(value);
      if (!controller.signal.aborted) { setDraft(String(size)); changed(size); setError(''); }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [path, revision]);
  return <details className="thread-list-preferences"><summary>Thread list settings</summary>
    <form onSubmit={(event) => {
      event.preventDefault(); setSaving(true);
      void api<Preference>(path, { method: 'PUT', csrf, body: { thread_list_page_size: Number(draft) } }).then((value) => {
        const size = pageSize(value); onChange(size); setDraft(String(size)); setError('');
      }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setSaving(false));
    }}><label>Threads per page<input className="app-input" type="number" min={1} max={100} step={1} required value={draft} disabled={saving} onChange={(event) => setDraft(event.target.value)} /></label><button className="app-button quiet" disabled={saving} aria-label="Save page size">Save</button></form>
    {error && <><p className="app-error" role="alert">{error}</p><button className="app-button quiet" onClick={() => setRevision((value) => value + 1)}>Retry preference</button></>}
  </details>;
}
