import { createPortal } from 'react-dom';
import { useEffect, useMemo, useRef, useState } from 'react';
import { PlusIcon } from 'lucide-react';
import { Dialog, DialogClose, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from './components/ui/dialog';
import { agentPath, api, errorMessage, type Agent, type Project } from './workspace-api';
import { WorkspaceInspectionList, type WorkspaceUpdate } from './WorkspaceInspection';

type Thread = { agent: string; session_id: string; title: string; project_id: string | null };
type ProjectDraft = Pick<Project, 'name' | 'description' | 'target_repository_url' | 'default_checkout_branch'>;
export type ProjectThreadSelection = { project: string | null; checkoutBranch?: string };
export type ProjectThreadStart = ProjectThreadSelection & { agent: string };

const emptyDraft = (): ProjectDraft => ({ name: '', description: '', target_repository_url: null, default_checkout_branch: null });

function projectDraft(project?: Project): ProjectDraft {
  return project ? {
    name: project.name,
    description: project.description,
    target_repository_url: project.target_repository_url,
    default_checkout_branch: project.default_checkout_branch,
  } : emptyDraft();
}

function threadRows(value: unknown, agent: string, projects: Record<string, string | null>): Thread[] {
  if (!Array.isArray(value)) throw new Error('Could not read the employee thread inventory.');
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const row = item as Record<string, unknown>;
    return typeof row.session_id === 'string' && typeof row.title === 'string'
      ? [{ agent, session_id: row.session_id, title: row.title, project_id: projects[row.session_id] ?? null }]
      : [];
  });
}

