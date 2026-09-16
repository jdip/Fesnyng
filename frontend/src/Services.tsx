import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorMessage, type Agent, type Project } from './workspace-api';

type ThreadAssociation = { agent_id: string; session_id: string };
type Thread = ThreadAssociation & { title: string; agent_name: string };
type Target = { kind: 'employee' | 'resource'; id: string; status: 'available' | 'missing' | 'unavailable'; running?: boolean };
type RouteStatus = { status: 'configured' | 'unavailable'; reason?: string; network_reachability: 'unverified' };
type Service = { id: string; name: string; target: Target; endpoint_url: string; route: 'custom' | 'tailscale'; threads: ThreadAssociation[]; project_ids: string[]; revision: number; route_status: RouteStatus };
type ResourceTarget = { id: string; name: string; status: 'available' | 'missing' | 'unavailable' };
type Loaded = { key: string; services?: Service[]; resources?: ResourceTarget[]; threads?: Thread[]; projects?: Project[]; warnings?: string[]; error?: string };
type Editing = { service?: Service; targetKey: string; name: string; endpoint: string; route: 'custom' | 'tailscale' };

const record = (value: unknown): Record<string, unknown> | undefined => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
const text = (value: unknown): string | undefined => typeof value === 'string' ? value : undefined;
const list = <T,>(value: unknown, parse: (item: unknown) => T | undefined): T[] => Array.isArray(value) ? value.flatMap((item) => { const parsed = parse(item); return parsed === undefined ? [] : [parsed]; }) : [];
const associationKey = (item: ThreadAssociation) => `${item.agent_id}:${item.session_id}`;
const associationFromKey = (key: string): ThreadAssociation | undefined => {
  const [agent_id, ...rest] = key.split(':');
  const session_id = rest.join(':');
  return agent_id && session_id ? { agent_id, session_id } : undefined;
};

function association(value: unknown): ThreadAssociation | undefined {
  const item = record(value);
  return item && typeof item.agent_id === 'string' && typeof item.session_id === 'string' ? { agent_id: item.agent_id, session_id: item.session_id } : undefined;
}
function target(value: unknown): Target | undefined {
  const item = record(value);
  return item && (item.kind === 'employee' || item.kind === 'resource') && typeof item.id === 'string' && (item.status === 'available' || item.status === 'missing' || item.status === 'unavailable') && (typeof item.running === 'boolean' || item.running === undefined) ? { kind: item.kind, id: item.id, status: item.status, ...(typeof item.running === 'boolean' ? { running: item.running } : {}) } : undefined;
}
function routeStatus(value: unknown): RouteStatus | undefined {
  const item = record(value);
  return item && (item.status === 'configured' || item.status === 'unavailable') && item.network_reachability === 'unverified' && (typeof item.reason === 'string' || item.reason === undefined) ? { status: item.status, network_reachability: 'unverified', ...(typeof item.reason === 'string' ? { reason: item.reason } : {}) } : undefined;
}
function service(value: unknown): Service | undefined {
  const item = record(value); const checkedTarget = item && target(item.target); const checkedRoute = item && routeStatus(item.route_status);
  return item && checkedTarget && checkedRoute && typeof item.id === 'string' && typeof item.name === 'string' && typeof item.endpoint_url === 'string' && (item.route === 'custom' || item.route === 'tailscale') && typeof item.revision === 'number' && Number.isSafeInteger(item.revision) ? { id: item.id, name: item.name, target: checkedTarget, endpoint_url: item.endpoint_url, route: item.route, threads: list(item.threads, association), project_ids: list(item.project_ids, text), revision: item.revision, route_status: checkedRoute } : undefined;
}
function resourceTarget(value: unknown): ResourceTarget | undefined {
  const item = record(value); const inspection = item && record(item.inspection);
  return item && inspection && typeof item.id === 'string' && typeof item.name === 'string' && (inspection.status === 'available' || inspection.status === 'missing' || inspection.status === 'unavailable') ? { id: item.id, name: item.name, status: inspection.status } : undefined;
}
function safeHttpUrl(value: string): string | undefined {
  try {
    if ([...value].some((character) => { const code = character.charCodeAt(0); return code < 32 || code === 127; })) return undefined;
    const url = new URL(value);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined;
    if (!url.hostname || url.username || url.password || url.hash) return undefined;
    if (['docker', 'docker.sock'].includes(url.hostname.toLowerCase())) return undefined;
    if (['2375', '2376', '4096'].includes(url.port)) return undefined;
    return url.href;
  } catch { return undefined; }
}
function targetFromKey(value: string): { kind: 'employee' | 'resource'; id: string } | undefined {
  const [kind, ...rest] = value.split(':'); const id = rest.join(':');
  return (kind === 'employee' || kind === 'resource') && id ? { kind, id } : undefined;
}

