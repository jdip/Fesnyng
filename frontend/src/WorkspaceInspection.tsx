import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { agentPath, api, errorMessage, workspaceInspection, type WorkspaceInspection as Inspection } from './workspace-api';

export type WorkspaceUpdate = { organization: string; agent: string; session: string; inspection: Inspection };

type Props = {
  organization: string;
  agent: string;
  session: string;
  csrf: string;
  compact?: boolean;
  revision?: number;
  onChanged?: (state: Inspection['state'] | undefined) => void;
  onOperation?: (inspection: Inspection) => void;
  onInspection?: (inspection: Inspection) => void;
  onUnavailable?: () => void;
};

const stateLabel: Record<Inspection['state'], string> = {
  ready: 'Ready', removed: 'Removed', legacy: 'Legacy workspace', unavailable: 'Unavailable', removing: 'Removing', replacing: 'Preparing replacement',
};

const kindLabel: Record<Inspection['kind'], string> = {
  repository: 'Repository worktree', ordinary: 'Ordinary directory', fork: 'Forked worktree', legacy: 'Legacy workspace', unavailable: 'Unavailable',
};

const workspacePath = (organization: string, agent: string, session: string) => `${agentPath(organization, agent)}/sessions/${encodeURIComponent(session)}/workspace`;
const expectation = (inspection: Inspection) => {
  const { workspace_id: workspaceId, generation, safety_digest: safetyDigest } = inspection;
  return typeof workspaceId === 'string' && workspaceId.length > 0 && typeof generation === 'number' && Number.isSafeInteger(generation) && generation >= 0 && typeof safetyDigest === 'string' && /^[0-9a-f]{64}$/.test(safetyDigest)
    ? { workspace_id: workspaceId, generation, safety_digest: safetyDigest }
    : undefined;
};
const evidenceKey = (inspection: Inspection) => {
  const evidence = expectation(inspection);
  return evidence && `${evidence.workspace_id}:${evidence.generation}:${evidence.safety_digest}`;
};

