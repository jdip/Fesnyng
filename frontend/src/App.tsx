import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { BrainIcon, LogOutIcon, PlusIcon, RefreshCwIcon, SettingsIcon, SlidersHorizontalIcon, XIcon } from 'lucide-react';
import { readWorkspaceLocation, saveWorkspaceLocation } from './workspace-location';
import { ConversationBoundary } from './ConversationBoundary';
import { ThreadNotificationsProvider, useThreadNotifications } from './ThreadNotifications';
import { ThreadNotificationBadge } from './ThreadNotificationBadge';
import { ConversationDraftsProvider } from './ConversationDrafts';
import { OrganizationSwitcher } from './OrganizationSwitcher';
import { OrganizationThreadSearch } from './OrganizationThreadSearch';
import { ThreadPagePreferenceForm, useThreadPagePreference } from './ThreadPagePreference';
import { Dialog, DialogClose, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from './components/ui/dialog';
import { ConversationDeliveryProvider } from './ConversationDelivery';
const Conversation = lazy(async () => ({ default: (await import('./Conversation')).Conversation }));
const CodexConversation = lazy(async () => ({ default: (await import('./CodexConversation')).CodexConversation }));
import { AgentSettings } from './AgentSettings';
import { OrganizationSettings } from './OrganizationSettings';
import { AgentMemory } from './AgentMemory';
import { DepartmentChart } from './DepartmentChart';
import { EmployeeNewThreadChooser, ProjectNavigation } from './ProjectNavigation';
import { agentPath, api, ApiError, errorMessage, type LoginSession, type Organization, type Agent, type Member, type Project } from './workspace-api';

function Brand() { return <div className="brand"><span className="brand-mark" aria-hidden="true">f</span>Fesnyng</div>; }
export function App() {
  const [session, setSession] = useState<LoginSession | null | undefined>();
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void api<LoginSession>('/auth/session', { signal: controller.signal }).then(setSession).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setSession(null);
      if (!(cause instanceof ApiError && cause.status === 401)) setError(errorMessage(cause));
    });
    return () => controller.abort();
  }, []);
  if (session === undefined) return <div className="sign-in"><p role="status">Connecting to your workspace…</p></div>;
  if (!session) return <SignIn onLogin={setSession} initialError={error} />;
  return <Workspace key={session.user.id} session={session} onLogout={() => setSession(null)} />;
}
function SignIn({ onLogin, initialError }: { onLogin: (session: LoginSession) => void; initialError: string }) {
  const [error, setError] = useState(initialError);
  const [busy, setBusy] = useState(false);
  return <main className="sign-in"><div className="sign-in-card"><Brand /><h1>Your agent organization.</h1><p className="page-intro">Sign in to work with your team.</p>
    <form className="app-form" onSubmit={(event) => {
      event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setError('');
      void api<LoginSession>('/auth/login', { method: 'POST', body: { login: data.get('login'), password: data.get('password') } }).then(onLogin).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
    }}><label>Login<input className="app-input" name="login" autoComplete="username" required /></label><label>Password<input className="app-input" name="password" type="password" autoComplete="current-password" required /></label>
      {error && <p className="app-error" role="alert">{error}</p>}<button className="app-button primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
    </form></div></main>;
}