type ServicesProps = { organization: string; agent: Agent; agents: Agent[]; csrf: string };

export function Services(props: ServicesProps) {
  return <ScopedServices key={`${props.organization}:${props.agent.host_id}:${props.agent.id}`} {...props} />;
}

function ScopedServices({ organization, agent, agents, csrf }: ServicesProps) {
  const hostAgents = useMemo(() => agents.filter((candidate) => candidate.host_id === agent.host_id), [agent.host_id, agents]);
  const key = `${organization}:${agent.host_id}:${agent.id}:${hostAgents.map((candidate) => candidate.id).sort().join(',')}`;
  const base = `/organizations/${encodeURIComponent(organization)}/hosts/${encodeURIComponent(agent.host_id)}/docker/services`;
  const dockerBase = `/organizations/${encodeURIComponent(organization)}/hosts/${encodeURIComponent(agent.host_id)}/docker`;
  const [loaded, setLoaded] = useState<Loaded>({ key: '' });
  const [editing, setEditing] = useState<Editing>();
  const [threadIds, setThreadIds] = useState<string[]>([]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [confirmation, setConfirmation] = useState<Service>();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const loadSequence = useRef(0);
  const actionSequence = useRef(0);
  const activeKey = useRef(key);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { activeKey.current = key; }, [key]);

  useEffect(() => {
    const current = ++loadSequence.current;
    const controller = new AbortController();
    const sessionRequests = Promise.allSettled(hostAgents.map(async (employee) => {
      const inventory = await api<unknown>(`/organizations/${encodeURIComponent(organization)}/agents/${encodeURIComponent(employee.id)}/sessions`, { signal: controller.signal });
      if (!Array.isArray(inventory)) throw new Error(`Could not read ${employee.name}'s thread inventory.`);
      return inventory.flatMap((entry): Thread[] => { const row = record(entry); return row && typeof row.session_id === 'string' && typeof row.title === 'string' ? [{ agent_id: employee.id, session_id: row.session_id, title: row.title, agent_name: employee.name }] : []; });
    }));
    void Promise.allSettled([
      api<unknown>(base, { signal: controller.signal }),
      api<unknown>(dockerBase, { signal: controller.signal }),
      api<Project[]>(`/organizations/${encodeURIComponent(organization)}/projects?include_archived=true`, { signal: controller.signal }),
      sessionRequests,
    ]).then((results) => {
      if (controller.signal.aborted || current !== loadSequence.current) return;
      const servicesResult = results[0];
      if (!servicesResult || servicesResult.status === 'rejected') { setLoaded({ key, error: errorMessage(servicesResult?.status === 'rejected' ? servicesResult.reason : new Error('Could not read services.')) }); return; }
      const services = list(record(servicesResult.value)?.services, service);
      const warnings: string[] = [];
      const dockerResult = results[1];
      const resources = dockerResult?.status === 'fulfilled' ? list(record(dockerResult.value)?.resources, resourceTarget) : [];
      if (dockerResult?.status === 'rejected') warnings.push('Registered resource targets could not be refreshed. Existing service records remain readable.');
      const projectsResult = results[2];
      const projects = projectsResult?.status === 'fulfilled' && Array.isArray(projectsResult.value) ? projectsResult.value : [];
      if (projectsResult?.status === 'rejected') warnings.push('Project inventory could not be refreshed. Existing associations remain available to detach.');
      const sessionsResult = results[3];
      const sessionResults = sessionsResult?.status === 'fulfilled' ? sessionsResult.value : [];
      const threads = sessionResults.flatMap((result) => result.status === 'fulfilled' ? result.value : []);
      const unavailableEmployees = sessionResults.flatMap((result, index) => result.status === 'rejected' ? [hostAgents[index]?.name] : []).filter((name): name is string => Boolean(name));
      if (unavailableEmployees.length) warnings.push(`Some host-local thread inventories are unavailable: ${unavailableEmployees.join(', ')}. Existing associations remain available to detach.`);
      setLoaded({ key, services, resources, threads, projects, warnings });
    });
    return () => controller.abort();
  }, [base, dockerBase, hostAgents, key, organization, refresh]);

  const current = loaded.key === key ? loaded : { key };
  const threadByKey = new Map((current.threads ?? []).map((item) => [associationKey(item), item]));
  const projectById = new Map((current.projects ?? []).map((item) => [item.id, item]));
  const refreshServices = () => {
    actionSequence.current += 1;
    setBusy(false); setActionError(''); setEditing(undefined); setConfirmation(undefined); setThreadIds([]); setProjectIds([]); setRefresh((value) => value + 1);
  };
  const startRegister = () => { setEditing({ targetKey: `employee:${agent.id}`, name: '', endpoint: '', route: 'custom' }); setThreadIds([]); setProjectIds([]); setActionError(''); setConfirmation(undefined); };
  const startEdit = (item: Service) => { setEditing({ service: item, targetKey: `${item.target.kind}:${item.target.id}`, name: item.name, endpoint: item.endpoint_url, route: item.route }); setThreadIds(item.threads.map(associationKey)); setProjectIds(item.project_ids); setActionError(''); setConfirmation(undefined); };
  const chooseThread = (value: string, checked: boolean) => setThreadIds((items) => checked ? [...new Set([...items, value])] : items.filter((item) => item !== value));
  const chooseProject = (value: string, checked: boolean) => setProjectIds((items) => checked ? [...new Set([...items, value])] : items.filter((item) => item !== value));
  const associations = () => threadIds.flatMap((value) => { const parsed = associationFromKey(value); return parsed ? [parsed] : []; });
  const saveService = () => {
    if (!editing) return;
    const checkedTarget = targetFromKey(editing.targetKey);
    if (!editing.name.trim() || !checkedTarget || !safeHttpUrl(editing.endpoint)) { setActionError(!safeHttpUrl(editing.endpoint) ? 'Enter a valid HTTP(S) endpoint.' : 'Enter a display name and choose a service target.'); return; }
    setBusy(true); setActionError(''); const request = ++actionSequence.current;
    const body = { name: editing.name.trim(), target_kind: checkedTarget.kind, target_id: checkedTarget.id, endpoint_url: editing.endpoint.trim(), route: editing.route, threads: associations(), project_ids: projectIds, ...(editing.service ? { expected_revision: editing.service.revision } : {}) };
    const path = editing.service ? `${base}/${encodeURIComponent(editing.service.id)}` : base;
    const method = editing.service ? 'PUT' : 'POST';
    void api<Service>(path, { method, csrf, body }).then((saved) => {
      const normalized = service(saved);
      if (!mounted.current || request !== actionSequence.current || activeKey.current !== key) return;
      if (!normalized) throw new Error('The host returned an invalid service record.');
      setLoaded((state) => state.key === key && state.services ? { ...state, services: [...state.services.filter((item) => item.id !== normalized.id), normalized] } : state);
      setEditing(undefined); setThreadIds([]); setProjectIds([]);
    }).catch((cause: unknown) => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setActionError(errorMessage(cause)); }).finally(() => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setBusy(false); });
  };
  const unregister = () => {
    if (!confirmation) return;
    setBusy(true); setActionError(''); const request = ++actionSequence.current;
    void api<{ removed: true }>(`${base}/${encodeURIComponent(confirmation.id)}/unregister`, { method: 'POST', csrf, body: { expected_revision: confirmation.revision } }).then(() => {
      if (mounted.current && request === actionSequence.current && activeKey.current === key) setLoaded((state) => state.key === key && state.services ? { ...state, services: state.services.filter((item) => item.id !== confirmation.id) } : state);
    }).catch((cause: unknown) => { if (mounted.current && request === actionSequence.current && activeKey.current === key) setActionError(errorMessage(cause)); }).finally(() => { if (mounted.current && request === actionSequence.current && activeKey.current === key) { setBusy(false); setConfirmation(undefined); } });
  };

  if (current.error) return <section className="workspace-page services" aria-label="Employee services"><h2>Services</h2><p className="app-error" role="alert">{current.error}</p><button className="app-button" onClick={refreshServices}>Retry</button></section>;
  if (!current.services || !current.resources || !current.threads || !current.projects) return <section className="workspace-page services" aria-label="Employee services"><h2>Services</h2><p role="status">Loading services…</p></section>;
  return <section className="workspace-page services" aria-label="Employee services"><h2>Services</h2><p className="page-intro">Service records describe user-facing endpoints for employee and registered resource targets. They never publish a port or configure a network route.</p>
    <section className="app-panel services-notice"><p className="app-notice" role="status">Docker capability is not required to register or view services.</p><p className="muted">Project and thread associations organize work. Organization membership does not grant tailnet or network access.</p><div className="app-actions"><button className="app-button" onClick={refreshServices}>Refresh services</button><button className="app-button primary" disabled={busy} onClick={startRegister}>Register service</button></div></section>
    {current.warnings?.map((warning) => <p key={warning} className="app-notice" role="status">{warning}</p>)}
    {actionError && <p className="app-error" role="alert">{actionError}</p>}
    <section className="service-list" aria-label="Registered services">{current.services.map((item) => <ServiceCard key={item.id} service={item} threads={threadByKey} projects={projectById} editing={editing?.service?.id === item.id} onEdit={() => startEdit(item)} onUnregister={() => setConfirmation(item)} />)}{!current.services.length && <p className="muted">No services are registered on this host.</p>}</section>
    {editing && <ServiceEditor editing={editing} hostAgents={hostAgents} resources={current.resources} threads={current.threads} projects={current.projects} threadIds={threadIds} projectIds={projectIds} onEditing={setEditing} onThread={chooseThread} onProject={chooseProject} onSave={saveService} onCancel={() => { setEditing(undefined); setThreadIds([]); setProjectIds([]); }} busy={busy} />}
    {confirmation && <section className="app-panel service-confirmation" aria-label={`unregister ${confirmation.name}`}><p>Unregister {confirmation.name}? This removes its Fesnyng service record only. It does not stop, remove, expose, or reconfigure the target.</p><div className="app-actions"><button className="app-button danger" disabled={busy} onClick={unregister}>Confirm unregister service</button><button className="app-button" disabled={busy} onClick={() => setConfirmation(undefined)}>Cancel</button></div></section>}
  </section>;
}

