import { useState } from 'react';
import { agentPath, api, errorMessage } from './workspace-api';
export function ThreadArtifact({ organization, agent, session }: { organization: string; agent: string; session: string }) {
  const [content, setContent] = useState<string>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="app-panel"><form className="app-form" onSubmit={(event) => { event.preventDefault(); const path = String(new FormData(event.currentTarget).get('path')); setBusy(true); setContent(undefined); setError(''); const params = new URLSearchParams({ sessionID: session, path }); void api<{ type: string; content: string; encoding?: string }>(`${agentPath(organization, agent)}/opencode/file/content?${params}`).then((file) => { if (file.type !== 'text' || file.encoding === 'base64' || typeof file.content !== 'string') throw new Error('This file does not have a text preview.'); setContent(file.content); }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setBusy(false)); }}><label>Workspace file<input className="app-input" name="path" required placeholder="Relative path, such as src/app.py" /></label><div><button className="app-button" disabled={busy}>Read file</button></div></form>{error && <p className="app-error" role="alert">{error}</p>}{content !== undefined && <pre style={{ maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap', marginTop: 16 }}>{content}</pre>}</div>;
}