export function ProjectNavigation({ organization, agents, csrf, manager, onOpenThread, onNewThread, detailsTarget, onOpenProject, currentThread, projectRevision = 0, onChanged, onWorkspaceOperation }: {
  organization: string;
  agents: Agent[];
  csrf: string;
  manager: boolean;
  onOpenThread: (agent: string, session: string) => void;
  onNewThread: (selection: ProjectThreadStart) => void;
  detailsTarget?: HTMLElement | null;
  onOpenProject?: () => void;
  currentThread?: { agent: string; session: string };
  projectRevision?: number;
  onChanged?: () => void;
  onWorkspaceOperation?: (update: WorkspaceUpdate) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [selected, setSelected] = useState<string | null>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [unavailableEmployees, setUnavailableEmployees] = useState<string[]>([]);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void api<Project[]>(`/organizations/${organization}/projects?include_archived=true`, { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) { setProjects(items); setError(''); }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    void Promise.allSettled(agents.map(async (agent) => {
        const sessions = await api<unknown>(`${agentPath(organization, agent.id)}/sessions`, { signal: controller.signal });
        const grouping = await api<{ threads: Array<{ session_id: string; project_id: string | null }> }>(`${agentPath(organization, agent.id)}/thread-projects`, { signal: controller.signal });
        const mappings = Object.fromEntries(grouping.threads.map((thread) => [thread.session_id, thread.project_id]));
        return threadRows(sessions, agent.id, mappings);
      })).then((records) => {
      if (controller.signal.aborted) return;
      setThreads(records.flatMap((result) => result.status === 'fulfilled' ? result.value : []));
      setUnavailableEmployees(records.flatMap((result, index) => result.status === 'rejected' ? [agents[index]!.name] : []));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [agents, currentThread?.agent, currentThread?.session, organization, projectRevision, revision]);

  const activeProjects = projects.filter((project) => !project.archived);
  const knownProjectIds = new Set(projects.map((project) => project.id));
  const visibleThreads = threads.map((thread) => knownProjectIds.has(thread.project_id ?? '') ? thread : { ...thread, project_id: null });
  const revealedProject = selected === undefined && currentThread ? visibleThreads.find((thread) => thread.agent === currentThread.agent && thread.session_id === currentThread.session)?.project_id ?? null : selected;
  const selectedProject = revealedProject === null ? undefined : projects.find((project) => project.id === revealedProject);
  const selectedThreads = revealedProject === undefined ? [] : visibleThreads.filter((thread) => (selectedProject ? thread.project_id === selectedProject.id : thread.project_id === null));
  const selectedName = selectedProject?.name ?? 'Ungrouped';

  const moveThread = async (thread: Thread, project_id: string | null) => {
    try {
      await api(`${agentPath(organization, thread.agent)}/sessions/${encodeURIComponent(thread.session_id)}/project`, { method: 'PUT', csrf, body: { project_id } });
      setThreads((items) => items.map((item) => item.agent === thread.agent && item.session_id === thread.session_id ? { ...item, project_id } : item));
      onChanged?.();
    } catch (cause) { setError(errorMessage(cause)); }
  };
  const saveProject = async (draft: ProjectDraft, project?: Project) => {
    const body = {
      name: draft.name.trim(), description: draft.description?.trim() || null,
      target_repository_url: draft.target_repository_url?.trim() || null,
      default_checkout_branch: draft.default_checkout_branch?.trim() || null,
    };
    try {
      const saved = await api<Project>(`/organizations/${organization}/projects${project ? `/${project.id}` : ''}`, { method: project ? 'PATCH' : 'POST', csrf, body });
      setProjects((items) => [...items.filter((item) => item.id !== saved.id), saved]);
      setSelected(saved.id);
      setError('');
      onChanged?.();
    } catch (cause) {
      setError(errorMessage(cause));
      throw cause;
    }
  };
  const lifecycleProject = async (project: Project, action: 'archive' | 'restore' | 'delete') => {
    try {
      await api(`/organizations/${organization}/projects/${project.id}${action === 'delete' ? '' : `/${action}`}`, { method: action === 'delete' ? 'DELETE' : 'POST', csrf });
      setProjects((items) => action === 'delete' ? items.filter((item) => item.id !== project.id) : items.map((item) => item.id === project.id ? { ...item, archived: action === 'archive' } : item));
      if (action === 'delete') setThreads((items) => items.map((item) => item.project_id === project.id ? { ...item, project_id: null } : item));
      setSelected(undefined);
      onChanged?.();
    } catch (cause) { setError(errorMessage(cause)); }
  };

  const details = revealedProject !== undefined ? <ProjectDetails key={revealedProject ?? 'ungrouped'} organization={organization} csrf={csrf} project={selectedProject} threads={selectedThreads} allProjects={activeProjects} agents={agents} manager={manager} error={error} selectedName={selectedName} onOpenThread={onOpenThread} onMoveThread={moveThread} onNewThread={onNewThread} onSaveProject={saveProject} onLifecycleProject={lifecycleProject} onWorkspaceOperation={onWorkspaceOperation} /> : <p className="muted project-empty">Choose a Project to see its threads and settings.</p>;
  if (loading) return <p role="status" className="muted">Loading Projects…</p>;
  return <div className="projects-layout">
    <nav className="project-list" aria-label="Projects">
      <div className="sidebar-heading"><span>Projects · {activeProjects.length}</span><ProjectEditor trigger="Create Project" onSave={saveProject} /></div>
      {activeProjects.map((project) => <button key={project.id} className="project-choice" aria-label={project.name} aria-pressed={revealedProject === project.id} onClick={() => { setSelected(project.id); onOpenProject?.(); }}><span>{project.name}</span><small>{visibleThreads.filter((thread) => thread.project_id === project.id).length} threads</small></button>)}
      <button className="project-choice" aria-label="Ungrouped" aria-pressed={revealedProject === null} onClick={() => { setSelected(null); onOpenProject?.(); }}><span>Ungrouped</span><small>{visibleThreads.filter((thread) => thread.project_id === null).length} threads</small></button>
      {manager && projects.some((project) => project.archived) && <details className="archived-projects"><summary>Archived Projects</summary>{projects.filter((project) => project.archived).map((project) => <button className="project-choice" key={project.id} aria-pressed={selected === project.id} onClick={() => { setSelected(project.id); onOpenProject?.(); }}>{project.name}</button>)}</details>}
    </nav>
    {detailsTarget ? createPortal(details, detailsTarget) : <div className="project-sidebar-threads">{selectedThreads.map((thread) => <button key={`${thread.agent}:${thread.session_id}`} className="project-sidebar-thread" aria-current={currentThread?.agent === thread.agent && currentThread.session === thread.session_id ? 'page' : undefined} onClick={() => onOpenThread(thread.agent, thread.session_id)}><span title={thread.title}>{thread.title}</span>{' '}<small>{agents.find((agent) => agent.id === thread.agent)?.name ?? thread.agent}</small></button>)}{revealedProject !== undefined && !selectedThreads.length && <p className="muted">No threads in this group.</p>}{revealedProject === undefined && <p className="muted">Select a Project to reveal its threads.</p>}</div>}
    {unavailableEmployees.length > 0 && <p className="muted" role="status">Unavailable: {unavailableEmployees.join(', ')}</p>}
    {error && <p className="app-error" role="alert">{error} <button className="app-button quiet" onClick={() => setRevision((value) => value + 1)}>Retry</button></p>}
  </div>;
}

/** Employee-first creation keeps its established direct path when no Project exists. */
export function EmployeeNewThreadChooser({ organization, agent, onStart, onCancel }: { organization: string; agent: Agent; onStart: (selection: ProjectThreadSelection) => void; onCancel: () => void }) {
  const [projects, setProjects] = useState<Project[]>();
  const [project, setProject] = useState('');
  const [checkoutBranch, setCheckoutBranch] = useState('');
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<Project[]>(`/organizations/${organization}/projects`, { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) setProjects(items.filter((item) => !item.archived));
    }).catch((cause) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organization, revision]);
  useEffect(() => { if (projects?.length === 0) onStart({ project: null }); }, [onStart, projects]);
  if (error) return <section className="app-panel project-thread-creator" aria-label="New thread"><p className="app-error" role="alert">{error}</p><div className="app-actions"><button className="app-button" onClick={onCancel}>Cancel</button><button className="app-button" onClick={() => { setError(''); setRevision((current) => current + 1); }}>Retry</button></div></section>;
  if (!projects) return <section className="app-panel project-thread-creator" aria-label="New thread"><p role="status">Loading Projects…</p><button className="app-button" onClick={onCancel}>Cancel</button></section>;
  if (projects.length === 0) return null;
  const selected = projects.find((item) => item.id === project);
  const repositoryInvalid = Boolean(selected?.target_repository_url && !selected.default_checkout_branch);
  return <section className="app-panel project-thread-creator" aria-label={`New thread for ${agent.name}`}><h3>New thread for {agent.name}</h3><label>Project<select className="app-select" aria-label="Project" value={project} onChange={(event) => { setProject(event.target.value); setCheckoutBranch(''); }}><option value="">Ungrouped</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{selected?.target_repository_url && <><p className="app-notice">This thread will prepare an independent workspace from {selected.default_checkout_branch ? `the Project default checkout ${selected.default_checkout_branch}.` : 'the Project checkout after it is configured.'}</p><label>Starting branch <span className="muted">Optional override</span><input className="app-input" aria-label="Starting branch" value={checkoutBranch} onChange={(event) => setCheckoutBranch(event.target.value)} /></label></>}{repositoryInvalid && <p className="app-error" role="alert">This Project needs an explicit default checkout before a thread can start.</p>}{error && <p className="app-error" role="alert">{error}</p>}<div className="app-actions"><button className="app-button" onClick={onCancel}>Cancel</button><button className="app-button primary" disabled={repositoryInvalid} onClick={() => onStart({ project: project || null, ...(checkoutBranch.trim() ? { checkoutBranch: checkoutBranch.trim() } : {}) })}>Start thread</button></div></section>;
}

function ProjectDetails({ organization, csrf, project, threads, allProjects, agents, manager, error, selectedName, onOpenThread, onMoveThread, onNewThread, onSaveProject, onLifecycleProject, onWorkspaceOperation }: {
  organization: string;
  csrf: string;
  project?: Project;
  threads: Thread[];
  allProjects: Project[];
  agents: Agent[];
  manager: boolean;
  error: string;
  selectedName: string;
  onOpenThread: (agent: string, session: string) => void;
  onMoveThread: (thread: Thread, project: string | null) => Promise<void>;
  onNewThread: (selection: ProjectThreadStart) => void;
  onSaveProject: (draft: ProjectDraft, project?: Project) => Promise<void>;
  onLifecycleProject: (project: Project, action: 'archive' | 'restore' | 'delete') => Promise<void>;
  onWorkspaceOperation?: (update: WorkspaceUpdate) => void;
}) {
  const [search, setSearch] = useState('');
  const [employee, setEmployee] = useState('');
  const [creating, setCreating] = useState(false);
  const [tab, setTab] = useState<'threads' | 'workspaces'>('threads');
  const filtered = useMemo(() => threads.filter((thread) => thread.title.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()) && (!employee || thread.agent === employee)), [employee, search, threads]);
  const repositoryInvalid = Boolean(project?.target_repository_url && !project.default_checkout_branch);
  return <section className="project-details" aria-label={`${selectedName} project`}>
    <div className="project-details-heading"><div><h2>{selectedName}</h2>{project?.description && <p className="page-intro">{project.description}</p>}{project?.target_repository_url && <p className="muted">Repository: {project.target_repository_url} · default checkout {project.default_checkout_branch}</p>}</div>
      <div className="app-actions">{project && <ProjectEditor trigger="Edit Project" project={project} onSave={onSaveProject} />}{project && manager && (project.archived ? <button className="app-button" onClick={() => { void onLifecycleProject(project, 'restore'); }}>Restore Project</button> : <button className="app-button" onClick={() => { void onLifecycleProject(project, 'archive'); }}>Archive Project</button>)}</div></div>
    {project && manager && <button className="app-button danger" onClick={() => { if (window.confirm(`Delete ${project.name}? Its threads will remain Ungrouped.`)) void onLifecycleProject(project, 'delete'); }}>Delete Project</button>}
    <div className="project-detail-tabs" role="tablist" aria-label={`${selectedName} view`}><button className="app-button quiet" role="tab" aria-selected={tab === 'threads'} onClick={() => setTab('threads')}>Threads</button><button className="app-button quiet" role="tab" aria-selected={tab === 'workspaces'} onClick={() => setTab('workspaces')}>Workspaces</button></div>
    {tab === 'threads' && <><div className="project-detail-toolbar"><label className="project-search-label">Search threads<input type="search" className="app-input" aria-label={`Search ${selectedName} threads`} value={search} onChange={(event) => setSearch(event.target.value)} /></label><label>Employee<select className="app-select" aria-label="Filter by employee" value={employee} onChange={(event) => setEmployee(event.target.value)}><option value="">All employees</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label><button className="app-button primary" disabled={project?.archived || repositoryInvalid} onClick={() => setCreating(true)}>New thread</button></div>
    {project?.archived && <p className="app-notice">Restore this Project before starting a new thread.</p>}
    {repositoryInvalid && <p className="app-error" role="alert">This Project needs an explicit default checkout before a thread can start.</p>}
    {creating && <NewProjectThread project={project} agents={agents} onClose={() => setCreating(false)} onStart={(agent, checkoutBranch) => { setCreating(false); onNewThread({ agent, project: project?.id ?? null, ...(checkoutBranch ? { checkoutBranch } : {}) }); }} />}
    {error && <p className="app-error" role="alert">{error}</p>}
    <div className="project-thread-list">{filtered.map((thread) => <div className="project-thread-row" key={`${thread.agent}:${thread.session_id}`}><button className="project-thread-open" onClick={() => onOpenThread(thread.agent, thread.session_id)}><span title={thread.title}>{thread.title}</span>{' '}<small>{agents.find((agent) => agent.id === thread.agent)?.name ?? thread.agent}</small></button><select aria-label={`Project for ${thread.title}`} value={thread.project_id ?? ''} onChange={(event) => { void onMoveThread(thread, event.target.value || null); }}><option value="">Ungrouped</option>{(project?.archived ? [project, ...allProjects] : allProjects).map((item) => <option key={item.id} value={item.id} disabled={item.archived}>{item.name}{item.archived ? ' (archived)' : ''}</option>)}</select></div>)}{!filtered.length && <p className="muted">No matching threads.</p>}</div></>}
    {tab === 'workspaces' && <ProjectWorkspaces organization={organization} csrf={csrf} threads={threads} onOpenThread={onOpenThread} onOperation={onWorkspaceOperation} />}
  </section>;
}