function ServiceEditor({ editing, hostAgents, resources, threads, projects, threadIds, projectIds, onEditing, onThread, onProject, onSave, onCancel, busy }: { editing: Editing; hostAgents: Agent[]; resources: ResourceTarget[]; threads: Thread[]; projects: Project[]; threadIds: string[]; projectIds: string[]; onEditing: (value: Editing) => void; onThread: (key: string, checked: boolean) => void; onProject: (id: string, checked: boolean) => void; onSave: () => void; onCancel: () => void; busy: boolean }) {
  const targetExists = targetFromKey(editing.targetKey) && ([...hostAgents.map((item) => `employee:${item.id}`), ...resources.map((item) => `resource:${item.id}`)].includes(editing.targetKey));
  return <section className="app-panel service-editor" aria-label={editing.service ? `Edit ${editing.service.name}` : 'Register service'}><h3>{editing.service ? `Edit ${editing.service.name}` : 'Register service'}</h3><label className="app-label">Display name<input className="app-input" value={editing.name} onChange={(event) => onEditing({ ...editing, name: event.target.value })} required /></label><label className="app-label">Service target<select className="app-select" value={editing.targetKey} onChange={(event) => onEditing({ ...editing, targetKey: event.target.value })}><option value="">Choose a target</option>{!targetExists && editing.service && <option value={editing.targetKey}>Unavailable target · {editing.service.target.kind} / {editing.service.target.id}</option>}<optgroup label="Employees">{hostAgents.map((item) => <option key={item.id} value={`employee:${item.id}`}>{item.name}</option>)}</optgroup><optgroup label="Registered resources">{resources.map((item) => <option key={item.id} value={`resource:${item.id}`}>{item.name}{item.status === 'available' ? '' : ` (${item.status})`}</option>)}</optgroup></select></label><label className="app-label">HTTP(S) endpoint<input className="app-input" type="url" value={editing.endpoint} onChange={(event) => onEditing({ ...editing, endpoint: event.target.value })} placeholder="https://service.example.test" required /></label><label className="app-label">Route<select className="app-select" value={editing.route} onChange={(event) => onEditing({ ...editing, route: event.target.value as 'custom' | 'tailscale' })}><option value="custom">Custom route</option><option value="tailscale">Tailscale Serve</option></select></label><p className="muted">Registering this record does not create a route or grant network access.</p><AssociationEditor threads={threads} projects={projects} threadIds={threadIds} projectIds={projectIds} onThread={onThread} onProject={onProject} /><div className="app-actions"><button className="app-button primary" disabled={busy} onClick={onSave}>Save service</button><button className="app-button" disabled={busy} onClick={onCancel}>Cancel</button></div></section>;
}

