import { useEffect, useState } from 'react';
import { agentPath, api, errorMessage, type Memory } from './workspace-api';
export function AgentMemory({ organization, agent, csrf }: { organization: string; agent: string; csrf: string }) {
  const [entries, setEntries] = useState<Memory[]>([]);
  const [draft, setDraft] = useState({ key: '', content: '', revision: 0 });
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const path = `${agentPath(organization, agent)}/memory`;
  useEffect(() => {
    const controller = new AbortController();
    void api<Memory[]>(path, { signal: controller.signal }).then(setEntries).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [path, reload]);
  return <div className="workspace-page"><h2>Agent memory</h2><p className="page-intro">Durable notes this agent can inspect and update across its threads. Project documents remain the shared source of truth.</p><div className="memory-layout"><nav className="memory-list" aria-label="Memory entries"><button className="app-button" onClick={() => { setDraft({ key: '', content: '', revision: 0 }); setError(''); setNotice(''); }}>+ New memory</button>{entries.map((item) => <button className="app-button" key={item.key} onClick={() => { setDraft(item); setError(''); setNotice(''); }}>{item.key}</button>)}<button className="app-button quiet" onClick={() => setReload((value) => value + 1)}>Refresh entries</button></nav><form className="app-form" onSubmit={(event) => {
    event.preventDefault(); setError(''); setNotice(''); setBusy(true);
    void api<Memory>(path, { method: 'PUT', csrf, body: { key: draft.key, content: draft.content, expected_revision: draft.revision } }).then((saved) => { setDraft(saved); setEntries((items) => [...items.filter((item) => item.key !== saved.key), saved].sort((a, b) => a.key.localeCompare(b.key))); setNotice('Memory saved.'); }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  }}><label>Memory key<input className="app-input" required pattern="[a-z0-9][a-z0-9_-]{0,63}" value={draft.key} disabled={draft.revision > 0} onChange={(event) => setDraft({ ...draft, key: event.target.value })} /></label><label>Memory content<textarea className="app-textarea" rows={14} maxLength={200000} value={draft.content} onChange={(event) => setDraft({ ...draft, content: event.target.value })} /></label><p className="muted">{draft.revision ? `Revision ${draft.revision}. Refresh and select the entry again to load another participant’s changes.` : 'New explicit memory entry.'}</p>{error && <p className="app-error" role="alert">{error}</p>}{notice && <p className="app-notice" role="status">{notice}</p>}<div><button className="app-button primary" disabled={busy}>Save memory</button></div></form></div></div>;
}
