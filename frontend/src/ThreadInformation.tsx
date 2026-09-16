import { useEffect, useRef, useState } from 'react';
import { FolderIcon, PencilIcon } from 'lucide-react';
import { repositoryLabel, useThreadContext } from './thread-context';
import { WorkspaceInspection } from './WorkspaceInspection';
import type { WorkspaceInspection as Inspection } from './workspace-api';

export type ThreadWorkspace = {
  organization: string;
  agent: string;
  csrf: string;
  revision?: number;
  onChanged?: (state: Inspection['state'] | undefined) => void;
  onOperation?: (inspection: Inspection) => void;
};

export type ThreadInformationProps = {
  baseUrl?: string; csrfToken: string; session: { id: string; title: string };
  refreshKey: string | number; onRename: (title: string) => Promise<void>;
  workspace?: ThreadWorkspace;
  onOpenFiles?: (trigger: HTMLButtonElement) => void;
  interactionDisabled?: boolean;
};

const workspaceRepositoryLabel = (inspection?: Inspection) => {
  if (!inspection) return 'Loading workspace…';
  if (inspection.repository.state !== 'available') return inspection.repository.state === 'absent' ? 'No repository' : 'Repository unavailable';
  const value = inspection.repository.url;
  if (!value) return 'Repository available';
  return value.replace(/\/$/, '').split('/').at(-1)?.replace(/\.git$/, '') || value;
};

const workspaceBranchLabel = (inspection?: Inspection) => {
  if (!inspection) return 'Loading branch…';
  if (inspection.repository.state === 'absent') return 'Not applicable';
  return inspection.git.branch ?? inspection.repository.working_branch ?? 'Unavailable';
};

export function ThreadInformation({ baseUrl, csrfToken, session, refreshKey, onRename, workspace, onOpenFiles, interactionDisabled = false }: ThreadInformationProps) {
  const { data, failed } = useThreadContext(baseUrl, csrfToken, session.id, refreshKey);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [inspection, setInspection] = useState<Inspection>();
  const [inspectionState, setInspectionState] = useState<'loading' | 'available' | 'unavailable'>('loading');
  useEffect(() => { if (editing) input.current?.select(); }, [editing]);
  const close = () => { setEditing(false); setError(''); trigger.current?.focus(); };
  const changes = data?.changes;
  const noRepository = data?.repository.state === 'absent';
  const workspaceUnavailable = inspectionState === 'unavailable';
  const repository = data ? repositoryLabel(data.repository) : workspace ? workspaceUnavailable ? 'Repository unavailable' : workspaceRepositoryLabel(inspection) : failed ? 'Repository unavailable' : 'Loading context…';
  const branch = data?.branch.state === 'available' ? data.branch.name ?? 'Detached HEAD' : data ? noRepository ? 'Not applicable' : 'Unavailable' : workspace ? workspaceUnavailable ? 'Unavailable' : workspaceBranchLabel(inspection) : failed ? 'Unavailable' : 'Loading context…';
  return <section className="thread-information" aria-label="Thread information">
    <div className="thread-information-summary">
      <div className="thread-information-title">
        <h2 title={session.title}>{session.title}</h2>
        <p aria-label="Repository and branch">{repository}<span aria-hidden="true"> · </span>{branch}</p>
      </div>
      <div className="thread-information-actions">
        {onOpenFiles && !interactionDisabled && <button type="button" className="app-button quiet" aria-label="Files" onClick={(event) => onOpenFiles(event.currentTarget)}><FolderIcon aria-hidden="true" />Files</button>}
        {!interactionDisabled && <button ref={trigger} type="button" className="sidebar-icon-button" aria-label="Rename thread" title="Rename thread" onClick={() => { setDraft(session.title); setEditing(true); }}><PencilIcon aria-hidden="true" /></button>}
      </div>
    </div>
    {editing && <form className="thread-title-form" onSubmit={(event) => {
      event.preventDefault(); const title = draft.trim(); if (!title || saving || interactionDisabled) return;
      setSaving(true); setError('');
      void onRename(title).then(close).catch(() => setError('Could not rename this thread. Try again.')).finally(() => setSaving(false));
    }}><input ref={input} className="app-input" aria-label="Thread title" required maxLength={120} value={draft} disabled={saving || interactionDisabled} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Escape' && !saving) { event.preventDefault(); close(); } }} /><button className="app-button" disabled={saving || interactionDisabled}>Save title</button><button className="app-button quiet" type="button" disabled={saving} onClick={close}>Cancel</button>{error && <p role="alert" className="app-error">{error}</p>}</form>}
    <details className="thread-information-details">
      <summary>Workspace details</summary>
      {data && <dl className="thread-information-fields">
      <div aria-label="Repository"><dt>Repository</dt><dd>{!data && !failed ? 'Loading…' : repositoryLabel(data?.repository)}</dd></div>
      <div aria-label="Branch"><dt>Branch</dt><dd>{branch}</dd></div>
      <div aria-label="Git changes"><dt>Git changes</dt><dd>{changes?.state === 'available' ? <><span title="Tracked text changes against HEAD, staged and unstaged">+{changes.added} / −{changes.deleted}</span><small>{changes.untracked} untracked · {changes.binaryFiles} binary</small></> : noRepository ? 'Not applicable' : changes?.reason === 'unborn' ? 'Unavailable (no HEAD commit)' : 'Unavailable'}</dd></div>
      <div aria-label="Subagents"><dt title="Native child sessions">Subagents</dt><dd>{data?.subagents.state === 'available' ? data.subagents.count : 'Unavailable'}</dd></div>
      <div aria-label="Background processes"><dt>Background processes</dt><dd>{data?.backgroundProcesses.state === 'available' ? data.backgroundProcesses.count : 'Unavailable'}</dd></div>
      </dl>}
      {failed && <p className="muted" role="status">Thread context could not be refreshed. Use Refresh to try again.</p>}
      {workspace && <WorkspaceInspection organization={workspace.organization} agent={workspace.agent} session={session.id} csrf={workspace.csrf} compact revision={workspace.revision} onChanged={workspace.onChanged} onOperation={workspace.onOperation} onInspection={(next) => { setInspection(next); setInspectionState('available'); }} onUnavailable={() => { setInspection(undefined); setInspectionState('unavailable'); }} />}
    </details>
  </section>;
}
