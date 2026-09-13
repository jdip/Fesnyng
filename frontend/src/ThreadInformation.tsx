import { useEffect, useRef, useState } from 'react';
import { PencilIcon } from 'lucide-react';
import { repositoryLabel, useThreadContext } from './thread-context';

export type ThreadInformationProps = {
  baseUrl: string; csrfToken: string; session: { id: string; title: string };
  refreshKey: string | number; onRename: (title: string) => Promise<void>;
};

export function ThreadInformation({ baseUrl, csrfToken, session, refreshKey, onRename }: ThreadInformationProps) {
  const { data, failed } = useThreadContext(baseUrl, csrfToken, session.id, refreshKey);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (editing) input.current?.select(); }, [editing]);
  const close = () => { setEditing(false); setError(''); trigger.current?.focus(); };
  const changes = data?.changes;
  const noRepository = data?.repository.state === 'absent';
  return <section className="thread-information" aria-label="Thread information">
    <div className="thread-information-title">
      <h2 title={session.title}>{session.title}</h2>
      <button ref={trigger} type="button" className="sidebar-icon-button" aria-label="Rename thread" title="Rename thread" onClick={() => { setDraft(session.title); setEditing(true); }}><PencilIcon aria-hidden="true" /></button>
    </div>
    {editing && <form className="thread-title-form" onSubmit={(event) => {
      event.preventDefault(); const title = draft.trim(); if (!title || saving) return;
      setSaving(true); setError('');
      void onRename(title).then(close).catch(() => setError('Could not rename this thread. Try again.')).finally(() => setSaving(false));
    }}><input ref={input} className="app-input" aria-label="Thread title" required maxLength={120} value={draft} disabled={saving} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Escape' && !saving) { event.preventDefault(); close(); } }} /><button className="app-button" disabled={saving}>Save title</button><button className="app-button quiet" type="button" disabled={saving} onClick={close}>Cancel</button>{error && <p role="alert" className="app-error">{error}</p>}</form>}
    <dl className="thread-information-fields">
      <div aria-label="Repository"><dt>Repository</dt><dd>{!data && !failed ? 'Loading…' : repositoryLabel(data?.repository)}</dd></div>
      <div aria-label="Branch"><dt>Branch</dt><dd>{data?.branch.state === 'available' ? data.branch.name ?? 'Detached HEAD' : noRepository ? 'Not applicable' : 'Unavailable'}</dd></div>
      <div aria-label="Git changes"><dt>Git changes</dt><dd>{changes?.state === 'available' ? <><span title="Tracked text changes against HEAD, staged and unstaged">+{changes.added} / −{changes.deleted}</span><small>{changes.untracked} untracked · {changes.binaryFiles} binary</small></> : noRepository ? 'Not applicable' : changes?.reason === 'unborn' ? 'Unavailable (no HEAD commit)' : 'Unavailable'}</dd></div>
      <div aria-label="Subagents"><dt title="Native child sessions">Subagents</dt><dd>{data?.subagents.state === 'available' ? data.subagents.count : 'Unavailable'}</dd></div>
      <div aria-label="Background processes"><dt>Background processes</dt><dd>{data?.backgroundProcesses.state === 'available' ? data.backgroundProcesses.count : 'Unavailable'}</dd></div>
    </dl>
    {failed && <p className="muted" role="status">Thread context could not be refreshed. Use Refresh to try again.</p>}
  </section>;
}
