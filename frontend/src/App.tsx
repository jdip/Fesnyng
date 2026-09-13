import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { NetworkIcon, SettingsIcon } from 'lucide-react';
import { readWorkspaceLocation, saveWorkspaceLocation } from './workspace-location';
import { ConversationBoundary } from './ConversationBoundary';
import { ThreadNotificationsProvider, useThreadNotifications } from './ThreadNotifications';
import { ThreadNotificationBadge } from './ThreadNotificationBadge';
import { ConversationDraftsProvider } from './ConversationDrafts';
import { OrganizationThreadSearch } from './OrganizationThreadSearch';
import { ThreadPagePreference } from './ThreadPagePreference';
import { ThreadContext } from './ThreadContext';
const Conversation = lazy(async () => ({ default: (await import('./Conversation')).Conversation }));
import { AgentSettings } from './AgentSettings';
import { OrganizationSettings } from './OrganizationSettings';
import { AgentMemory } from './AgentMemory';
import { api, ApiError, errorMessage, type LoginSession, type Organization, type Agent, type Member } from './workspace-api';

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
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void api<Organization[]>('/organizations', { signal: controller.signal }).then((items) => {
      if (controller.signal.aborted) return;
      setLoading(false); setOrganizations(items); const saved = readWorkspaceLocation().organization; setOrganization(items.find((item) => item.id === saved)?.id ?? items[0]?.id ?? '');
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, []);
  if (error) return <main className="workspace-page"><p className="app-error" role="alert">{error}</p><button className="app-button" onClick={onLogout}>Sign in again</button></main>;
  if (loading) return <div className="app-empty" role="status">Loading organizations…</div>;
  if (!organization) return <main className="sign-in"><div className="sign-in-card"><Brand /><h1>Create your organization</h1><p className="page-intro">Bring your persistent agents together.</p><form className="app-form" onSubmit={(event) => {
    event.preventDefault(); const name = new FormData(event.currentTarget).get('name'); setCreating(true);
    void api<Organization>('/organizations', { method: 'POST', csrf: session.csrf_token, body: { name } }).then((created) => { setOrganizations((items) => [...items, created]); saveWorkspaceLocation(created.id); setOrganization(created.id); }).catch((cause: unknown) => setError(errorMessage(cause)));
  }}><label>Organization name<input className="app-input" name="name" required maxLength={120} /></label><button className="app-button primary" disabled={creating}>Create organization</button>{organizations.length > 0 && <button className="app-button quiet" type="button" onClick={() => setOrganization(organizations[0].id)}>Cancel</button>}</form></div></main>;
  return <OrganizationWorkspace key={organization} organization={organization} organizations={organizations} onOrganization={(id) => { setCreating(false); saveWorkspaceLocation(id === '__new__' ? '' : id); setOrganization(id === '__new__' ? '' : id); }} session={session} onLogout={onLogout} />;
}
function OrganizationWorkspace({ organization, organizations, onOrganization, session, onLogout }: { organization: string; organizations: Organization[]; onOrganization: (id: string) => void; session: LoginSession; onLogout: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [selected, setSelected] = useState(() => readWorkspaceLocation().organization === organization ? readWorkspaceLocation().agent ?? '' : '');
  const [thread, setThread] = useState<string | undefined>(() => readWorkspaceLocation().organization === organization ? readWorkspaceLocation().thread : undefined);
  const lastThreads = useRef<Record<string, string | undefined>>({});
  const [threadListTarget, setThreadListTarget] = useState<HTMLDivElement | null>(null);
  const [threadPageSize, setThreadPageSize] = useState(6);
  const [view, setView] = useState(() => readWorkspaceLocation().organization === organization && readWorkspaceLocation().agent ? 'conversation' : 'chart');
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [mobile, setMobile] = useState(false);
  const [version, setVersion] = useState(0);
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
  const openAgent = (id: string, nativeSession?: string) => { if (selected) lastThreads.current[selected] = thread; const nextThread = nativeSession ?? (id === selected ? thread : lastThreads.current[id]); saveWorkspaceLocation(organization, id, nextThread); setSelected(id); setThread(nextThread); navigate('conversation'); };
  return <ConversationDraftsProvider organization={organization}><ThreadNotificationsProvider organization={organization} agents={agents} csrf={session.csrf_token} selectedAgent={selected}><div className="app-shell"><aside className={`app-sidebar ${mobile ? 'is-open' : ''}`} aria-label="Workspace navigation"><Brand />
    <label className="app-label">Organization<select className="app-select" value={organization} onChange={(event) => onOrganization(event.target.value)}>{organizations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}<option value="__new__">+ Create organization</option></select></label>
    <div className="sidebar-heading"><span>Agents · {agents.length}</span>{manager && <button className="app-button quiet" aria-label="Create agent" onClick={() => { setSelected(''); navigate('agent-settings'); }}>+</button>}</div>
    <input type="search" className="app-input" aria-label="Search organization threads" placeholder="Search all threads…" value={search} onChange={(event) => setSearch(event.target.value)} />
    <div className="sidebar-thread-navigation">
      {search.trim() && <OrganizationThreadSearch organization={organization} agents={agents} query={search} onOpen={openAgent} />}
      <div className="agent-list" hidden={Boolean(search.trim())}>{agents.map((item) => <div key={item.id}>
        <button className="agent-choice" aria-pressed={selected === item.id} aria-expanded={selected === item.id} onClick={() => openAgent(item.id)}><span className="agent-avatar" aria-hidden="true">{item.name.slice(0, 2).toUpperCase()}</span><span>{item.name}<small>{item.title || 'Agent'}</small></span><ThreadNotificationBadge agent={item.id} /></button>
        {selected === item.id && <div className="sidebar-agent-threads" role="region" aria-label={`${item.name} threads`} ref={setThreadListTarget} />}
      </div>)}</div>
    </div>
    <ThreadPagePreference organization={organization} csrf={session.csrf_token} onChange={setThreadPageSize} />
    <div className="sidebar-utilities" aria-label="Organization navigation"><button className="sidebar-icon-button" aria-label="Reporting chart" aria-current={view === 'chart' ? 'page' : undefined} title="Reporting chart" onClick={() => navigate('chart')}><NetworkIcon aria-hidden="true" /><span className="sidebar-icon-tooltip" aria-hidden="true">Reporting chart</span></button>{manager && <button className="sidebar-icon-button" aria-label="Organization settings" aria-current={view === 'organization' ? 'page' : undefined} title="Organization settings" onClick={() => navigate('organization')}><SettingsIcon aria-hidden="true" /><span className="sidebar-icon-tooltip" aria-hidden="true">Organization settings</span></button>}</div>
    <div className="sidebar-footer"><span>{session.user.display_name}</span><button className="app-button quiet" onClick={() => { void api('/auth/logout', { method: 'POST', csrf: session.csrf_token }).then(onLogout).catch((cause: unknown) => setError(errorMessage(cause))); }}>Sign out</button></div>
  </aside><main className="app-workspace"><header className="workspace-header"><button className="app-button mobile-menu" onClick={() => setMobile(!mobile)} aria-label="Toggle navigation">☰</button><div><h1>{view === 'chart' ? 'Reporting chart' : view === 'organization' ? 'Organization settings' : agent?.name ?? 'Create agent'}</h1><p className="muted">{agent && !['chart', 'organization'].includes(view) ? `${agent.title || 'Persistent agent'} · ${agent.configuration.workspace}` : organizations.find((item) => item.id === organization)?.name}</p></div>
    <div className="header-actions">{agent && !['chart', 'organization'].includes(view) && <><button className="app-button" onClick={() => navigate('conversation')}>Threads</button><button className="app-button" onClick={() => navigate('memory')}>Memory</button>{manager && <button className="app-button" onClick={() => navigate('agent-settings')}>Agent settings</button>}</>}<button className="app-button quiet" onClick={() => setVersion((current) => current + 1)}>Refresh</button></div></header>
    {error && <p className="app-error" role="alert">{error}</p>}<div className="workspace-content"><NotificationErrors />
    {agent && <div className="conversation-region" hidden={view !== 'conversation'}>{view === 'conversation' && thread && <ThreadContext key={`${agent.id}:${thread}`} organization={organization} agent={agent.id} session={thread} csrf={session.csrf_token} onOpen={openAgent} />}<ConversationBoundary key={agent.id}><Suspense fallback={<p role="status" className="app-empty">Loading conversation…</p>}><Conversation key={agent.id} baseUrl={new URL(`/api/organizations/${organization}/agents/${agent.id}/opencode`, window.location.origin).href} csrfToken={session.csrf_token} refreshKey={version} sessionId={thread} showThreadList={false} threadListTarget={threadListTarget} threadPageSize={threadPageSize} onThreadSelect={() => navigate('conversation')} onSessionChange={(id) => { setMobile(false); setThread(id); lastThreads.current[agent.id] = id; saveWorkspaceLocation(organization, agent.id, id); }} onError={(cause) => setError(errorMessage(cause))} /></Suspense></ConversationBoundary></div>}
    {view === 'agent-settings' && manager && <AgentSettings key={agent?.id ?? 'new'} organization={organization} agent={agent} agents={agents} csrf={session.csrf_token} onSaved={(saved) => { setAgents((items) => [...items.filter((item) => item.id !== saved.id), saved]); setSelected(saved.id); }} />}
    {view === 'organization' && manager && <OrganizationSettings organization={organization} csrf={session.csrf_token} agents={agents} onChanged={() => setVersion((current) => current + 1)} />}
    {view === 'memory' && agent && <AgentMemory key={agent.id} organization={organization} agent={agent.id} csrf={session.csrf_token} />}
    {view === 'chart' && <div className="workspace-page"><p className="page-intro">Reporting relationships help agents find the right collaborator.</p><ReportingChart agents={agents} onOpen={openAgent} />{!agents.length && <p className="app-empty">{manager ? 'Create your first agent to get started.' : 'Your organization has no agents yet.'}</p>}</div>}
    </div></main></div></ThreadNotificationsProvider></ConversationDraftsProvider>;
}
function ReportingChart({ agents, onOpen }: { agents: Agent[]; onOpen: (id: string) => void }) {
  function branch(parent: string | null, seen: Set<string>) {
    return <ul className="chart-list">{agents.filter((agent) => (agent.reports_to_agent_id ?? null) === parent && !seen.has(agent.id)).map((agent) => <li key={agent.id}><button className="chart-node" onClick={() => onOpen(agent.id)}><span className="agent-avatar">{agent.name.slice(0, 2).toUpperCase()}</span><span>{agent.name}<small>{agent.title || 'Agent'}</small></span></button>{agents.some((item) => item.reports_to_agent_id === agent.id) && branch(agent.id, new Set([...seen, agent.id]))}</li>)}</ul>;
  }
  return branch(null, new Set());
}
function NotificationErrors() {
  const notifications = useThreadNotifications();
  if (!notifications?.errors.length) return null;
  return <details className="app-notice"><summary>Some notification updates failed</summary>
    {notifications.errors.map((error) => <p key={error}>{error}</p>)}
    <button className="app-button" onClick={() => { void notifications.refresh(); }}>Retry notifications</button>
  </details>;
}
