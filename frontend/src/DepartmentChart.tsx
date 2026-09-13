import { useEffect, useId, useRef, useState } from 'react';
import { PencilIcon, PlusIcon } from 'lucide-react';
import { api, errorMessage, type Agent } from './workspace-api';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './components/ui/dialog';
import './department-chart.css';

export type Department = { id: string; organization_id: string; name: string; parent_id: string | null; head_agent_id: string | null };
type Props = { organization: string; agents: Agent[]; manager: boolean; csrf: string; onSelect: (agent: Agent) => void };

export function DepartmentChart({ organization, agents, manager, csrf, onSelect }: Props) {
  const [departments, setDepartments] = useState<Department[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<Department | 'new' | null>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const editTrigger = useRef<HTMLButtonElement | null>(null);
  const addTrigger = useRef<HTMLButtonElement>(null);
  const [lines, setLines] = useState<{ id: string; path: string }[]>([]);
  const marker = useId().replaceAll(':', '');
  useEffect(() => {
    const controller = new AbortController();
    void api<Department[]>(`/organizations/${organization}/departments`, { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) { setDepartments(items); setLoaded(true); }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organization]);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const measure = () => {
      const origin = element.getBoundingClientRect();
      const nodes = new Map(Array.from(element.querySelectorAll<HTMLElement>('[data-chart-agent]')).map((node) => [node.dataset.chartAgent, node.getBoundingClientRect()]));
      setLines(agents.flatMap((agent) => {
        const start = nodes.get(agent.reports_to_agent_id ?? '');
        const end = nodes.get(agent.id);
        if (!start || !end) return [];
        const x1 = start.left + start.width / 2 - origin.left;
        const y1 = start.bottom - origin.top;
        const x2 = end.left + end.width / 2 - origin.left;
        const y2 = end.top - origin.top;
        const bend = Math.max(24, Math.abs(y2 - y1) / 2);
        return [{ id: agent.id, path: `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}` }];
      }));
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(element);
    measure();
    window.addEventListener('resize', measure);
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure); };
  }, [agents, departments, loaded]);
  function agentTree(agent: Agent, members: Agent[], seen: Set<string>) {
    if (seen.has(agent.id)) return null;
    const next = new Set([...seen, agent.id]);
    const reports = members.filter((item) => item.reports_to_agent_id === agent.id);
    const headOf = departments.filter((item) => item.head_agent_id === agent.id);
    return <div className="org-agent-branch" key={agent.id}>
      <button className="org-agent-card" data-chart-agent={agent.id} onClick={() => onSelect(agent)}>
        <span className="org-agent-avatar" aria-hidden="true">{agent.name.slice(0, 2).toUpperCase()}</span>
        <span><strong>{agent.name}</strong>{' '}<small>{agent.title || 'Agent'}</small>{' '}{headOf.map((department) => <small className="org-head-label" key={department.id}>Head of {department.name}</small>)}
          {agent.reports_to_agent_id && <span className="sr-only">Reports to {agents.find((item) => item.id === agent.reports_to_agent_id)?.name ?? 'another agent'}</span>}
        </span>
      </button>
      {reports.length > 0 && <div className="org-chart-row">{reports.map((item) => agentTree(item, members, next))}</div>}
    </div>;
  }
  function membersFor(department: string | null) {
    const members = agents.filter((agent) => (agent.department_id ?? null) === department);
    return <div className="org-chart-row">{members.filter((agent) => !members.some((item) => item.id === agent.reports_to_agent_id)).map((agent) => agentTree(agent, members, new Set()))}</div>;
  }
  function group(department: Department, seen: Set<string>) {
    if (seen.has(department.id)) return null;
    const children = departments.filter((item) => item.parent_id === department.id);
    return <section className="org-department" key={department.id} aria-label={`${department.name} department`}>
      <div className="org-department-heading"><h3>{department.name}</h3>{manager && <button className="sidebar-icon-button" aria-label={`Edit ${department.name} department`} title="Edit department" onClick={(event) => { editTrigger.current = event.currentTarget; setEditing(department); }}><PencilIcon size={14} /></button>}</div>
      {membersFor(department.id)}
      {!agents.some((agent) => agent.department_id === department.id) && children.length === 0 && <p className="org-empty-department">No agents assigned</p>}
      {children.length > 0 && <div className="org-chart-row org-department-children">{children.map((item) => group(item, new Set([...seen, department.id])))}</div>}
    </section>;
  }
  return <div className="department-chart">
    <div className="org-chart-toolbar"><p>Departments organize your team. Lines show who each agent reports to.</p>{manager && <button className="app-button" ref={addTrigger} onClick={(event) => { editTrigger.current = event.currentTarget; setEditing('new'); }}><PlusIcon size={16} />Add department</button>}</div>
    {error && <p className="app-error" role="alert">{error}</p>}
    {!loaded && !error && <p role="status">Loading organization chart…</p>}
    {loaded && <div className="org-chart-viewport" role="region" aria-label="Organization chart" tabIndex={0}><div className="org-chart-canvas" ref={canvas}>
      <svg className="org-reporting-lines" aria-hidden="true"><defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="currentColor" /></marker></defs>{lines.map((line) => <path key={line.id} d={line.path} markerEnd={`url(#${marker})`} />)}</svg>
      {agents.some((agent) => !agent.department_id) && <section className="org-unassigned" aria-label="Agents without a department">{departments.length > 0 && <h3>Without a department</h3>}{membersFor(null)}</section>}
      <div className="org-chart-row">{departments.filter((item) => !item.parent_id).map((item) => group(item, new Set()))}</div>
      {agents.length === 0 && departments.length === 0 && <p className="app-empty">Create an agent or department to start your organization chart.</p>}
    </div></div>}
    <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) setEditing(null); }}><DialogContent className="department-editor" onCloseAutoFocus={(event) => { event.preventDefault(); (editTrigger.current?.isConnected ? editTrigger.current : addTrigger.current)?.focus(); }}>
      <DialogHeader><DialogTitle>{editing === 'new' ? 'Add department' : 'Edit department'}</DialogTitle><DialogDescription>Organize agents without changing their reporting lines or permissions.</DialogDescription></DialogHeader>
      {editing && <DepartmentForm key={typeof editing === 'string' ? editing : editing.id} department={editing === 'new' ? undefined : editing} departments={departments} agents={agents} organization={organization} csrf={csrf} onSaved={(saved, deleted) => { setDepartments((items) => deleted ? items.filter((item) => item.id !== deleted) : saved ? [...items.filter((item) => item.id !== saved.id), saved] : items); setEditing(null); }} />}
    </DialogContent></Dialog>
  </div>;
}

