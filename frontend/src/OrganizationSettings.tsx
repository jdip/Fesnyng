import { useEffect, useState } from 'react';
import { agentPath, api, errorMessage, type Agent, type Host, type Member, type Organization, type Policy, type Profile } from './workspace-api';
import { RuleEditor } from './RuleEditor';
import { OrganizationIconSettings } from './OrganizationIconSettings';
export function OrganizationSettings({ organization, csrf, agents, onChanged, onIdentityChanged }: { organization: string; csrf: string; agents: Agent[]; onChanged: () => void; onIdentityChanged?: (updated: Organization) => void }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [hosts, setHosts] = useState<Host[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [policy, setPolicy] = useState<Policy>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const base = `/organizations/${organization}`;
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([api<Member[]>(`${base}/members`, { signal: controller.signal }), api<Host[]>(`${base}/hosts`, { signal: controller.signal }), api<Profile[]>(`${base}/profiles`, { signal: controller.signal }), api<Policy>(`${base}/policy`, { signal: controller.signal })]).then(([people, places, credentials, limits]) => { if (!controller.signal.aborted) { setMembers(people); setHosts(places); setProfiles(credentials); setPolicy(limits); } }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [base, revision]);
  async function run(action: () => Promise<void>) { setError(''); setNotice(''); setBusy(true); try { await action(); } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); } }
  return <div className="workspace-page"><h2>Organization settings</h2><p className="page-intro">Manage the people, credentials, and execution defaults shared by this organization.</p>
    {error && <p className="app-error" role="alert">{error}</p>}{notice && <p className="app-notice" role="status">{notice}</p>}
    <OrganizationIconSettings organization={organization} csrf={csrf} onSaved={(updated) => { onIdentityChanged?.(updated); onChanged(); }} />
    <section className="app-panel"><h3>People</h3><table className="app-table"><thead><tr><th>Name</th><th>Login</th><th>Role</th><th /></tr></thead><tbody>{members.map((member) => <tr key={member.user_id}><td>{member.display_name}</td><td>{member.login}</td><td>{member.role}</td><td>{member.role !== 'owner' && <button className="app-button danger" disabled={busy} onClick={() => { void run(async () => { await api(`${base}/members/${member.user_id}`, { method: 'DELETE', csrf }); setRevision((value) => value + 1); onChanged(); setNotice('Membership removed.'); }); }}>Remove</button>}</td></tr>)}</tbody></table><form className="app-form" style={{ marginTop: 20 }} onSubmit={(event) => {
      event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
      void run(async () => { await api(`${base}/members`, { method: 'PUT', csrf, body: { login: values.get('login'), display_name: values.get('display_name'), password: values.get('password') || null, role: values.get('role') } }); form.reset(); setRevision((value) => value + 1); onChanged(); setNotice('Member added.'); });
    }}><div className="form-grid"><label>Member login<input className="app-input" name="login" required maxLength={64} autoComplete="off" /></label><label>Display name<input className="app-input" name="display_name" required maxLength={100} /></label><label>Initial password<input className="app-input" name="password" type="password" autoComplete="new-password" maxLength={256} /></label><label>Role<select className="app-select" name="role" defaultValue="member"><option value="member">Member</option><option value="admin">Admin</option><option value="owner">Owner</option></select></label></div><p className="muted">For an existing login, leave the password blank. New users need an initial password.</p><div><button className="app-button" disabled={busy}>Add member</button></div></form></section>
    {policy && <section className="app-panel"><h3>Permissions</h3><form className="app-form" onSubmit={(event) => { event.preventDefault(); void run(async () => {
      const saved = await api<Policy>(`${base}/policy`, { method: 'PUT', csrf, body: { expected_version: policy.desired_version, configuration: policy.configuration } }); setPolicy(saved);
      const results = await Promise.allSettled(agents.map((agent) => api(`${agentPath(organization, agent.id)}/apply`, { method: 'POST', csrf })));
      onChanged(); const pending = results.filter((result) => result.status === 'rejected').length;
      setNotice(pending ? `Policy saved. ${pending} agent applications need retry.` : 'Policy saved and sent to every agent. Busy agents apply at a safe boundary.');
    }); }}><label>Default permission<select className="app-select" value={policy.configuration.default_permission} onChange={(event) => setPolicy({ ...policy, configuration: { ...policy.configuration, default_permission: event.target.value as Policy['configuration']['default_permission'] } })}><option value="allow">Full access inside the agent container</option><option value="ask">Ask for permission</option><option value="deny">Deny by default</option></select></label><label className="checkbox-label"><input type="checkbox" checked={policy.configuration.allow_thread_overrides} onChange={(event) => setPolicy({ ...policy, configuration: { ...policy.configuration, allow_thread_overrides: event.target.checked } })} />Allow thread-specific settings</label><p className="muted">Mandatory rules take precedence over thread settings.</p><RuleEditor rules={policy.configuration.mandatory_permissions} onChange={(rules) => setPolicy({ ...policy, configuration: { ...policy.configuration, mandatory_permissions: rules } })} /><div><button className="app-button primary" disabled={busy}>Save organization policy</button></div></form></section>}
    <section className="app-panel"><h3>Credential profiles</h3><p className="muted">Each host signs in and refreshes independently. Agents assigned to a profile share its host-local authorization.</p>{profiles.map((profile) => <div key={profile.id}><h4>{profile.name}</h4>{hosts.map((host) => <ProfileConnection key={host.id} base={`${base}/hosts/${host.id}/profiles/${profile.id}`} host={host.name} csrf={csrf} />)}</div>)}<form className="app-form" onSubmit={(event) => { event.preventDefault(); const form = event.currentTarget; const name = new FormData(form).get('name'); void run(async () => { await api(`${base}/profiles`, { method: 'POST', csrf, body: { name, provider: 'openai' } }); form.reset(); setRevision((value) => value + 1); setNotice('Credential profile created. Sign in separately on each assigned host.'); }); }}><label>New profile name<input className="app-input" name="name" required maxLength={120} /></label><div><button className="app-button" disabled={busy}>Create profile</button></div></form></section>
    <section className="app-panel"><h3>Registered hosts</h3><p className="muted">Host installation and networking are managed by operations.</p>{hosts.map((host) => <p key={host.id}>{host.name}</p>)}<button className="app-button" disabled={busy} onClick={() => { void run(async () => { await api(`${base}/peers/apply`, { method: 'POST', csrf }); setNotice('Collaboration configuration sent to the hosts.'); }); }}>Apply collaboration configuration</button></section>
  </div>;
}
function ProfileConnection({ base, host, csrf }: { base: string; host: string; csrf: string }) {
  const [state, setState] = useState('Checking…');
  const [login, setLogin] = useState<{ verification_uri: string; user_code: string; expires_at: number }>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ state: string }>(base, { signal: controller.signal }).then((result) => { if (!controller.signal.aborted) { setState(result.state); if (result.state === 'ready') setLogin(undefined); } }).catch((cause: unknown) => { if (!controller.signal.aborted) { setState('Not connected'); setError(errorMessage(cause)); } });
    return () => controller.abort();
  }, [base, revision]);
  useEffect(() => { if (!login) return; const timer = window.setInterval(() => { if (Date.now() / 1000 >= login.expires_at) { setLogin(undefined); setState('Login expired'); } else setRevision((value) => value + 1); }, 5000); return () => window.clearInterval(timer); }, [login]);
  return <div className="app-panel"><div className="app-actions"><strong>{host}</strong><span className="app-badge">{state.replaceAll('_', ' ')}</span><button className="app-button" disabled={busy || !!login} onClick={() => { setBusy(true); setError(''); void api<{ verification_uri: string; user_code: string; expires_at: number }>(`${base}/login`, { method: 'POST', csrf }).then(setLogin).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false)); }}>Sign in on this host</button><button className="app-button quiet" onClick={() => { setError(''); setRevision((value) => value + 1); }}>Refresh status</button></div>{login && <p>Open <a href={login.verification_uri.startsWith('https://auth.openai.com/') ? login.verification_uri : undefined} target="_blank" rel="noreferrer">OpenAI sign in</a> and enter <strong>{login.user_code}</strong>.</p>}{error && <p className="app-error" role="alert">{error}</p>}</div>;
}