function ProjectWorkspaces({ organization, csrf, threads, onOpenThread, onOperation }: { organization: string; csrf: string; threads: Thread[]; onOpenThread: (agent: string, session: string) => void; onOperation?: (update: WorkspaceUpdate) => void }) {
  const byEmployee = threads.reduce<Record<string, Thread[]>>((groups, thread) => ({ ...groups, [thread.agent]: [...(groups[thread.agent] ?? []), thread] }), {});
  return <div className="project-workspaces" role="tabpanel">{Object.entries(byEmployee).map(([agent, employeeThreads]) => <WorkspaceInspectionList key={agent} organization={organization} agent={agent} csrf={csrf} sessions={employeeThreads.map((thread) => ({ id: thread.session_id, title: thread.title }))} onOpen={(session) => onOpenThread(agent, session)} onOperation={onOperation} />)}</div>;
}

function NewProjectThread({ project, agents, onClose, onStart }: { project?: Project; agents: Agent[]; onClose: () => void; onStart: (agent: string, checkoutBranch?: string) => void }) {
  const [agent, setAgent] = useState(agents[0]?.id ?? '');
  const [checkoutBranch, setCheckoutBranch] = useState('');
  return <section className="app-panel project-thread-creator" aria-label="New thread"><h3>New thread</h3><label>Employee<select className="app-select" aria-label="Employee" value={agent} onChange={(event) => setAgent(event.target.value)}>{agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><p className="muted">{project ? `This thread will be grouped under ${project.name}.` : 'This thread will remain Ungrouped.'}</p>{project?.target_repository_url && <><p className="app-notice">This thread will prepare an independent workspace from the Project default checkout {project.default_checkout_branch}.</p><label>Starting branch <span className="muted">Optional override</span><input className="app-input" aria-label="Starting branch" value={checkoutBranch} onChange={(event) => setCheckoutBranch(event.target.value)} /></label></>}<div className="app-actions"><button className="app-button" onClick={onClose}>Cancel</button><button className="app-button primary" disabled={!agent} onClick={() => onStart(agent, checkoutBranch.trim() || undefined)}>Start thread</button></div></section>;
}

function ProjectEditor({ trigger, project, onSave }: { trigger: string; project?: Project; onSave: (draft: ProjectDraft, project?: Project) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => projectDraft(project));
  const [saveError, setSaveError] = useState('');
  const nameInput = useRef<HTMLInputElement>(null);
  const repositoryConfigured = Boolean(draft.target_repository_url?.trim());
  const creating = !project;
  return <Dialog open={open} onOpenChange={(next) => {
    setOpen(next);
    if (next) {
      setDraft(projectDraft(project));
      setSaveError('');
    }
  }}><DialogTrigger asChild><button className={creating ? 'app-button quiet project-create-trigger' : 'app-button'} aria-label={trigger} title={trigger}>{creating ? <PlusIcon aria-hidden="true" size={16} /> : trigger}</button></DialogTrigger>
    <DialogContent aria-label={trigger} className="project-editor-modal" onOpenAutoFocus={(event) => { event.preventDefault(); nameInput.current?.focus(); }}><DialogHeader><DialogTitle>{trigger}</DialogTitle></DialogHeader><form className="app-form" onSubmit={(event) => {
      event.preventDefault();
      setSaveError('');
      void onSave(draft, project).then(() => setOpen(false)).catch((cause: unknown) => setSaveError(errorMessage(cause)));
    }}><label>Name<input ref={nameInput} className="app-input" required maxLength={120} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label><label>Description<textarea className="app-textarea" value={draft.description ?? ''} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label><label>Target repository <span className="muted">Optional</span><input className="app-input" value={draft.target_repository_url ?? ''} onChange={(event) => setDraft({ ...draft, target_repository_url: event.target.value || null })} /></label><label>Default checkout <span className="muted">Required with a repository</span><input className="app-input" required={repositoryConfigured} value={draft.default_checkout_branch ?? ''} onChange={(event) => setDraft({ ...draft, default_checkout_branch: event.target.value || null })} /></label><p className="muted">Repository settings guide new workspaces only. Existing threads keep their current workspace.</p>{saveError && <p className="app-error" role="alert">{saveError}</p>}<div className="app-actions"><DialogClose asChild><button type="button" className="app-button">Cancel</button></DialogClose><button className="app-button primary">Save Project</button></div></form>
    </DialogContent></Dialog>;
}