function AssociationEditor({ threads, projects, threadIds, projectIds, onThread, onProject }: { threads: Thread[]; projects: Project[]; threadIds: string[]; projectIds: string[]; onThread: (value: string, checked: boolean) => void; onProject: (value: string, checked: boolean) => void }) {
  const threadByKey = new Map(threads.map((item) => [associationKey(item), item]));
  const projectById = new Map(projects.map((item) => [item.id, item]));
  const retainedThreads = threadIds.flatMap((id) => threadByKey.has(id) ? [] : [associationFromKey(id)]).filter((item): item is ThreadAssociation => Boolean(item));
  const activeProjects = projects.filter((item) => !item.archived);
  const retainedProjects = projectIds.filter((id) => !activeProjects.some((item) => item.id === id));
  return <div className="service-association-editor"><fieldset><legend>Thread associations</legend>{threads.map((item) => { const key = associationKey(item); return <label key={key} className="checkbox-label"><input type="checkbox" checked={threadIds.includes(key)} onChange={(event) => onThread(key, event.target.checked)} />{item.title} · {item.agent_name}</label>; })}{retainedThreads.map((item) => { const key = associationKey(item); return <label key={key} className="checkbox-label service-retained-association"><input type="checkbox" checked={threadIds.includes(key)} onChange={(event) => onThread(key, event.target.checked)} />Unavailable or deleted thread · {item.agent_id} / {item.session_id}</label>; })}{!threads.length && !retainedThreads.length && <p className="muted">No host-local employee threads are currently available.</p>}</fieldset><fieldset><legend>Project associations</legend>{activeProjects.map((item) => <label key={item.id} className="checkbox-label"><input type="checkbox" checked={projectIds.includes(item.id)} onChange={(event) => onProject(item.id, event.target.checked)} />{item.name}</label>)}{retainedProjects.map((id) => { const project = projectById.get(id); const label = project?.archived ? `${project.name} (archived Project)` : `Unavailable or deleted Project · ${id}`; return <label key={id} className="checkbox-label service-retained-association"><input type="checkbox" checked={projectIds.includes(id)} onChange={(event) => onProject(id, event.target.checked)} />{label}</label>; })}{!activeProjects.length && !retainedProjects.length && <p className="muted">No active Projects are available.</p>}</fieldset></div>;
}

