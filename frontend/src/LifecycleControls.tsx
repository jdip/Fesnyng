import { useEffect, useRef, useState } from 'react';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './components/ui/dialog';
import { agentPath, api, errorMessage } from './workspace-api';

type LifecycleAction = 'start' | 'stop' | 'restart' | 'rebuild';
type RuntimeState = {
  desired_state?: 'running' | 'stopped';
  lifecycle_state?: 'running' | 'stopped' | 'pending' | 'transitioning' | 'recovering' | 'recovery_required' | 'missing' | 'unreachable' | 'failed';
  retry_action?: 'start' | 'rebuild';
  confirmation_required?: boolean;
  confirmation_code?: string;
  message?: string;
  error?: string;
};

const actionLabel: Record<LifecycleAction, string> = {
  start: 'Start', stop: 'Stop', restart: 'Restart', rebuild: 'Rebuild',
};

function statusFor(runtime?: RuntimeState) {
  switch (runtime?.lifecycle_state) {
    case 'running': return ['Running', 'The agent container is running.'];
    case 'stopped': return ['Stopped', 'The agent was intentionally stopped. Start applies any pending configuration.'];
    case 'missing': return ['Missing', 'The agent container is missing. Rebuild creates a replacement after verifying retained resources.'];
    case 'pending':
    case 'transitioning': return ['Transitioning', 'A lifecycle change is in progress. New work remains paused until it settles.'];
    case 'recovering': return ['Recovering', 'The host is reconciling the agent and retained native history.'];
    case 'recovery_required': return ['Recovery required', 'The previous lifecycle operation could not establish a safe result. Use Thread activity to investigate or resolve recorded outcomes before retrying.'];
    case 'failed': return ['Lifecycle failed', 'The owning host reported a lifecycle failure. Use Thread activity to investigate recorded outcomes before retrying.'];
    case 'unreachable': return ['Host unreachable', 'The owning host cannot be reached. Refresh after it is available.'];
    default: return ['Runtime status unavailable', 'Refresh to request the agent state from its owning host.'];
  }
}

function actionsFor(runtime?: RuntimeState): LifecycleAction[] {
  if (runtime?.lifecycle_state === 'running') return ['stop', 'restart', 'rebuild'];
  if (runtime?.lifecycle_state === 'stopped') return ['start', 'rebuild'];
  if (runtime?.lifecycle_state === 'missing') return ['rebuild'];
  if (runtime && ['recovery_required', 'failed'].includes(runtime.lifecycle_state ?? '') && runtime.retry_action) return [runtime.retry_action];
  return [];
}

export function LifecycleControls({ organization, agent, agentName, csrf }: { organization: string; agent: string; agentName: string; csrf: string }) {
  const [runtime, setRuntime] = useState<RuntimeState>();
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [action, setAction] = useState<LifecycleAction>();
  const [challenge, setChallenge] = useState<Pick<RuntimeState, 'confirmation_code' | 'message'>>();
  const [code, setCode] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const opener = useRef<HTMLButtonElement | null>(null);
  const refreshButton = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api<RuntimeState>(`${agentPath(organization, agent)}/runtime`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) { setRuntime(result); setError(''); } })
      .catch((cause: unknown) => { if (!controller.signal.aborted) { setRuntime(undefined); setError(errorMessage(cause)); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [agent, organization, refresh]);

  useEffect(() => {
    if (!['pending', 'transitioning', 'recovering'].includes(runtime?.lifecycle_state ?? '')) return;
    const timer = window.setInterval(() => setRefresh((value) => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, [runtime?.lifecycle_state]);

  function resetConfirmation() {
    setAction(undefined);
    setChallenge(undefined);
    setCode('');
  }

  function restoreFocus() {
    window.setTimeout(() => {
      if (opener.current?.isConnected && !opener.current.disabled) opener.current.focus();
      else refreshButton.current?.focus();
    }, 0);
  }

  function closeConfirmation() {
    if (!sending) { resetConfirmation(); restoreFocus(); }
  }

  function execute(next: LifecycleAction, confirmationCode?: string) {
    setSending(true);
    setError('');
    void api<RuntimeState>(`${agentPath(organization, agent)}/lifecycle`, {
      method: 'POST', csrf, body: { action: next, confirmed: true, ...(confirmationCode ? { code: confirmationCode } : {}) },
    }).then((result) => {
      if (result.confirmation_required) {
        setChallenge({ confirmation_code: result.confirmation_code, message: result.message });
        return;
      }
      setRuntime(result);
      resetConfirmation();
      restoreFocus();
    }).catch((cause: unknown) => {
      setError(errorMessage(cause));
      if (!action) restoreFocus();
    }).finally(() => setSending(false));
  }

  const [status, description] = statusFor(runtime);
  const buttons = actionsFor(runtime);
  const title = action ? `${actionLabel[action]} ${agentName}` : '';
  const hostMessage = runtime?.message ?? runtime?.error;

  return <section className="app-panel app-form" aria-labelledby="agent-lifecycle-heading">
    <h3 id="agent-lifecycle-heading">Agent lifecycle</h3>
    <p><strong>{loading ? 'Loading runtime status…' : status}</strong></p>
    {!loading && <p className="muted">{description}</p>}
    {runtime?.desired_state && <p className="muted">Requested state: {runtime.desired_state}.</p>}
    {hostMessage && <p className="muted" role="status">{hostMessage}</p>}
    {!action && error && <p className="app-error" role="alert">{error}</p>}
    <div className="app-actions">
      <button ref={refreshButton} className="app-button" type="button" disabled={loading || sending} onClick={() => { setLoading(true); setRefresh((value) => value + 1); }}>Refresh</button>
      {buttons.map((next) => {
        const retry = runtime?.retry_action === next && ['recovery_required', 'failed'].includes(runtime.lifecycle_state ?? '');
        return <button key={next} className={`app-button${next === 'stop' || next === 'rebuild' ? ' danger' : ''}`} type="button" disabled={loading || sending} onClick={(event) => { opener.current = event.currentTarget; if (next === 'start') execute(next); else setAction(next); }}>{retry ? `Retry ${actionLabel[next].toLowerCase()}` : actionLabel[next]}</button>;
      })}
    </div>
    <Dialog open={!!action} onOpenChange={(open) => { if (!open) closeConfirmation(); }}>
      <DialogContent showCloseButton={!sending}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {challenge?.message ?? (action === 'rebuild'
              ? 'Rebuild removes the container writable layer. Agent identity, verified retained home and workspace volumes, conversation history, and host OAuth configuration are preserved.'
              : action === 'restart'
                ? 'Restart pauses new work while the host safely interrupts and restarts the existing container.'
                : 'Stop pauses new work while the host safely interrupts the existing container.')}
          </DialogDescription>
        </DialogHeader>
        {challenge?.confirmation_code && <label>Confirmation code<p className="muted">Type <strong>{challenge.confirmation_code}</strong> to confirm this active-work interruption.</p><input className="app-input" aria-label="Confirmation code" autoComplete="off" value={code} onChange={(event) => setCode(event.target.value)} /></label>}
        {error && <p className="app-error" role="alert">{error}</p>}
        <DialogFooter>
          <DialogClose asChild><button className="app-button" type="button" disabled={sending}>Cancel</button></DialogClose>
          <button className="app-button danger" type="button" disabled={sending || (!!challenge?.confirmation_code && !code)} onClick={() => action && execute(action, challenge?.confirmation_code ? code : undefined)}>{sending ? 'Working…' : `Confirm ${action ? actionLabel[action].toLowerCase() : ''}`}</button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>;
}