function Workspace({ session, onLogout }: { session: LoginSession; onLogout: () => void }) {
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState('');
  const [organizationView, setOrganizationView] = useState<'chart' | 'organization'>();
  const [organizationRoles, setOrganizationRoles] = useState<Record<string, 'manager' | 'member' | 'unavailable'>>({});
  const [roleLookupVersion, setRoleLookupVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [focusIdentity, setFocusIdentity] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void api<Organization[]>('/organizations', { signal: controller.signal }).then((items) => {
      if (controller.signal.aborted) return;
      setLoading(false); setOrganizations(items); const saved = readWorkspaceLocation().organization; setOrganization(items.find((item) => item.id === saved)?.id ?? items[0]?.id ?? '');
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all(organizations.map(async (item) => {
      try {
        const members = await api<Member[]>(`/organizations/${item.id}/members`, { signal: controller.signal });
        return [item.id, ['owner', 'admin'].includes(members.find((member) => member.user_id === session.user.id)?.role ?? '') ? 'manager' : 'member'] as const;
      } catch (cause) {
        return [item.id, cause instanceof ApiError && cause.status === 403 ? 'member' : 'unavailable'] as const;
      }
    })).then((items) => {
      if (!controller.signal.aborted) setOrganizationRoles(Object.fromEntries(items));
    });
    return () => controller.abort();
  }, [organizations, roleLookupVersion, session.user.id]);
  if (error) return <main className="workspace-page"><p className="app-error" role="alert">{error}</p><button className="app-button" onClick={onLogout}>Sign in again</button></main>;
  if (loading) return <div className="app-empty" role="status">Loading organizations…</div>;
  if (!organization) return <main className="sign-in"><div className="sign-in-card"><Brand /><h1>Create your organization</h1><p className="page-intro">Bring your persistent agents together.</p><form className="app-form" onSubmit={(event) => {
    event.preventDefault(); const name = new FormData(event.currentTarget).get('name'); setCreating(true);
    void api<Organization>('/organizations', { method: 'POST', csrf: session.csrf_token, body: { name } }).then((created) => { setOrganizations((items) => [...items, created]); saveWorkspaceLocation(created.id); setOrganization(created.id); }).catch((cause: unknown) => setError(errorMessage(cause)));
  }}><label>Organization name<input className="app-input" name="name" required maxLength={120} /></label><button className="app-button primary" disabled={creating}>Create organization</button>{organizations.length > 0 && <button className="app-button quiet" type="button" onClick={() => setOrganization(organizations[0].id)}>Cancel</button>}</form></div></main>;
  const managerOrganizationIds = organizations.filter((item) => organizationRoles[item.id] === 'manager').map((item) => item.id);
  return <OrganizationWorkspace key={organization} focusIdentity={focusIdentity} initialView={organizationView} managerOrganizationIds={managerOrganizationIds} organization={organization} organizations={organizations} onIdentityChanged={(updated) => setOrganizations((items) => items.map((item) => item.id === updated.id ? updated : item))} onOrganization={(id, view) => { setOrganizationView(view); setFocusIdentity(true); setCreating(false); saveWorkspaceLocation(id === '__new__' ? '' : id); setOrganization(id === '__new__' ? '' : id); }} onRefreshRoles={() => setRoleLookupVersion((current) => current + 1)} session={session} onLogout={onLogout} />;
}
function OrganizationWorkspace({ organization, organizations, managerOrganizationIds, initialView, onOrganization, onIdentityChanged, onRefreshRoles, focusIdentity, session, onLogout }: { focusIdentity: boolean; initialView?: 'chart' | 'organization'; managerOrganizationIds: string[]; organization: string; organizations: Organization[]; onIdentityChanged: (updated: Organization) => void; onOrganization: (id: string, view?: 'chart' | 'organization') => void; onRefreshRoles: () => void; session: LoginSession; onLogout: () => void }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (focusIdentity) headingRef.current?.focus(); }, [focusIdentity]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [selected, setSelected] = useState(() => readWorkspaceLocation().organization === organization ? readWorkspaceLocation().agent ?? '' : '');
  const [thread, setThread] = useState<string | undefined>(() => readWorkspaceLocation().organization === organization ? readWorkspaceLocation().thread : undefined);
  const lastThreads = useRef<Record<string, string | undefined>>({});
  const newThreadSequence = useRef(0);
  const [threadListTarget, setThreadListTarget] = useState<HTMLDivElement | null>(null);
  const [newThreadRequest, setNewThreadRequest] = useState<{ agent: string; id: number; project: string | null }>();
  const [newThreadEmployee, setNewThreadEmployee] = useState<string>();
  const [threadPageSize, setThreadPageSize] = useState(6);
  const projectNavigationKey = `fesnyng:project-navigation:${session.user.id}:${organization}`;
  const [navigationMode, setNavigationMode] = useState<'projects' | 'employees'>(() => window.localStorage.getItem(projectNavigationKey) === 'projects' ? 'projects' : 'employees');
  const [projectDetailsTarget, setProjectDetailsTarget] = useState<HTMLDivElement | null>(null);
  const [view, setView] = useState(() => initialView ?? (readWorkspaceLocation().organization === organization && readWorkspaceLocation().agent ? 'conversation' : 'chart'));
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [mobile, setMobile] = useState(false);
  const [version, setVersion] = useState(0);
  const [projectRevision, setProjectRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([api<Agent[]>(`/organizations/${organization}/agents`, { signal: controller.signal }), api<Member[]>(`/organizations/${organization}/members`, { signal: controller.signal })]).then(([items, people]) => {
      if (controller.signal.aborted) return; setAgents(items); setMembers(people);
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organization, version]);
  const manager = ['owner', 'admin'].includes(members.find((member) => member.user_id === session.user.id)?.role ?? '');
  const agent = agents.find((item) => item.id === selected);
  const navigate = (next: string) => { setView(next); setMobile(false); setError(''); };
  useEffect(() => { window.localStorage.setItem(projectNavigationKey, navigationMode); }, [navigationMode, projectNavigationKey]);
  const refreshWorkspace = () => { setVersion((current) => current + 1); onRefreshRoles(); };
  const openAgent = (id: string, nativeSession?: string) => { if (selected) lastThreads.current[selected] = thread; const nextThread = nativeSession ?? (id === selected ? thread : lastThreads.current[id]); saveWorkspaceLocation(organization, id, nextThread); setSelected(id); setThread(nextThread); navigate('conversation'); };
  const openNewThread = (id: string, project: string | null = null) => { if (selected) lastThreads.current[selected] = thread; saveWorkspaceLocation(organization, id); setSelected(id); setThread(undefined); newThreadSequence.current += 1; setNewThreadRequest({ agent: id, id: newThreadSequence.current, project }); navigate('conversation'); };
  const chooseNewThread = (id: string) => { setNewThreadEmployee(id); navigate('new-thread'); };
  const openOrganizationView = (id: string, next: 'chart' | 'organization') => { if (id === organization) navigate(next); else onOrganization(id, next); };
  return <ConversationDraftsProvider organization={organization}><ThreadNotificationsProvider organization={organization} agents={agents} csrf={session.csrf_token} selectedAgent={selected}><div className="app-shell"><aside className={`app-sidebar ${mobile ? 'is-open' : ''}`} aria-label="Workspace navigation"><OrganizationSwitcher organizations={organizations} selected={organization} managerOrganizationIds={managerOrganizationIds} onSelect={(id) => { if (id !== organization) onOrganization(id); }} onOpen={openOrganizationView} />
    <input type="search" className="app-input" aria-label="Search organization threads" placeholder="Search all threads…" value={search} onChange={(event) => setSearch(event.target.value)} />
    <div className="sidebar-view-toggle" role="group" aria-label="Thread navigation view"><button className="app-button quiet" aria-pressed={navigationMode === 'projects'} onClick={() => setNavigationMode('projects')}>Projects</button><button className="app-button quiet" aria-pressed={navigationMode === 'employees'} onClick={() => setNavigationMode('employees')}>Employees</button></div>
    <div className="sidebar-thread-navigation">
      {navigationMode === 'projects' ? <ProjectNavigation organization={organization} agents={agents} csrf={session.csrf_token} manager={manager} projectRevision={projectRevision} onChanged={() => setProjectRevision((current) => current + 1)} currentThread={selected && thread ? { agent: selected, session: thread } : undefined} detailsTarget={view === 'projects' ? projectDetailsTarget : null} onOpenProject={() => navigate('projects')} onOpenThread={(agentId, sessionId) => openAgent(agentId, sessionId)} onNewThread={({ agent: agentId, project }) => openNewThread(agentId, project)} /> : <><div className="sidebar-heading"><span>Agents · {agents.length}</span>{manager && <button className="app-button quiet" aria-label="Create agent" onClick={() => { setSelected(''); navigate('agent-settings'); }}>+</button>}</div>
      {search.trim() && <OrganizationThreadSearch organization={organization} agents={agents} query={search} onOpen={openAgent} />}
      <div className="agent-list" hidden={Boolean(search.trim())}>{agents.map((item) => <div className="agent-entry" key={item.id}>
        <div className="agent-card"><button className="agent-choice" aria-pressed={selected === item.id} aria-expanded={selected === item.id} onClick={() => openAgent(item.id)}><span className="agent-avatar-wrap"><span className="agent-avatar" aria-hidden="true">{item.name.slice(0, 2).toUpperCase()}</span><ThreadNotificationBadge agent={item.id} /></span><span className="agent-name-row"><span className="agent-name">{item.name}</span></span><small className="agent-role">{item.title || 'Agent'}</small></button><button className="agent-new-thread-button" aria-label={`New thread for ${item.name}`} title="New thread" onClick={() => chooseNewThread(item.id)}><PlusIcon aria-hidden="true" size={16} /></button></div>
        {selected === item.id && <div className="sidebar-agent-threads" role="region" aria-label={`${item.name} threads`} ref={setThreadListTarget} />}
      </div>)}</div></>}
    </div>
    <div className="sidebar-footer"><span>{session.user.display_name}</span><div className="sidebar-footer-actions"><UserPreferencesPanel organization={organization} csrf={session.csrf_token} onChange={setThreadPageSize} /><button className="sidebar-icon-button" aria-label="Sign out" title="Sign out" onClick={() => { void api('/auth/logout', { method: 'POST', csrf: session.csrf_token }).then(onLogout).catch((cause: unknown) => setError(errorMessage(cause))); }}><LogOutIcon aria-hidden="true" /><span className="sidebar-icon-tooltip" aria-hidden="true">Sign out</span></button></div></div>
  </aside><main className="app-workspace"><header className="workspace-header"><button className="app-button mobile-menu" onClick={() => setMobile(!mobile)} aria-label="Toggle navigation">☰</button><div><h1 ref={headingRef} tabIndex={-1}>{view === 'chart' ? 'Reporting chart' : view === 'organization' ? 'Organization settings' : view === 'projects' ? 'Projects' : agent?.name ?? 'Create agent'}</h1><p className="muted">{agent && !['chart', 'organization', 'projects'].includes(view) ? `${agent.title || 'Persistent agent'} · ${agent.configuration.workspace}` : organizations.find((item) => item.id === organization)?.name}</p></div>
    <div className="header-actions">{agent && !['chart', 'organization'].includes(view) && <><button className="app-button quiet header-icon-button" aria-label="Memory" title="Memory" onClick={() => navigate('memory')}><BrainIcon aria-hidden="true" /></button>{manager && <button className="app-button quiet header-icon-button" aria-label="Agent settings" title="Agent settings" onClick={() => navigate('agent-settings')}><SettingsIcon aria-hidden="true" /></button>}</>}<button className="app-button quiet header-icon-button" aria-label="Refresh workspace" title="Refresh workspace" onClick={refreshWorkspace}><RefreshCwIcon aria-hidden="true" /></button></div></header>
    {error && <p className="app-error" role="alert">{error}</p>}<div className="workspace-content"><NotificationErrors />
    {agent && <AgentConversation organization={organization} agent={agent} csrfToken={session.csrf_token} hidden={view !== 'conversation'} refreshKey={version} projectRevision={projectRevision} sessionId={thread} threadListTarget={navigationMode === 'employees' ? threadListTarget : null} newThreadRequest={newThreadRequest?.agent === agent.id ? newThreadRequest.id : undefined} projectId={newThreadRequest?.agent === agent.id ? newThreadRequest.project : null} onNewThreadStarted={() => {}} threadPageSize={threadPageSize} onThreadSelect={() => { navigate('conversation'); setMobile(false); }} onSessionChange={(id) => { setMobile(false); setThread(id); lastThreads.current[agent.id] = id; if (id) { setNewThreadRequest((current) => current?.agent === agent.id ? undefined : current); setProjectRevision((current) => current + 1); } saveWorkspaceLocation(organization, agent.id, id); }} onError={(cause) => setError(errorMessage(cause))} onOpen={openAgent} />}
    {view === 'projects' && <div className="workspace-page project-details-page" ref={setProjectDetailsTarget} />}
    {newThreadEmployee && agents.find((item) => item.id === newThreadEmployee) && <div className="workspace-page"><EmployeeNewThreadChooser organization={organization} agent={agents.find((item) => item.id === newThreadEmployee)!} onCancel={() => { setNewThreadEmployee(undefined); navigate('conversation'); }} onStart={(project) => { const employee = newThreadEmployee; setNewThreadEmployee(undefined); openNewThread(employee, project); }} /></div>}
    {view === 'agent-settings' && manager && <AgentSettings key={agent?.id ?? 'new'} organization={organization} agent={agent} agents={agents} csrf={session.csrf_token} onSaved={(saved) => { setAgents((items) => [...items.filter((item) => item.id !== saved.id), saved]); setSelected(saved.id); }} />}
    {view === 'organization' && manager && <OrganizationSettings onIdentityChanged={onIdentityChanged} organization={organization} csrf={session.csrf_token} agents={agents} onChanged={refreshWorkspace} />}
    {view === 'memory' && agent && <AgentMemory key={agent.id} organization={organization} agent={agent.id} csrf={session.csrf_token} />}
    {view === 'chart' && <DepartmentChart key={`${organization}:${version}`} organization={organization} agents={agents} manager={manager} csrf={session.csrf_token} onSelect={(item) => openAgent(item.id)} />}
    </div></main></div></ThreadNotificationsProvider></ConversationDraftsProvider>;
}
type SessionBinding = { session_id: string; title: string; runtime_type: 'opencode' | 'codex'; frozen: boolean; frozen_at?: number | null };
const sessionBindings = (value: unknown, defaultRuntime: SessionBinding['runtime_type']): SessionBinding[] => {
  if (!Array.isArray(value)) throw new Error('Could not read the agent thread inventory.');
  return value.filter((row): row is Record<string, unknown> => Boolean(
    row && typeof row === 'object'
    && typeof (row as Record<string, unknown>).session_id === 'string'
    && typeof (row as Record<string, unknown>).title === 'string'
  )).map((row) => ({
    session_id: row.session_id as string,
    title: row.title as string,
    runtime_type: row.runtime_type === 'codex' || row.runtime_type === 'opencode' ? row.runtime_type : defaultRuntime,
    frozen: row.frozen === true || typeof row.frozen_at === 'number',
    ...(typeof row.frozen_at === 'number' ? { frozen_at: row.frozen_at } : {}),
  }));
};

function FrozenHistoryNavigation({ bindings, current, onSelect }: { bindings: SessionBinding[]; current?: string; onSelect: (session: string) => void }) {
  const frozen = bindings.filter((binding) => binding.frozen);
  if (!frozen.length) return null;
  return <section className="app-panel" aria-label="Frozen thread history"><h3>Frozen history</h3><p className="muted">These original threads remain permanently read-only.</p>{frozen.map((binding) => <button key={binding.session_id} type="button" className="app-button quiet" aria-current={binding.session_id === current ? 'page' : undefined} onClick={() => onSelect(binding.session_id)}>{binding.title} <span className="muted">({binding.runtime_type === 'codex' ? 'Codex' : 'OpenCode'})</span></button>)}</section>;
}

export function AgentConversation({ organization, agent, csrfToken, hidden, refreshKey, projectRevision = 0, sessionId, threadListTarget, newThreadRequest, projectId, onNewThreadStarted, threadPageSize, onThreadSelect, onSessionChange, onError, onOpen }: { organization: string; agent: Agent; csrfToken: string; hidden: boolean; refreshKey: number; projectRevision?: number; sessionId: string | undefined; threadListTarget: HTMLElement | null; newThreadRequest: number | undefined; projectId?: string | null; onNewThreadStarted: (request: number) => void; threadPageSize: number; onThreadSelect: () => void; onSessionChange: (id: string | undefined) => void; onError: (cause: unknown) => void; onOpen: (id: string, session?: string) => void }) {
  const inventoryKey = `${organization}:${agent.id}:${refreshKey}:${sessionId ?? ''}`;
  const [inventory, setInventory] = useState<{ key: string; bindings?: SessionBinding[]; error?: string }>({ key: '' });
  const [projectLabels, setProjectLabels] = useState<Record<string, string>>({});
  useEffect(() => {
    const controller = new AbortController();
    void api<unknown>(`${agentPath(organization, agent.id)}/sessions`, { signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setInventory({ key: inventoryKey, bindings: sessionBindings(value, agent.configuration.runtime_type ?? 'opencode') });
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setInventory({ key: inventoryKey, error: errorMessage(cause) }); });
    return () => controller.abort();
  }, [agent.configuration.runtime_type, agent.id, inventoryKey, organization]);
  useEffect(() => {
    const controller = new AbortController();
    void api<unknown>(`${agentPath(organization, agent.id)}/sessions`, { signal: controller.signal }).then(async () => {
      const [projects, grouping] = await Promise.all([
        api<Project[]>(`/organizations/${organization}/projects?include_archived=true`, { signal: controller.signal }),
        api<{ threads: Array<{ session_id: string; project_id: string | null }> }>(`${agentPath(organization, agent.id)}/thread-projects`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      const names = new Map(projects.map((project) => [project.id, project.name]));
      setProjectLabels(Object.fromEntries(grouping.threads.flatMap((thread) => thread.project_id && names.has(thread.project_id) ? [[thread.session_id, names.get(thread.project_id)!]] : [])));
    }).catch(() => { if (!controller.signal.aborted) setProjectLabels({}); });
    return () => controller.abort();
  }, [agent.id, organization, projectRevision, refreshKey]);
  const bindings = inventory.key === inventoryKey ? inventory.bindings : undefined;
  const bindingError = inventory.key === inventoryKey ? inventory.error ?? '' : '';
  const selectedBinding = sessionId ? bindings?.find((binding) => binding.session_id === sessionId) : undefined;
  const harness = selectedBinding?.runtime_type ?? (agent.configuration.runtime_type ?? 'opencode');
  const readOnly = selectedBinding?.frozen === true;
  if (sessionId && !selectedBinding && bindings && bindings.length > 0) return <div className="conversation-region" hidden={hidden}><p className="app-error" role="alert">This thread is not available in the immutable agent inventory.</p></div>;
  if (sessionId && !selectedBinding && !bindings && !bindingError) return <div className="conversation-region" hidden={hidden}><p role="status" className="app-empty">Loading thread history…</p></div>;
  if (bindingError) return <div className="conversation-region" hidden={hidden}><p className="app-error" role="alert">{bindingError}</p></div>;
  const navigation = <FrozenHistoryNavigation bindings={bindings ?? []} current={sessionId} onSelect={(id) => { onSessionChange(id); onThreadSelect(); }} />;
  if (!sessionId && agent.configuration_status === 'pending') return <div className="conversation-region" hidden={hidden}>{threadListTarget ? createPortal(navigation, threadListTarget) : navigation}<p className="app-notice" role="status">The selected harness is still applying. New threads will be available after configuration finishes.</p></div>;
  const sessionChanged = (id: string | undefined) => { onSessionChange(id); };
  const conversation = <ConversationBoundary key={`${agent.id}:${sessionId ?? 'new'}:${harness}:${readOnly}`}><Suspense fallback={<p role="status" className="app-empty">Loading conversation…</p>}>{harness === 'codex' ? <CodexConversation key={agent.id} baseUrl={new URL(`/api/organizations/${organization}/agents/${agent.id}/codex`, window.location.origin).href} csrfToken={csrfToken} refreshKey={refreshKey} sessionId={sessionId} showThreadList={false} threadListTarget={readOnly ? null : threadListTarget} newThreadRequest={readOnly ? undefined : newThreadRequest} projectId={projectId} projectLabels={projectLabels} onNewThreadStarted={onNewThreadStarted} threadPageSize={threadPageSize} onThreadSelect={onThreadSelect} onSessionChange={sessionChanged} onError={onError} readOnly={readOnly} /> : <Conversation key={agent.id} baseUrl={new URL(`/api/organizations/${organization}/agents/${agent.id}/opencode`, window.location.origin).href} csrfToken={csrfToken} refreshKey={refreshKey} sessionId={sessionId} showThreadList={false} threadListTarget={readOnly ? null : threadListTarget} newThreadRequest={readOnly ? undefined : newThreadRequest} projectId={projectId} projectLabels={projectLabels} onNewThreadStarted={onNewThreadStarted} threadPageSize={threadPageSize} onThreadSelect={onThreadSelect} onSessionChange={sessionChanged} onError={onError} readOnly={readOnly} />}</Suspense></ConversationBoundary>;
  const shell = <div className="conversation-region" hidden={hidden}>{threadListTarget ? createPortal(navigation, threadListTarget) : navigation}{conversation}</div>;
  return <ConversationDeliveryProvider organization={organization} agent={agent.id} session={sessionId} csrf={csrfToken} onOpen={onOpen} readOnly={readOnly}>{shell}</ConversationDeliveryProvider>;
}
function UserPreferencesPanel({ organization, csrf, onChange }: { organization: string; csrf: string; onChange: (size: number) => void }) {
  const [open, setOpen] = useState(false);
  const preference = useThreadPagePreference({ organization, csrf, onChange });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><button className="sidebar-icon-button" aria-label="User preferences" title="User preferences"><SlidersHorizontalIcon aria-hidden="true" /><span className="sidebar-icon-tooltip" aria-hidden="true">User preferences</span></button></DialogTrigger>
      <DialogContent aria-label="User preferences" className="preferences-panel" showCloseButton={false}><DialogHeader className="preferences-panel-header"><DialogTitle>User preferences</DialogTitle><DialogClose asChild><button className="sidebar-icon-button" aria-label="Close preferences" title="Close preferences"><XIcon aria-hidden="true" /></button></DialogClose></DialogHeader><ThreadPagePreferenceForm preference={preference} /></DialogContent>
    </Dialog>;
}
function NotificationErrors() {
  const notifications = useThreadNotifications();
  if (!notifications?.errors.length) return null;
  return <details className="app-notice"><summary>Some notification updates failed</summary>
    {notifications.errors.map((error) => <p key={error}>{error}</p>)}
    <button className="app-button" onClick={() => { void notifications.refresh(); }}>Retry notifications</button>
  </details>;
}