export function WorkspaceInspection({ organization, agent, session, csrf, compact = false, revision = 0, onChanged, onOperation, onInspection, onUnavailable }: Props) {
  const path = workspacePath(organization, agent, session);
  const [result, setResult] = useState<{ path: string; inspection?: Inspection; error?: string }>({ path: '' });
  const [busy, setBusy] = useState<{ path: string; operation: 'remove' | 'discard' | 'replace' }>();
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [discardAcknowledged, setDiscardAcknowledged] = useState(false);
  const [discardFingerprint, setDiscardFingerprint] = useState<string>();
  const mounted = useRef(false);
  const request = useRef(0);
  const currentPath = useRef(path);
  useLayoutEffect(() => {
    currentPath.current = path;
    return () => { mounted.current = false; };
  }, [path]);

  const current = (token: number) => mounted.current && currentPath.current === path && request.current === token;
  const apply = (inspection: Inspection, token: number) => {
    if (!current(token)) return;
    setConfirmDiscard(false);
    setDiscardAcknowledged(false);
    setDiscardFingerprint(undefined);
    setResult({ path, inspection });
    onChanged?.(inspection.state);
    onInspection?.(inspection);
  };
  const refresh = (signal?: AbortSignal, afterOperation = false) => {
    const token = ++request.current;
    void api<unknown>(path, { signal }).then(workspaceInspection).then((inspection) => { apply(inspection, token); if (afterOperation && current(token)) onOperation?.(inspection); }).catch((cause: unknown) => {
      if (signal?.aborted || !current(token)) return;
      setResult({ path, error: errorMessage(cause) });
      onUnavailable?.();
    });
  };
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    refresh(controller.signal);
    return () => { mounted.current = false; controller.abort(); };
  // `path` and `revision` identify a current host receipt without remounting native history.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, revision]);
  const visible = result.path === path ? result : { path };
  const activeOperation = busy?.path === path ? busy.operation : undefined;
  const operate = (operation: 'remove' | 'discard' | 'replace') => {
    const inspection = visible.inspection;
    const evidence = inspection && expectation(inspection);
    if (!inspection || !evidence || activeOperation) return;
    if (operation === 'discard' && (!discardAcknowledged || discardFingerprint !== evidenceKey(inspection))) return;
    const token = ++request.current;
    setBusy({ path, operation });
    void api<unknown>(`${path}/${operation}`, {
      method: 'POST', csrf,
      body: evidence,
    }).then(workspaceInspection).then((next) => {
      if (!current(token)) return;
      apply(next, token);
      onOperation?.(next);
    }).catch((cause: unknown) => {
      if (!current(token)) return;
      // A stale or ambiguous mutation receipt cannot authorize a second cleanup attempt.
      setResult({ path, error: errorMessage(cause) });
      setBusy((currentBusy) => currentBusy?.path === path ? undefined : currentBusy);
      refresh(undefined, true);
    }).finally(() => { if (current(token)) setBusy((currentBusy) => currentBusy?.path === path ? undefined : currentBusy); });
  };
  if (!visible.inspection && !visible.error) return <section className="workspace-inspection" aria-label="Workspace"><p role="status" className="muted">Inspecting workspace…</p></section>;
  if (!visible.inspection) return <section className="workspace-inspection" aria-label="Workspace"><p role="status" className="muted">Workspace evidence is unavailable. Existing thread access remains unchanged. {visible.error}</p><button className="app-button quiet" onClick={() => refresh()}>Inspect again</button></section>;
  const inspection = visible.inspection;
  const can = inspection.cleanup;
  const evidence = expectation(inspection);
  const action = (operation: 'remove' | 'discard' | 'replace', label: string, available: boolean, reason?: string, danger = false) => <div className="workspace-action"><button className={`app-button${danger ? ' danger' : ''}`} disabled={!available || Boolean(activeOperation)} title={!available ? reason : undefined} onClick={() => { if (operation === 'discard') { setDiscardAcknowledged(false); setDiscardFingerprint(evidenceKey(inspection)); setConfirmDiscard(true); } else operate(operation); }}>{activeOperation === operation ? `${label}…` : label}</button>{!available && reason && <small className="muted">{reason}</small>}</div>;
  return <section className={`workspace-inspection${compact ? ' compact' : ''}`} aria-label="Workspace">
    <div className="workspace-inspection-heading"><div><h3>Workspace</h3><p className="muted">{stateLabel[inspection.state]} · {kindLabel[inspection.kind]}</p></div><button className="app-button quiet" disabled={Boolean(busy)} onClick={() => refresh()}>Inspect again</button></div>
    <dl className="workspace-inspection-fields">
      <div className="workspace-location"><dt>Location</dt><dd>{inspection.directory ?? 'Unavailable'}</dd></div>
      <div><dt>Repository</dt><dd>{inspection.repository.url ?? inspection.repository.state}</dd></div>
      <div><dt>Branch</dt><dd>{inspection.git.branch ?? inspection.repository.working_branch ?? 'Unavailable'}</dd></div>
      <div><dt>Git safety</dt><dd>{gitSafety(inspection)}</dd></div>
      <div><dt>History</dt><dd>{inspection.history.state === 'verified' ? 'Verified' : 'Unavailable'}</dd></div>
    </dl>
    {inspection.state === 'legacy' && <p className="app-notice" role="status">This legacy workspace keeps its established execution path. It cannot be removed or migrated automatically.</p>}
    {inspection.state === 'unavailable' && <p className="app-notice" role="status">The host could not verify this workspace. Inspect again before relying on cleanup evidence.</p>}
    {visible.error && <p className="app-error" role="alert">{visible.error}</p>}
    <div className="workspace-actions">
      {action('remove', 'Remove workspace', inspection.state === 'ready' && can.remove.available && Boolean(evidence), inspection.state === 'ready' ? can.remove.reason ?? (evidence ? undefined : 'Workspace safety evidence is unavailable.') : 'Inspect a ready workspace before removal.')}
      {action('discard', 'Discard workspace', inspection.state === 'ready' && can.discard.available && Boolean(evidence), inspection.state === 'ready' ? can.discard.reason ?? (evidence ? undefined : 'Workspace safety evidence is unavailable.') : 'Inspect a ready workspace before discard.', true)}
      {action('replace', 'Prepare replacement', inspection.state === 'removed' && can.replace.available && Boolean(evidence), inspection.state === 'removed' ? can.replace.reason ?? (evidence ? undefined : 'Workspace safety evidence is unavailable.') : 'Remove this workspace before preparing its replacement.')}
    </div>
    {confirmDiscard && discardFingerprint === evidenceKey(inspection) && <form className="workspace-discard-confirmation" onSubmit={(event) => { event.preventDefault(); operate('discard'); }}><p>This permanently deletes the files in {inspection.directory ?? 'this workspace'}.</p><p><strong>Known loss:</strong> {discardLoss(inspection)}.</p><p className="muted">Conversation history and Project grouping remain. Linked services are not stopped.</p><label><input type="checkbox" checked={discardAcknowledged} onChange={(event) => setDiscardAcknowledged(event.target.checked)} aria-label="I understand this deletes the workspace files and cannot be undone." />I understand this deletes the workspace files and cannot be undone.</label><div className="app-actions"><button type="button" className="app-button" disabled={Boolean(activeOperation)} onClick={() => { setDiscardAcknowledged(false); setDiscardFingerprint(undefined); setConfirmDiscard(false); }}>Cancel</button><button className="app-button danger" disabled={!discardAcknowledged || discardFingerprint !== evidenceKey(inspection) || Boolean(activeOperation)}>Discard workspace permanently</button></div></form>}
  </section>;
}

