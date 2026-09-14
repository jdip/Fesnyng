import { lazy, Suspense, useEffect, useRef, useState } from 'react';
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
import { AgentSettings } from './AgentSettings';
import { OrganizationSettings } from './OrganizationSettings';
import { AgentMemory } from './AgentMemory';
import { DepartmentChart } from './DepartmentChart';
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
  const [newThreadRequest, setNewThreadRequest] = useState<{ agent: string; id: number }>();
  const [threadPageSize, setThreadPageSize] = useState(6);
  const [view, setView] = useState(() => initialView ?? (readWorkspaceLocation().organization === organization && readWorkspaceLocation().agent ? 'conversation' : 'chart'));
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
  const refreshWorkspace = () => { setVersion((current) => current + 1); onRefreshRoles(); };
  const openAgent = (id: string, nativeSession?: string) => { if (selected) lastThreads.current[selected] = thread; const nextThread = nativeSession ?? (id === selected ? thread : lastThreads.current[id]); saveWorkspaceLocation(organization, id, nextThread); setSelected(id); setThread(nextThread); navigate('conversation'); };
  const openNewThread = (id: string) => { if (selected) lastThreads.current[selected] = thread; saveWorkspaceLocation(organization, id); setSelected(id); setThread(undefined); newThreadSequence.current += 1; setNewThreadRequest({ agent: id, id: newThreadSequence.current }); navigate('conversation'); };
  const openOrganizationView = (id: string, next: 'chart' | 'organization') => { if (id === organization) navigate(next); else onOrganization(id, next); };
  return <ConversationDraftsProvider organization={organization}><ThreadNotificationsProvider organization={organization} agents={agents} csrf={session.csrf_token} selectedAgent={selected}><div className="app-shell"><aside className={`app-sidebar ${mobile ? 'is-open' : ''}`} aria-label="Workspace navigation"><OrganizationSwitcher organizations={organizations} selected={organization} managerOrganizationIds={managerOrganizationIds} onSelect={(id) => { if (id !== organization) onOrganization(id); }} onOpen={openOrganizationView} />
    <input type="search" className="app-input" aria-label="Search organization threads" placeholder="Search all threads…" value={search} onChange={(event) => setSearch(event.target.value)} />
    <div className="sidebar-heading"><span>Agents · {agents.length}</span>{manager && <button className="app-button quiet" aria-label="Create agent" onClick={() => { setSelected(''); navigate('agent-settings'); }}>+</button>}</div>
    <div className="sidebar-thread-navigation">
      {search.trim() && <OrganizationThreadSearch organization={organization} agents={agents} query={search} onOpen={openAgent} />}
      <div className="agent-list" hidden={Boolean(search.trim())}>{agents.map((item) => <div className="agent-entry" key={item.id}>
        <div className="agent-card"><button className="agent-choice" aria-pressed={selected === item.id} aria-expanded={selected === item.id} onClick={() => openAgent(item.id)}><span className="agent-avatar" aria-hidden="true">{item.name.slice(0, 2).toUpperCase()}</span><span className="agent-name-row"><span className="agent-name">{item.name}</span><ThreadNotificationBadge agent={item.id} /></span><small className="agent-role">{item.title || 'Agent'}</small></button><button className="agent-new-thread-button" aria-label={`New thread for ${item.name}`} title="New thread" onClick={() => openNewThread(item.id)}><PlusIcon aria-hidden="true" size={16} /></button></div>
        {selected === item.id && <div className="sidebar-agent-threads" role="region" aria-label={`${item.name} threads`} ref={setThreadListTarget} />}
      </div>)}</div>
    </div>
    <div className="sidebar-footer"><span>{session.user.display_name}</span><div className="sidebar-footer-actions"><UserPreferencesPanel organization={organization} csrf={session.csrf_token} onChange={setThreadPageSize} /><button className="sidebar-icon-button" aria-label="Sign out" title="Sign out" onClick={() => { void api('/auth/logout', { method: 'POST', csrf: session.csrf_token }).then(onLogout).catch((cause: unknown) => setError(errorMessage(cause))); }}><LogOutIcon aria-hidden="true" /><span className="sidebar-icon-tooltip" aria-hidden="true">Sign out</span></button></div></div>
  </aside><main className="app-workspace"><header className="workspace-header"><button className="app-button mobile-menu" onClick={() => setMobile(!mobile)} aria-label="Toggle navigation">☰</button><div><h1 ref={headingRef} tabIndex={-1}>{view === 'chart' ? 'Reporting chart' : view === 'organization' ? 'Organization settings' : agent?.name ?? 'Create agent'}</h1><p className="muted">{agent && !['chart', 'organization'].includes(view) ? `${agent.title || 'Persistent agent'} · ${agent.configuration.workspace}` : organizations.find((item) => item.id === organization)?.name}</p></div>
    <div className="header-actions">{agent && !['chart', 'organization'].includes(view) && <><button className="app-button quiet header-icon-button" aria-label="Memory" title="Memory" onClick={() => navigate('memory')}><BrainIcon aria-hidden="true" /></button>{manager && <button className="app-button quiet header-icon-button" aria-label="Agent settings" title="Agent settings" onClick={() => navigate('agent-settings')}><SettingsIcon aria-hidden="true" /></button>}</>}<button className="app-button quiet header-icon-button" aria-label="Refresh workspace" title="Refresh workspace" onClick={refreshWorkspace}><RefreshCwIcon aria-hidden="true" /></button></div></header>
    {error && <p className="app-error" role="alert">{error}</p>}<div className="workspace-content"><NotificationErrors />
    {agent && <ConversationDeliveryProvider organization={organization} agent={agent.id} session={thread} csrf={session.csrf_token} onOpen={openAgent}><div className="conversation-region" hidden={view !== 'conversation'}><ConversationBoundary key={agent.id}><Suspense fallback={<p role="status" className="app-empty">Loading conversation…</p>}><Conversation key={agent.id} baseUrl={new URL(`/api/organizations/${organization}/agents/${agent.id}/opencode`, window.location.origin).href} csrfToken={session.csrf_token} refreshKey={version} sessionId={thread} showThreadList={false} threadListTarget={threadListTarget} newThreadRequest={newThreadRequest?.agent === agent.id ? newThreadRequest.id : undefined} onNewThreadStarted={(request) => setNewThreadRequest((current) => current?.id === request ? undefined : current)} threadPageSize={threadPageSize} onThreadSelect={() => { navigate('conversation'); setMobile(false); }} onSessionChange={(id) => { setMobile(false); setThread(id); lastThreads.current[agent.id] = id; saveWorkspaceLocation(organization, agent.id, id); }} onError={(cause) => setError(errorMessage(cause))} /></Suspense></ConversationBoundary></div></ConversationDeliveryProvider>}
    {view === 'agent-settings' && manager && <AgentSettings key={agent?.id ?? 'new'} organization={organization} agent={agent} agents={agents} csrf={session.csrf_token} onSaved={(saved) => { setAgents((items) => [...items.filter((item) => item.id !== saved.id), saved]); setSelected(saved.id); }} />}
    {view === 'organization' && manager && <OrganizationSettings onIdentityChanged={onIdentityChanged} organization={organization} csrf={session.csrf_token} agents={agents} onChanged={refreshWorkspace} />}
    {view === 'memory' && agent && <AgentMemory key={agent.id} organization={organization} agent={agent.id} csrf={session.csrf_token} />}
    {view === 'chart' && <DepartmentChart key={`${organization}:${version}`} organization={organization} agents={agents} manager={manager} csrf={session.csrf_token} onSelect={(item) => openAgent(item.id)} />}
    </div></main></div></ThreadNotificationsProvider></ConversationDraftsProvider>;
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
