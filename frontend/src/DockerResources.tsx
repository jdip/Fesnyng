import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorMessage, type Agent, type Project } from './workspace-api';

type Capability = { enabled: boolean; available: boolean; reason?: string | null; engine_id?: string | null };
type ThreadAssociation = { agent_id: string; session_id: string };
type Mount = { type: string; source: string; destination: string; read_only: boolean };
type Port = { container_port: string; host_ip: string; host_port: string };
type Inspection = { status: 'available' | 'missing' | 'unavailable'; reason?: string; state?: string; running?: boolean; mounts?: Mount[]; ports?: Port[]; networks?: string[]; compose_project?: string | null; compose_service?: string | null };
type Resource = { id: string; container_id: string; name: string; revision: number; threads: ThreadAssociation[]; project_ids: string[]; engine_id: string; inspection: Inspection };
type Discovery = { container_id: string; name: string; state: string; compose_project: string | null; compose_service: string | null };
type Thread = ThreadAssociation & { title: string; agent_name: string };
type Loaded = { key: string; capability?: Capability; hostCapability?: Capability; resources?: Resource[]; threads?: Thread[]; projects?: Project[]; unavailableEmployees?: string[]; error?: string };
type Confirmation = { resource: Resource; action: 'start' | 'stop' | 'remove' | 'unregister' };

const containerId = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const record = (value: unknown): Record<string, unknown> | undefined => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
const text = (value: unknown): string | undefined => typeof value === 'string' ? value : undefined;
const list = <T,>(value: unknown, parse: (item: unknown) => T | undefined): T[] => Array.isArray(value) ? value.flatMap((item) => { const parsed = parse(item); return parsed === undefined ? [] : [parsed]; }) : [];

function capability(value: unknown): Capability {
  const item = record(value);
  if (!item || typeof item.enabled !== 'boolean' || typeof item.available !== 'boolean') throw new Error('Docker capability is unavailable.');
  return { enabled: item.enabled, available: item.available, ...(typeof item.reason === 'string' || item.reason === null ? { reason: item.reason } : {}), ...(typeof item.engine_id === 'string' || item.engine_id === null ? { engine_id: item.engine_id } : {}) };
}
function inspection(value: unknown): Inspection | undefined {
  const item = record(value);
  if (!item || !['available', 'missing', 'unavailable'].includes(item.status as string)) return undefined;
  const mount = (entry: unknown): Mount | undefined => { const row = record(entry); return row && typeof row.type === 'string' && typeof row.source === 'string' && typeof row.destination === 'string' && typeof row.read_only === 'boolean' ? { type: row.type, source: row.source, destination: row.destination, read_only: row.read_only } : undefined; };
  const port = (entry: unknown): Port | undefined => { const row = record(entry); return row && typeof row.container_port === 'string' && typeof row.host_ip === 'string' && typeof row.host_port === 'string' ? { container_port: row.container_port, host_ip: row.host_ip, host_port: row.host_port } : undefined; };
  return { status: item.status as Inspection['status'], ...(typeof item.reason === 'string' ? { reason: item.reason } : {}), ...(typeof item.state === 'string' ? { state: item.state } : {}), ...(typeof item.running === 'boolean' ? { running: item.running } : {}), mounts: list(item.mounts, mount), ports: list(item.ports, port), networks: list(item.networks, text), ...(typeof item.compose_project === 'string' || item.compose_project === null ? { compose_project: item.compose_project } : {}), ...(typeof item.compose_service === 'string' || item.compose_service === null ? { compose_service: item.compose_service } : {}) };
}
function resource(value: unknown): Resource | undefined {
  const item = record(value); const checked = item && inspection(item.inspection);
  if (!item || !checked || typeof item.id !== 'string' || !containerId(item.container_id) || typeof item.name !== 'string' || typeof item.revision !== 'number' || !Number.isSafeInteger(item.revision) || typeof item.engine_id !== 'string') return undefined;
  const association = (entry: unknown): ThreadAssociation | undefined => { const row = record(entry); return row && typeof row.agent_id === 'string' && typeof row.session_id === 'string' ? { agent_id: row.agent_id, session_id: row.session_id } : undefined; };
  return { id: item.id, container_id: item.container_id, name: item.name, revision: item.revision, threads: list(item.threads, association), project_ids: list(item.project_ids, text), engine_id: item.engine_id, inspection: checked };
}
function discovery(value: unknown): Discovery | undefined {
  const item = record(value);
  return item && containerId(item.container_id) && typeof item.name === 'string' && typeof item.state === 'string' && (typeof item.compose_project === 'string' || item.compose_project === null) && (typeof item.compose_service === 'string' || item.compose_service === null) ? { container_id: item.container_id, name: item.name, state: item.state, compose_project: item.compose_project, compose_service: item.compose_service } : undefined;
}
const associationKey = (association: ThreadAssociation) => `${association.agent_id}:${association.session_id}`;
const associationFromKey = (key: string): ThreadAssociation | undefined => {
  const [agent_id, ...rest] = key.split(':');
  const session_id = rest.join(':');
  return agent_id && session_id ? { agent_id, session_id } : undefined;
};

