import { useEffect, useState } from 'react';
import { agentPath, api, errorMessage, type Rule } from './workspace-api';
import { RuleEditor } from './RuleEditor';

export function ThreadContext({ organization, agent, session, csrf }: { organization: string; agent: string; session: string; csrf: string }) {
  const [panel, setPanel] = useState('');
  return <div className="thread-context"><button className="app-button quiet" aria-expanded={panel === 'policy'} onClick={() => setPanel(panel === 'policy' ? '' : 'policy')}>Thread permissions</button>{panel === 'policy' && <ThreadPolicy organization={organization} agent={agent} session={session} csrf={csrf} />}</div>;
}
type ThreadPolicyRecord = { desired_revision: number; applied_revision: number; rules: Rule[]; effective_rules: Rule[] };
function ThreadPolicy({ organization, agent, session, csrf }: { organization: string; agent: string; session: string; csrf: string }) {
  const [policy, setPolicy] = useState<ThreadPolicyRecord>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const path = `${agentPath(organization, agent)}/sessions/${encodeURIComponent(session)}/policy`;
  useEffect(() => { const controller = new AbortController(); void api<ThreadPolicyRecord>(path, { signal: controller.signal }).then(setPolicy).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); }); return () => controller.abort(); }, [path]);
  return <div className="app-panel" style={{ maxHeight: 360, overflow: 'auto' }}>{error && <p className="app-error" role="alert">{error}</p>}{policy && <form className="app-form" onSubmit={(event) => { event.preventDefault(); setBusy(true); setError(''); void api<ThreadPolicyRecord>(path, { method: 'PUT', csrf, body: { expected_revision: policy.desired_revision, rules: policy.rules } }).then(setPolicy).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false)); }}><p className="muted">Organization mandatory rules always take precedence. Desired revision {policy.desired_revision} · Applied {policy.applied_revision}.</p><RuleEditor rules={policy.rules} onChange={(rules) => setPolicy({ ...policy, rules })} /><details><summary>Effective permissions</summary><ul>{policy.effective_rules.map((rule, index) => <li key={index}>{rule.permission} · {rule.pattern} · {rule.action}</li>)}</ul></details><div><button className="app-button" disabled={busy}>Save thread permissions</button></div></form>}</div>;
}
