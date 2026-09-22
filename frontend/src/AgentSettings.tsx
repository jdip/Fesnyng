import { useEffect, useState } from 'react';
import type { Department } from './DepartmentChart';
import { CodexModelSettings } from './CodexModelSettings';
import { LifecycleControls } from './LifecycleControls';
import { agentPath, api, errorMessage, type Agent, type Configuration, type Host, type Profile, type Skill } from './workspace-api';
const defaultConfiguration: Configuration = { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt-6-astra', profile_id: null, workspace: 'default', instructions: '', skills: [] };
type HarnessSwitchResponse = { agent: Agent; switch_state: 'pending_apply'; message: string; intent: { state: 'capturing' | 'frozen'; target_runtime_type: 'opencode' | 'codex' } };
type HarnessSwitchRuntime = { switch_state?: 'capturing' | 'frozen' | null; switch_target_runtime?: 'opencode' | 'codex' | null; error?: string | null };
type PendingHarnessSwitch = { switch_state: 'capturing' | 'frozen'; switch_target_runtime: 'opencode' | 'codex'; error?: string | null };
export function AgentSettings({ organization, agent, agents, csrf, onSaved }: { organization: string; agent?: Agent; agents: Agent[]; csrf: string; onSaved: (agent: Agent) => void }) {
  const [name, setName] = useState(agent?.name ?? '');
  const [title, setTitle] = useState(agent?.title ?? '');
  const [host, setHost] = useState(agent?.host_id ?? '');
  const [department, setDepartment] = useState(agent?.department_id ?? '');
  const [departments, setDepartments] = useState<Department[]>([]);
  const [reportsTo, setReportsTo] = useState(agent?.reports_to_agent_id ?? '');
  const [configuration, setConfiguration] = useState(agent?.configuration ?? defaultConfiguration);
  const [current, setCurrent] = useState(agent);
  const [targetRuntime, setTargetRuntime] = useState<'opencode' | 'codex'>(agent?.configuration.runtime_type ?? 'opencode');
  const [hosts, setHosts] = useState<Host[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<PendingHarnessSwitch>();
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([api<Host[]>(`/organizations/${organization}/hosts`, { signal: controller.signal }), api<Profile[]>(`/organizations/${organization}/profiles`, { signal: controller.signal }), api<Department[]>(`/organizations/${organization}/departments`, { signal: controller.signal })]).then(([availableHosts, availableProfiles, availableDepartments]) => { if (!controller.signal.aborted) { setHosts(availableHosts); setProfiles(availableProfiles); setDepartments(availableDepartments); } }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [organization]);
  useEffect(() => {
    if (!current) return;
    const controller = new AbortController();
    void api<HarnessSwitchRuntime>(`${agentPath(organization, current.id)}/runtime`, { signal: controller.signal }).then((runtime) => {
      if (!controller.signal.aborted) setPendingSwitch(runtime.switch_state && runtime.switch_target_runtime ? { switch_state: runtime.switch_state, switch_target_runtime: runtime.switch_target_runtime, error: runtime.error } : undefined);
    }).catch(() => { if (!controller.signal.aborted) setPendingSwitch(undefined); });
    return () => controller.abort();
  }, [current, organization]);
  const switchHarness = (runtime = targetRuntime) => {
    if (!current || runtime === (current.configuration.runtime_type ?? 'opencode')) return;
    setBusy(true); setError(''); setNotice('');
    void api<HarnessSwitchResponse>(`${agentPath(organization, current.id)}/harness-switch`, {
      method: 'POST', csrf, body: { expected_version: current.desired_version, target_runtime_type: runtime },
    }).then(({ agent: saved, message }) => {
      setCurrent(saved);
      setConfiguration(saved.configuration);
      setTargetRuntime(saved.configuration.runtime_type ?? 'opencode');
      onSaved(saved);
      setNotice(message);
    }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  };
  const applySelectedHarness = () => {
    if (!current) return;
    setBusy(true); setError('');
    void api(`${agentPath(organization, current.id)}/apply`, { method: 'POST', csrf }).then(() => api<Agent>(agentPath(organization, current.id))).then((saved) => {
      setCurrent(saved); setConfiguration(saved.configuration); setTargetRuntime(saved.configuration.runtime_type ?? 'opencode'); onSaved(saved); setNotice(`Configuration ${saved.configuration_status}.`);
    }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  };
  function changeSkill(index: number, update: Partial<Skill>) { setConfiguration({ ...configuration, skills: configuration.skills.map((skill, at) => at === index ? { ...skill, ...update } : skill) }); }
  return <div className="workspace-page"><h2>{current ? 'Agent settings' : 'Create an agent'}</h2><p className="page-intro">Identity and instructions persist across threads. Configuration applies when the agent reaches a safe boundary.</p><form className="app-form" onSubmit={(event) => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    const body = { name, title, department_id: department || null, reports_to_agent_id: reportsTo || null, configuration, ...(current ? { expected_version: current.desired_version } : { host_id: host }) };
    void api<Agent>(current ? agentPath(organization, current.id) : `/organizations/${organization}/agents`, { method: current ? 'PATCH' : 'POST', csrf, body }).then(async (saved) => {
      setCurrent(saved); setConfiguration(saved.configuration); setTargetRuntime(saved.configuration.runtime_type ?? 'opencode');
      try { await api(`${agentPath(organization, saved.id)}/apply`, { method: 'POST', csrf }); await api(`/organizations/${organization}/peers/apply`, { method: 'POST', csrf }); }
      catch (cause) { setError(`Settings saved; application is pending. ${errorMessage(cause)}`); onSaved(saved); return; }
      const refreshed = await api<Agent>(agentPath(organization, saved.id)); setCurrent(refreshed); setConfiguration(refreshed.configuration); setTargetRuntime(refreshed.configuration.runtime_type ?? 'opencode'); onSaved(refreshed); setNotice(refreshed.configuration_status === 'applied' ? 'Settings saved and applied.' : 'Settings saved. Waiting for a safe boundary.');
    }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  }}><div className="app-panel app-form"><div className="form-grid"><label>Agent name<input className="app-input" required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label><label>Role title<input className="app-input" maxLength={120} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Department<select className="app-select" value={department} onChange={(event) => setDepartment(event.target.value)}><option value="">No department</option>{departments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Reports to<select className="app-select" value={reportsTo} onChange={(event) => setReportsTo(event.target.value)}><option value="">No manager</option>{agents.filter((item) => item.id !== current?.id).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Home host<select className="app-select" required disabled={!!current} value={host} onChange={(event) => setHost(event.target.value)}><option value="">Choose a registered host</option>{hosts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div><label>Instructions<textarea className="app-textarea" rows={8} maxLength={200000} value={configuration.instructions} onChange={(event) => setConfiguration({ ...configuration, instructions: event.target.value })} /></label></div>
    <div className="app-panel app-form"><h3>Execution</h3><div className="form-grid">{!current ? <label>Harness<select className="app-select" value={configuration.runtime_type ?? 'opencode'} onChange={(event) => setConfiguration({ ...configuration, runtime_type: event.target.value as 'opencode' | 'codex' })}><option value="opencode">OpenCode</option><option value="codex">Codex</option></select></label> : <label>Harness<select aria-label="Harness" className="app-select" value={targetRuntime} disabled={busy} onChange={(event) => setTargetRuntime(event.target.value as 'opencode' | 'codex')}><option value="opencode">OpenCode</option><option value="codex">Codex</option></select><small className="muted">Switching permanently freezes existing threads and preserves their readable history.</small><button type="button" className="app-button" disabled={busy || targetRuntime === (current.configuration.runtime_type ?? 'opencode')} onClick={() => switchHarness()}>Switch to {targetRuntime === 'codex' ? 'Codex' : 'OpenCode'}</button></label>}<label>Credential profile<select className="app-select" value={configuration.profile_id ?? ''} onChange={(event) => setConfiguration({ ...configuration, profile_id: event.target.value || null })}><option value="">No profile assigned</option>{profiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Workspace<input className="app-input" required pattern="[a-z0-9][a-z0-9_-]{0,63}" value={configuration.workspace} onChange={(event) => setConfiguration({ ...configuration, workspace: event.target.value })} /></label>{configuration.runtime_type === 'codex' ? <CodexModelSettings organization={organization} host={host} configuration={configuration} onChange={setConfiguration} /> : <label>Model<input className="app-input" list="subscription-models" required maxLength={120} value={configuration.model} onChange={(event) => setConfiguration({ ...configuration, model: event.target.value })} /><datalist id="subscription-models"><option value="gpt-6-astra" /><option value="gpt-5.6-luna" /></datalist></label>}</div><p className="muted">OpenAI subscription · Docker execution. Model availability depends on the assigned account. Unsupported models return an error in the thread.</p>{current && <p className="muted">Desired version {current.desired_version} · Applied {current.applied_version ?? 'not yet'} · {current.configuration_status}</p>}</div>
    {current && pendingSwitch?.switch_state && pendingSwitch.switch_target_runtime && <HarnessSwitchRecovery currentRuntime={current.configuration.runtime_type ?? 'opencode'} pending={pendingSwitch} busy={busy} onRetry={() => switchHarness(pendingSwitch.switch_target_runtime!)} onApply={applySelectedHarness} />}
    {current && <LifecycleControls organization={organization} agent={current.id} agentName={current.name} csrf={csrf} />}
    <div className="app-panel app-form"><h3>Skills and explicit workflows</h3>{configuration.skills.map((skill, index) => <fieldset key={index}><legend>Skill {index + 1}</legend><div className="app-form"><label>Skill name<input className="app-input" required pattern="[a-z0-9][a-z0-9_-]{0,63}" value={skill.name} onChange={(event) => changeSkill(index, { name: event.target.value })} /></label><label>Skill instructions<textarea className="app-textarea" required maxLength={200000} value={skill.content} onChange={(event) => changeSkill(index, { content: event.target.value })} /></label><label className="checkbox-label"><input type="checkbox" checked={skill.explicit_only} onChange={(event) => changeSkill(index, { explicit_only: event.target.checked })} />Only run when explicitly invoked</label><div><button type="button" className="app-button danger" onClick={() => setConfiguration({ ...configuration, skills: configuration.skills.filter((_, at) => at !== index) })}>Remove skill</button></div></div></fieldset>)}<div><button type="button" className="app-button" disabled={configuration.skills.length >= 100} onClick={() => setConfiguration({ ...configuration, skills: [...configuration.skills, { name: '', content: '', explicit_only: false }] })}>Add skill</button></div></div>
    {error && <p className="app-error" role="alert">{error}</p>}{notice && <p className="app-notice" role="status">{notice}</p>}<div className="app-actions"><button className="app-button primary" disabled={busy}>{busy ? 'Saving…' : 'Save and apply'}</button>{current && <button className="app-button" type="button" disabled={busy} onClick={applySelectedHarness}>Retry application</button>}</div>
  </form></div>;
}

function HarnessSwitchRecovery({ currentRuntime, pending, busy, onRetry, onApply }: { currentRuntime: 'opencode' | 'codex'; pending: PendingHarnessSwitch; busy: boolean; onRetry: () => void; onApply: () => void }) {
  const targetSelected = currentRuntime === pending.switch_target_runtime;
  const needsRetry = pending.switch_state === 'capturing' || !targetSelected;
  const targetName = pending.switch_target_runtime === 'codex' ? 'Codex' : 'OpenCode';
  return <section className="app-panel app-form" aria-label="Harness switch recovery"><h3>Harness switch pending</h3><p className="muted">{needsRetry ? pending.switch_state === 'capturing' ? `Fesnyng is confirming the permanent freeze before switching to ${targetName}.` : `Historical threads are frozen. Retry the switch to record ${targetName} as the selected harness.` : `Historical threads are frozen. Apply ${targetName} when ready.`}</p>{pending.error && <p className="app-error" role="alert">{pending.error}</p>}<button type="button" className="app-button" disabled={busy} onClick={needsRetry ? onRetry : onApply}>{needsRetry ? 'Retry harness switch' : 'Apply selected harness'}</button></section>;
}