type ResourcesProps = { organization: string; agent: Agent; agents: Agent[]; csrf: string };

export function DockerResources(props: ResourcesProps) {
  return <ScopedDockerResources key={`${props.organization}:${props.agent.host_id}:${props.agent.id}`} {...props} />;
}

function ScopedDockerResources({ organization, agent, agents, csrf }: ResourcesProps) {
  const hostAgents = useMemo(() => agents.filter((candidate) => candidate.host_id === agent.host_id), [agent.host_id, agents]);
  const key = `${organization}:${agent.host_id}:${agent.id}:${hostAgents.map((candidate) => candidate.id).sort().join(',')}`;
  const base = `/organizations/${encodeURIComponent(organization)}/hosts/${encodeURIComponent(agent.host_id)}/docker`;
  const [loaded, setLoaded] = useState<Loaded>({ key: '' });
  const [discovered, setDiscovered] = useState<Discovery[]>();
  const [discoveryError, setDiscoveryError] = useState('');
  const [selection, setSelection] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [threadIds, setThreadIds] = useState<string[]>([]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [editing, setEditing] = useState<Resource>();
  const [confirmation, setConfirmation] = useState<Confirmation>();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const loadSequence = useRef(0);
  const actionSequence = useRef(0);
  const discoverySequence = useRef(0);
  const activeKey = useRef(key);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { activeKey.current = key; }, [key]);

  useEffect(() => {
    const current = ++loadSequence.current;
    const controller = new AbortController();
    const sessions = Promise.allSettled(hostAgents.map(async (employee) => {
      const inventory = await api<unknown>(`/organizations/${encodeURIComponent(organization)}/agents/${encodeURIComponent(employee.id)}/sessions`, { signal: controller.signal });
      if (!Array.isArray(inventory)) throw new Error(`Could not read ${employee.name}'s thread inventory.`);
      return inventory.flatMap((item): Thread[] => { const row = record(item); return row && typeof row.session_id === 'string' && typeof row.title === 'string' ? [{ agent_id: employee.id, session_id: row.session_id, title: row.title, agent_name: employee.name }] : []; });
    }));
    void Promise.all([
      api<unknown>(base, { signal: controller.signal }),
      api<unknown>(`${base}/capability?agent_id=${encodeURIComponent(agent.id)}`, { signal: controller.signal }),
      sessions,
      api<Project[]>(`/organizations/${encodeURIComponent(organization)}/projects?include_archived=true`, { signal: controller.signal }),
    ]).then(([inventory, selectedCapability, sessionResults, projects]) => {
      if (controller.signal.aborted || current !== loadSequence.current) return;
      const inventoryRow = record(inventory); const resources = list(inventoryRow?.resources, resource);
      const threads = sessionResults.flatMap((result) => result.status === 'fulfilled' ? result.value : []);
      const unavailableEmployees = sessionResults.flatMap((result, index) => result.status === 'rejected' ? [hostAgents[index]!.name] : []);
      setLoaded({ key, capability: capability(selectedCapability), hostCapability: capability(inventoryRow?.capability), resources, threads, projects, unavailableEmployees });
    }).catch((cause: unknown) => { if (!controller.signal.aborted && current === loadSequence.current) setLoaded({ key, error: errorMessage(cause) }); });
    return () => controller.abort();
  }, [key, refresh, agent.id, base, hostAgents, organization]);

  const current = loaded.key === key ? loaded : { key };
  const available = current.capability?.enabled === true && current.capability.available === true;
  const hostAvailable = current.hostCapability?.enabled === true && current.hostCapability.available === true;
  const threadByKey = new Map((current.threads ?? []).map((item) => [associationKey(item), item]));
  const projectById = new Map((current.projects ?? []).map((item) => [item.id, item]));
  const chooseThread = (key: string, checked: boolean) => setThreadIds((items) => checked ? [...new Set([...items, key])] : items.filter((item) => item !== key));
  const chooseProject = (id: string, checked: boolean) => setProjectIds((items) => checked ? [...new Set([...items, id])] : items.filter((item) => item !== id));
  const associations = () => threadIds.flatMap((key) => { const association = associationFromKey(key); return association ? [association] : []; });
  const refreshResources = () => {
    actionSequence.current += 1;
    discoverySequence.current += 1;
    setBusy(false); setConfirmation(undefined); setEditing(undefined); setDiscovered(undefined);
    setSelection(''); setDisplayName(''); setThreadIds([]); setProjectIds([]);
    setActionError(''); setDiscoveryError(''); setRefresh((value) => value + 1);
  };

  const discoverContainers = async () => {
    setDiscoveryError(''); setActionError('');
    const request = ++discoverySequence.current;
    try {
      const value = await api<unknown>(`${base}/discovery`);
      if (!mounted.current || request !== discoverySequence.current || activeKey.current !== key) return;
      const containers = list(record(value)?.containers, discovery);
      setDiscovered(containers); setSelection(''); setDisplayName(''); setThreadIds([]); setProjectIds([]);
    } catch (cause) { if (mounted.current && request === discoverySequence.current && activeKey.current === key) setDiscoveryError(errorMessage(cause)); }
  };
  const selectedDiscovery = discovered?.find((item) => item.container_id === selection);
  const beginRegistration = () => {
    if (!selectedDiscovery || !containerId(selectedDiscovery.container_id) || !displayName.trim()) { setActionError('Choose a discovered container and enter a display name.'); return; }
    setBusy(true); setActionError(''); const request = ++actionSequence.current;
    void api<Resource>(`${base}/resources`, { method: 'POST', csrf, body: { container_id: selectedDiscovery.container_id, name: displayName.trim(), threads: associations(), project_ids: projectIds } }).then((saved) => {
      const normalized = resource(saved); if (!mounted.current || request !== actionSequence.current || activeKey.current !== key) return; if (!normalized) throw new Error('The host returned an invalid resource record.');
      setLoaded((state) => state.key === key && state.resources ? { ...state, resources: [...state.resources.filter((item) => item.id !== normalized.id), normalized] } : state);
      setDiscovered(undefined); setSelection(''); setDisplayName(''); setThreadIds([]); setProjectIds([]);
    }).catch((cause: unknown) => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setActionError(errorMessage(cause)); }).finally(() => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setBusy(false); });
  };
  const beginEdit = (item: Resource) => { setEditing(item); setThreadIds(item.threads.map(associationKey)); setProjectIds(item.project_ids); setActionError(''); };
  const saveAssociations = () => {
    if (!editing) return;
    setBusy(true); setActionError(''); const request = ++actionSequence.current;
    void api<Resource>(`${base}/resources/${encodeURIComponent(editing.id)}`, { method: 'PUT', csrf, body: { name: editing.name, threads: associations(), project_ids: projectIds, expected_revision: editing.revision } }).then((saved) => {
      const normalized = resource(saved); if (!mounted.current || request !== actionSequence.current || activeKey.current !== key) return; if (!normalized) throw new Error('The host returned an invalid resource record.');
      setLoaded((state) => state.key === key && state.resources ? { ...state, resources: state.resources.map((item) => item.id === normalized.id ? normalized : item) } : state); setEditing(undefined);
    }).catch((cause: unknown) => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setActionError(errorMessage(cause)); }).finally(() => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setBusy(false); });
  };
  const operate = () => {
    if (!confirmation) return;
    const { resource: item, action } = confirmation;
    setBusy(true); setActionError(''); const request = ++actionSequence.current;
    void api<Resource | { removed: true }>(`${base}/resources/${encodeURIComponent(item.id)}/${action}`, { method: 'POST', csrf, body: { expected_revision: item.revision } }).then((result) => {
      if (!mounted.current || request !== actionSequence.current || activeKey.current !== key) return;
      if (action === 'unregister') { setLoaded((state) => state.key === key && state.resources ? { ...state, resources: state.resources.filter((entry) => entry.id !== item.id) } : state); return; }
      const normalized = resource(result); if (!normalized) throw new Error('The host returned an invalid resource record.');
      setLoaded((state) => state.key === key && state.resources ? { ...state, resources: state.resources.map((entry) => entry.id === normalized.id ? normalized : entry) } : state);
    }).catch((cause: unknown) => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setActionError(errorMessage(cause)); }).finally(() => { if (mounted.current && request === actionSequence.current && activeKey.current === key) { setBusy(false); setConfirmation(undefined); } });
  };

  if (current.error) return <section className="workspace-page docker-resources" aria-label="Employee Docker resources"><h2>Resources</h2><p className="app-error" role="alert">{current.error}</p><button className="app-button" onClick={refreshResources}>Retry</button></section>;
  if (!current.capability || !current.hostCapability || !current.resources || !current.threads || !current.projects) return <section className="workspace-page docker-resources" aria-label="Employee Docker resources"><h2>Resources</h2><p role="status">Loading Docker resources…</p></section>;
  const reason = current.capability.reason ?? (!available ? 'Docker resources are not available for this employee.' : undefined);
  return <section className="workspace-page docker-resources" aria-label="Employee Docker resources"><h2>Resources</h2><p className="page-intro">Registered containers are shared resources on this organization-dedicated host. Thread and Project links organize access; they do not manage Compose stack lifetimes.</p>
    <section className="app-panel docker-capability" aria-label="Docker capability"><h3>Docker capability</h3><p className={available || !current.capability.enabled ? 'app-notice' : 'app-error'} role="status">{available ? 'Enabled for this employee on the organization-dedicated host.' : reason}</p><div className="app-actions"><button className="app-button" onClick={refreshResources}>Refresh resources</button><button className="app-button" disabled={!hostAvailable || busy} onClick={() => { void discoverContainers(); }}>Discover containers</button></div>{!hostAvailable && current.hostCapability.reason && current.hostCapability.reason !== reason && <p className="muted">{current.hostCapability.reason}</p>}{discoveryError && <p className="app-error" role="alert">{discoveryError}</p>}</section>
    {current.unavailableEmployees?.length ? <p className="app-notice" role="status">Some host-local thread inventories are unavailable: {current.unavailableEmployees.join(', ')}. Existing associations remain available to edit or detach.</p> : null}
    {hostAvailable && discovered && <section className="app-panel docker-registration" aria-label="Register container"><h3>Register discovered container</h3>{!discovered.length ? <p className="muted">No eligible containers were discovered on this host.</p> : <><label className="app-label">Discovered container<select className="app-select" value={selection} onChange={(event) => { const next = event.target.value; setSelection(next); setDisplayName(discovered.find((item) => item.container_id === next)?.name ?? ''); }}><option value="">Choose a container</option>{discovered.map((item) => <option key={item.container_id} value={item.container_id}>{item.name} · {item.state} · {item.container_id}</option>)}</select></label><label className="app-label">Display name<input className="app-input" value={displayName} onChange={(event) => setDisplayName(event.target.value)} required /></label><AssociationEditor threads={current.threads} projects={current.projects} threadIds={threadIds} projectIds={projectIds} onThread={chooseThread} onProject={chooseProject} /><div className="app-actions"><button className="app-button primary" disabled={busy || !selection || !displayName.trim()} onClick={beginRegistration}>Register container</button><button className="app-button" disabled={busy} onClick={() => setDiscovered(undefined)}>Cancel</button></div></>}</section>}
    {actionError && <p className="app-error" role="alert">{actionError}</p>}
    <section className="docker-resource-list" aria-label="Registered Docker resources">{current.resources.map((item) => <ResourceCard key={item.id} resource={item} threads={threadByKey} projects={projectById} editing={editing?.id === item.id} onEdit={() => beginEdit(item)} onAction={(action) => setConfirmation({ resource: item, action })} />)}{!current.resources.length && <p className="muted">No Docker resources are registered on this host.</p>}</section>
    {editing && <section className="app-panel docker-associations" aria-label={`Associations for ${editing.name}`}><h3>Associations for {editing.name}</h3><p className="muted">Sharing is visible to organization members. It does not grant Docker daemon access.</p><AssociationEditor threads={current.threads} projects={current.projects} threadIds={threadIds} projectIds={projectIds} onThread={chooseThread} onProject={chooseProject} /><div className="app-actions"><button className="app-button primary" disabled={busy} onClick={saveAssociations}>Save associations</button><button className="app-button" disabled={busy} onClick={() => setEditing(undefined)}>Cancel</button></div></section>}
    {confirmation && <section className="app-panel docker-confirmation" aria-label={`${confirmation.action} ${confirmation.resource.name}`}><p>{confirmationText(confirmation)}</p><div className="app-actions"><button className={confirmation.action === 'remove' ? 'app-button danger' : 'app-button primary'} disabled={busy} onClick={operate}>Confirm {confirmation.action} container</button><button className="app-button" disabled={busy} onClick={() => setConfirmation(undefined)}>Cancel</button></div></section>}
  </section>;
}