function discardLoss(inspection: Inspection) {
  if (inspection.git.state === 'unavailable') return 'the host could not enumerate file changes';
  if (inspection.git.kind === 'ordinary') return typeof inspection.git.entries === 'number'
    ? `${inspection.git.entries} ${inspection.git.entries === 1 ? 'entry' : 'entries'} in this directory`
    : 'the host did not report the directory entry count';
  const parts = [
    `${inspection.git.dirty ?? 'Unknown'} modified ${inspection.git.dirty === 1 ? 'file' : 'files'}`,
    `${inspection.git.untracked ?? 'Unknown'} untracked ${inspection.git.untracked === 1 ? 'file' : 'files'}`,
    `${inspection.git.ignored ?? 'Unknown'} ignored ${inspection.git.ignored === 1 ? 'file' : 'files'}`,
  ];
  if (inspection.git.ahead === undefined) parts.push('unknown unpushed commits');
  else if (inspection.git.ahead) parts.push(`${inspection.git.ahead} commit${inspection.git.ahead === 1 ? '' : 's'} ahead of upstream`);
  return parts.join(', ');
}

function gitSafety(inspection: Inspection) {
  if (inspection.git.state === 'unavailable') return 'Unverified';
  if (inspection.git.state === 'safe') return 'Clean';
  if (inspection.git.kind === 'ordinary') return typeof inspection.git.entries === 'number' ? `${inspection.git.entries} directory ${inspection.git.entries === 1 ? 'entry' : 'entries'}` : 'Unknown directory entries';
  return `${inspection.git.dirty ?? 'Unknown'} modified · ${inspection.git.untracked ?? 'Unknown'} untracked · ${inspection.git.ignored ?? 'Unknown'} ignored · ${inspection.git.ahead ?? 'Unknown'} ahead`;
}

export function WorkspaceInspectionList({ organization, agent, sessions, csrf, onOpen, onOperation, showHeading = true }: { organization: string; agent: string; sessions: Array<{ id: string; title: string; project?: string }>; csrf: string; onOpen: (session: string) => void; onOperation?: (update: WorkspaceUpdate) => void; showHeading?: boolean }) {
  return <section className="workspace-inspection-list" aria-label="Workspaces">{showHeading && <h3>Workspaces</h3>}{sessions.map((session) => <article key={session.id} className="workspace-list-item"><button className="project-thread-open" onClick={() => onOpen(session.id)}>{session.title}{session.project && <small>{session.project}</small>}</button><WorkspaceInspection compact organization={organization} agent={agent} session={session.id} csrf={csrf} onOperation={onOperation ? (inspection) => onOperation({ organization, agent, session: session.id, inspection }) : undefined} /></article>)}{!sessions.length && <p className="muted">No workspace threads are available.</p>}</section>;
}

export function EmployeeWorkspaces({ organization, agent, csrf, onOpen, onOperation }: { organization: string; agent: string; csrf: string; onOpen: (session: string) => void; onOperation?: (update: WorkspaceUpdate) => void }) {
  const key = `${organization}:${agent}`;
  const [loaded, setLoaded] = useState<{ key: string; sessions?: Array<{ id: string; title: string; project?: string }>; error?: string }>({ key: '' });
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      api<unknown>(`${agentPath(organization, agent)}/sessions`, { signal: controller.signal }),
      api<{ threads: Array<{ session_id: string; project_id: string | null }> }>(`${agentPath(organization, agent)}/thread-projects`, { signal: controller.signal }),
      api<Array<{ id: string; name: string }>>(`/organizations/${encodeURIComponent(organization)}/projects?include_archived=true`, { signal: controller.signal }),
    ]).then(([inventory, grouping, projects]) => {
      if (controller.signal.aborted || !Array.isArray(inventory)) return;
      const projectNames = new Map(projects.map((project) => [project.id, project.name]));
      const assignments = new Map(grouping.threads.map((thread) => [thread.session_id, thread.project_id]));
      const sessions = inventory.flatMap((item) => item && typeof item === 'object' && typeof (item as Record<string, unknown>).session_id === 'string' && typeof (item as Record<string, unknown>).title === 'string' ? [{ id: (item as Record<string, unknown>).session_id as string, title: (item as Record<string, unknown>).title as string, project: projectNames.get(assignments.get((item as Record<string, unknown>).session_id as string) ?? '') ?? 'Ungrouped' }] : []);
      setLoaded({ key, sessions });
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ key, error: errorMessage(cause) }); });
    return () => controller.abort();
  }, [agent, key, organization]);
  const current = loaded.key === key ? loaded : { key };
  if (current.error) return <section className="workspace-page" aria-label="Employee Workspaces"><p className="app-error" role="alert">{current.error}</p></section>;
  if (!current.sessions) return <section className="workspace-page" aria-label="Employee Workspaces"><p role="status">Loading workspaces…</p></section>;
  return <section className="workspace-page employee-workspaces" aria-label="Employee Workspaces"><h2>Workspaces</h2><p className="page-intro">Inspect the employee’s thread workspaces. Cleanup does not change their conversation history or Project grouping.</p><WorkspaceInspectionList organization={organization} agent={agent} csrf={csrf} sessions={current.sessions} onOpen={onOpen} onOperation={onOperation} showHeading={false} /></section>;
}
