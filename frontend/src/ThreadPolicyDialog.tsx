import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './components/ui/dialog';
import { RuleEditor } from './RuleEditor';
import { createFesnyngOpenCodeFetch } from './lib/opencode-client';
import { errorMessage, type Rule } from './workspace-api';

type ThreadPolicyRecord = { desired_revision: number; applied_revision: number; rules: Rule[]; effective_rules: Rule[] };
type PolicyLoad = { scope: string; policy?: ThreadPolicyRecord; error: string };

export function ThreadPolicyDialog({
  baseUrl,
  csrfToken,
  session,
  onClose,
  onRestoreFocus,
}: {
  baseUrl: string;
  csrfToken: string;
  session: { id: string; title: string };
  onClose: () => void;
  onRestoreFocus?: () => void;
}) {
  const [open, setOpen] = useState(true);
  const [busy, setBusy] = useState(false);
  const request = useMemo(() => createFesnyngOpenCodeFetch(csrfToken), [csrfToken]);
  const endpoint = useMemo(() => new URL(`../sessions/${encodeURIComponent(session.id)}/policy`, `${baseUrl.replace(/\/$/, '')}/`).href, [baseUrl, session.id]);
  const [loaded, setLoaded] = useState<PolicyLoad>({ scope: endpoint, error: '' });
  const current = loaded.scope === endpoint ? loaded : { scope: endpoint, error: '' };
  const titleRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void request(endpoint, { signal: controller.signal })
      .then((response) => response.json() as Promise<ThreadPolicyRecord>)
      .then((policy) => { if (!controller.signal.aborted) setLoaded({ scope: endpoint, policy, error: '' }); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ scope: endpoint, error: errorMessage(cause) }); });
    return () => controller.abort();
  }, [endpoint, request]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!current.policy || busy) return;
    setBusy(true);
    setLoaded((previous) => previous.scope === endpoint ? { ...previous, error: '' } : previous);
    try {
      const response = await request(endpoint, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: current.policy.desired_revision, rules: current.policy.rules }),
      });
      setLoaded({ scope: endpoint, policy: await response.json() as ThreadPolicyRecord, error: '' });
    } catch (cause) {
      setLoaded((previous) => previous.scope === endpoint ? { ...previous, error: errorMessage(cause) } : previous);
    } finally {
      setBusy(false);
    }
  };

  return <Dialog open={open} onOpenChange={(next) => { if (!next) setOpen(false); }}>
    <DialogContent aria-label="Thread permissions" className="max-h-[calc(100dvh-2rem)] overflow-y-auto" onOpenAutoFocus={(event) => { event.preventDefault(); titleRef.current?.focus(); }} onCloseAutoFocus={(event) => { event.preventDefault(); onRestoreFocus?.(); onClose(); }}>
      <DialogHeader><DialogTitle ref={titleRef} tabIndex={-1}>Thread permissions</DialogTitle><DialogDescription>Permissions for {session.title}.</DialogDescription></DialogHeader>
      {current.error && <p className="app-error" role="alert">{current.error}</p>}
      {!current.policy && !current.error && <p role="status">Loading thread permissions…</p>}
      {current.policy && <form className="app-form" onSubmit={(event) => { void save(event); }}><p className="muted">Organization mandatory rules always take precedence. Desired revision {current.policy.desired_revision} · Applied {current.policy.applied_revision}.</p><RuleEditor rules={current.policy.rules} onChange={(rules) => setLoaded((previous) => previous.scope === endpoint && previous.policy ? { ...previous, policy: { ...previous.policy, rules } } : previous)} /><details><summary>Effective permissions</summary><ul>{current.policy.effective_rules.map((rule, index) => <li key={index}>{rule.permission} · {rule.pattern} · {rule.action}</li>)}</ul></details><div><button className="app-button" disabled={busy}>Save thread permissions</button></div></form>}
    </DialogContent>
  </Dialog>;
}
