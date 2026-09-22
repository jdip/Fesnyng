import { useEffect, useState } from 'react';
import { agentPath, api, errorMessage } from './workspace-api';

type Grant = { enabled: boolean };

export function AgentManagementSettings({ organization, agent, csrf }: { organization: string; agent: string; csrf: string }) {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const path = `${agentPath(organization, agent)}/management`;
  useEffect(() => {
    const controller = new AbortController();
    void api<Grant>(path, { signal: controller.signal }).then((grant) => {
      if (!controller.signal.aborted) { setEnabled(grant.enabled); setLoaded(true); }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [path]);
  const save = () => {
    setBusy(true); setError(''); setNotice('');
    void api<Grant>(path, { method: 'PUT', csrf, body: { enabled } }).then((grant) => {
      setEnabled(grant.enabled); setNotice('Management access saved.');
    }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false));
  };
  return <section className="app-panel app-form" aria-label="Organization management access">
    <h3>Organization management</h3>
    <label className="checkbox-label"><input type="checkbox" checked={enabled} disabled={!loaded || busy} onChange={(event) => { setEnabled(event.target.checked); setNotice(''); }} />Allow organization management</label>
    <p className="muted">Allow this agent to create and edit agents, core instructions, settings, departments, and reporting relationships in this organization. Only human administrators can view or change this permission. Revoking access blocks subsequent management operations.</p>
    {error && <p className="app-error" role="alert">{error}</p>}
    {notice && <p className="app-notice" role="status">{notice}</p>}
    <div><button type="button" className="app-button" disabled={!loaded || busy} onClick={save}>{busy ? 'Saving…' : 'Save management access'}</button></div>
  </section>;
}