function AssociationEditor({ threads, projects, threadIds, projectIds, onThread, onProject }: { threads: Thread[]; projects: Project[]; threadIds: string[]; projectIds: string[]; onThread: (key: string, checked: boolean) => void; onProject: (id: string, checked: boolean) => void }) {
  const knownThreads = new Set(threads.map(associationKey));
  const retainedThreads = threadIds.filter((key) => !knownThreads.has(key)).flatMap((key) => { const association = associationFromKey(key); return association ? [association] : []; });
  const activeProjects = projects.filter((project) => !project.archived);
  const activeProjectIds = new Set(activeProjects.map((project) => project.id));
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const retainedProjectIds = projectIds.filter((id) => !activeProjectIds.has(id));
  return <div className="docker-association-editor"><fieldset><legend>Thread associations</legend>{threads.map((thread) => { const key = associationKey(thread); return <label key={key} className="checkbox-label"><input type="checkbox" checked={threadIds.includes(key)} onChange={(event) => onThread(key, event.target.checked)} />{thread.title} · {thread.agent_name}</label>; })}{retainedThreads.map((thread) => { const key = associationKey(thread); return <label key={key} className="checkbox-label docker-retained-association"><input type="checkbox" checked={threadIds.includes(key)} onChange={(event) => onThread(key, event.target.checked)} />Unavailable or deleted thread · {thread.agent_id} / {thread.session_id}</label>; })}{!threads.length && !retainedThreads.length && <p className="muted">No host-local employee threads are currently available in the loaded inventories.</p>}</fieldset><fieldset><legend>Project associations</legend>{activeProjects.map((project) => <label key={project.id} className="checkbox-label"><input type="checkbox" checked={projectIds.includes(project.id)} onChange={(event) => onProject(project.id, event.target.checked)} />{project.name}</label>)}{retainedProjectIds.map((id) => { const project = projectById.get(id); const label = project?.archived ? `${project.name} (archived Project)` : `Deleted Project · ${id}`; return <label key={id} className="checkbox-label docker-retained-association"><input type="checkbox" checked={projectIds.includes(id)} onChange={(event) => onProject(id, event.target.checked)} />{label}</label>; })}{!activeProjects.length && !retainedProjectIds.length && <p className="muted">No active Projects are available.</p>}</fieldset></div>;
}