function ServiceCard({ service, threads, projects, editing, onEdit, onUnregister }: { service: Service; threads: Map<string, Thread>; projects: Map<string, Project>; editing: boolean; onEdit: () => void; onUnregister: () => void }) {
  const endpoint = safeHttpUrl(service.endpoint_url);
  const targetState = service.target.status === 'unavailable' ? 'Target unavailable' : service.target.status === 'missing' ? 'Target missing' : service.target.running === true ? 'Target running' : service.target.running === false ? 'Target stopped' : 'Target state unknown';
  const route = service.route === 'tailscale'
    ? service.route_status.status === 'configured' ? 'Serve route configured' : 'Serve route unavailable'
    : service.route_status.status === 'configured' ? 'Endpoint registered' : 'Endpoint route unavailable';
  const routeDetail = `${route} · ${service.route_status.reason ? `${service.route_status.reason} ` : ''}Client network reachability is unverified.`;
  const linkedThreads = service.threads.map((item) => threads.get(associationKey(item)) ? `${threads.get(associationKey(item))!.title} · ${threads.get(associationKey(item))!.agent_name}` : `Unavailable or deleted thread · ${item.agent_id} / ${item.session_id}`);
  const linkedProjects = service.project_ids.map((id) => { const item = projects.get(id); return item ? item.archived ? `${item.name} (archived Project)` : item.name : `Unavailable or deleted Project · ${id}`; });
  return <article className="app-panel service"><div className="service-heading"><div><h3>{service.name}</h3><p className="muted">{service.target.kind === 'employee' ? 'Employee' : 'Registered resource'} target · {service.target.id}</p></div><span className="app-badge">{targetState}</span></div><dl className="service-fields"><div><dt>Endpoint</dt><dd>{endpoint ? <a href={endpoint} target="_blank" rel="noreferrer noopener" aria-label={`Open ${service.name}`}>{service.endpoint_url}</a> : <span className="app-error">Endpoint is not a safe HTTP(S) URL.</span>}</dd></div><div><dt>Route</dt><dd>{service.route === 'tailscale' ? 'Tailscale Serve' : 'Custom route'}</dd></div><div><dt>Route status</dt><dd>{routeDetail}</dd></div><div><dt>Thread sharing</dt><dd>{linkedThreads.length ? linkedThreads.join(', ') : 'No linked threads'}</dd></div><div><dt>Project sharing</dt><dd>{linkedProjects.length ? linkedProjects.join(', ') : 'No linked Projects'}</dd></div></dl><div className="app-actions"><button className="app-button" disabled={editing} onClick={onEdit}>Edit service</button><button className="app-button danger" disabled={editing} onClick={onUnregister}>Unregister service</button></div></article>;
}