function DepartmentForm({ department, departments, agents, organization, csrf, onSaved }: { department?: Department; departments: Department[]; agents: Agent[]; organization: string; csrf: string; onSaved: (saved?: Department, deleted?: string) => void }) {
  const [name, setName] = useState(department?.name ?? '');
  const [parent, setParent] = useState(department?.parent_id ?? '');
  const [head, setHead] = useState(department?.head_agent_id ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const base = `/organizations/${organization}/departments`;
  const descendants = new Set<string>(department ? [department.id] : []);
  for (let index = 0; index < departments.length; index++) for (const item of departments) if (item.parent_id && descendants.has(item.parent_id)) descendants.add(item.id);
  return <form className="app-form" onSubmit={(event) => {
    event.preventDefault(); setBusy(true); setError('');
    void api<Department>(department ? `${base}/${department.id}` : base, { method: department ? 'PATCH' : 'POST', csrf, body: { name, parent_id: parent || null, head_agent_id: head || null } }).then((saved) => onSaved(saved)).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  }}>
    <label>Department name<input className="app-input" required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
    <label>Parent department<select className="app-select" value={parent} onChange={(event) => setParent(event.target.value)}><option value="">Top level</option>{departments.filter((item) => !descendants.has(item.id)).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    <label>Department head<select className="app-select" value={head} onChange={(event) => setHead(event.target.value)}><option value="">No head assigned</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
    {error && <p className="app-error" role="alert">{error}</p>}
    <div className="app-actions"><button className="app-button primary" disabled={busy}>{busy ? 'Saving…' : department ? 'Save department' : 'Create department'}</button>{department && <button type="button" className="app-button danger" disabled={busy} onClick={() => setDeleting(true)}>Delete department</button>}</div>
    {department && deleting && <div className="app-panel"><p>Delete {department.name}? Move its agents and child departments first.</p><div className="app-actions"><button type="button" className="app-button danger" disabled={busy} onClick={() => { setBusy(true); setError(''); void api(`${base}/${department.id}`, { method: 'DELETE', csrf }).then(() => onSaved(undefined, department.id)).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false)); }}>Confirm deletion</button><button type="button" className="app-button" onClick={() => setDeleting(false)}>Cancel</button></div></div>}
  </form>;
}