function ResourceCard({ resource, threads, projects, editing, onEdit, onAction }: { resource: Resource; threads: Map<string, Thread>; projects: Map<string, Project>; editing: boolean; onEdit: () => void; onAction: (action: Confirmation['action']) => void }) {
  const missing = resource.inspection.status === 'missing'; const verified = resource.inspection.status === 'available' && typeof resource.inspection.running === 'boolean'; const running = resource.inspection.running === true;
  const linkedThreads = resource.threads.map((entry) => { const thread = threads.get(associationKey(entry)); return thread ? `${thread.title} · ${thread.agent_name}` : `Unavailable or deleted thread · ${entry.agent_id} / ${entry.session_id}`; });
  const linkedProjects = resource.project_ids.map((id) => { const project = projects.get(id); return project ? (project.archived ? `${project.name} (archived Project)` : project.name) : `Deleted Project · ${id}`; });
  return <article className="app-panel docker-resource"><div className="docker-resource-heading"><div><h3>{resource.name}</h3><p className="muted">{resource.container_id} · {resource.inspection.state ?? resource.inspection.status}</p></div><span className="app-badge">{missing ? 'Missing' : !verified ? 'Unavailable' : running ? 'Running' : 'Stopped'}</span></div>{resource.inspection.reason && <p className="app-notice" role="status">{resource.inspection.reason}</p>}
    <dl className="docker-resource-fields"><div><dt>Compose</dt><dd>{resource.inspection.compose_project ?? 'None'}{resource.inspection.compose_service ? ` · ${resource.inspection.compose_service}` : ''}</dd></div><div><dt>Networks</dt><dd>{resource.inspection.networks?.join(', ') || 'None reported'}</dd></div><div><dt>Ports</dt><dd>{resource.inspection.ports?.map((port) => `${port.host_ip}:${port.host_port} → ${port.container_port}`).join(', ') || 'None reported'}</dd></div><div className="docker-resource-mounts"><dt>Mounts</dt><dd>{resource.inspection.mounts?.map((mount) => `${mount.type}: ${mount.source} → ${mount.destination}${mount.read_only ? ' (read-only)' : ''}`).join('; ') || 'None reported'}</dd></div><div><dt>Thread sharing</dt><dd>{linkedThreads.length ? linkedThreads.join(', ') : 'No linked threads'}</dd></div><div><dt>Project sharing</dt><dd>{linkedProjects.length ? linkedProjects.join(', ') : 'No linked Projects'}</dd></div></dl>
    <div className="app-actions"><button className="app-button" disabled={editing} onClick={onEdit}>Edit associations</button><button className="app-button" disabled={!verified || running} onClick={() => onAction('start')}>Start container</button><button className="app-button" disabled={!verified || !running} onClick={() => onAction('stop')}>Stop container</button><span className="docker-action"><button className="app-button danger" disabled={!verified || running} onClick={() => onAction('remove')}>Remove container</button>{running && <small>Stop the container before removing it.</small>}</span><button className="app-button" disabled={editing} onClick={() => onAction('unregister')}>Unregister container</button></div></article>;
}

function confirmationText({ resource, action }: Confirmation) {
  if (action === 'remove') return `Remove this stopped container? Files in its writable layer will be lost. Volumes and bind-mounted host files are retained.`;
  if (action === 'unregister') return `Unregister ${resource.name}? The container keeps running state and data.`;
  if (action === 'start') return `Start ${resource.name}? This does not manage any Compose stack.`;
  return `Stop ${resource.name}? Linked threads and Projects remain visible.`;
}
