import { useEffect, useEffectEvent, useState, type FormEvent } from 'react';
import { api, errorMessage } from './workspace-api';
type Preference = { thread_list_page_size: number };
function pageSize(value: Preference) {
  if (!Number.isInteger(value.thread_list_page_size) || value.thread_list_page_size < 1 || value.thread_list_page_size > 100) throw new Error('Could not read your thread list preference.');
  return value.thread_list_page_size;
}
type ThreadPagePreferenceOptions = { organization: string; csrf: string; onChange: (size: number) => void };
export type ThreadPagePreferenceState = { draft: string; setDraft: (value: string) => void; error: string; notice: string; saving: boolean; save: (event: FormEvent<HTMLFormElement>) => void; retry: () => void };

export function useThreadPagePreference({ organization, csrf, onChange }: ThreadPagePreferenceOptions): ThreadPagePreferenceState {
  const [draft, assignDraft] = useState('6');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  const changed = useEffectEvent(onChange);
  const path = `/organizations/${organization}/workspace-preferences`;
  useEffect(() => {
    const controller = new AbortController();
    void api<Preference>(path, { signal: controller.signal }).then((value) => {
      const size = pageSize(value);
      if (!controller.signal.aborted) { assignDraft(String(size)); changed(size); setError(''); }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [path, revision]);
  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setNotice('');
    void api<Preference>(path, { method: 'PUT', csrf, body: { thread_list_page_size: Number(draft) } }).then((value) => {
      const size = pageSize(value);
      onChange(size);
      assignDraft(String(size));
      setError('');
      setNotice('Preference saved.');
    }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setSaving(false));
  };
  return { draft, setDraft: (value) => { assignDraft(value); setNotice(''); }, error, notice, saving, save, retry: () => setRevision((value) => value + 1) };
}

export function ThreadPagePreferenceForm({ preference }: { preference: ThreadPagePreferenceState }) {
  return <section className="thread-list-preferences">
    <form onSubmit={preference.save}><label>Threads per page<input className="app-input" type="number" min={1} max={100} step={1} required value={preference.draft} disabled={preference.saving} onChange={(event) => preference.setDraft(event.target.value)} /></label><button className="app-button primary" disabled={preference.saving} aria-label="Save page size">{preference.saving ? 'Saving…' : 'Save'}</button></form>
    {preference.notice && <p className="app-notice" role="status">{preference.notice}</p>}
    {preference.error && <><p className="app-error" role="alert">{preference.error}</p><button className="app-button quiet" onClick={preference.retry}>Retry preference</button></>}
  </section>;
}
