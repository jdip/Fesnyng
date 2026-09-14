import { AssistantRuntimeProvider, useAuiState, useExternalStoreRuntime, useRemoteThreadListRuntime, type RemoteThreadListAdapter } from '@assistant-ui/react';
import { createPortal } from 'react-dom';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Thread } from './components/assistant-ui/elements/thread.aui';
import { ThreadList } from './components/assistant-ui/elements/thread-list.aui';
import { applyCodexEvent, createFesnyngCodexFetch, projectCodexHistory, type CodexEvent, type CodexHistory } from './lib/codex-client';
import { errorMessage } from './workspace-api';
import { InlineComposer } from './InlineComposer';
import { ConversationDeliveryRecovery, ConversationMessageFooter } from './ConversationDelivery';
import { NativeEditToolFallback } from './components/assistant-ui/elements/native-edit-tool';

type Session = { id: string; title: string; time?: { updated?: number; archived?: number | null } };

export type CodexConversationProps = {
  baseUrl: string;
  csrfToken: string;
  sessionId?: string;
  onSessionChange?: (sessionId: string | undefined) => void;
  onError?: (error: unknown) => void | Promise<void>;
  showThreadList?: boolean;
  refreshKey?: number;
  threadListTarget?: HTMLElement | null;
  newThreadRequest?: number;
  onNewThreadStarted?: (request: number) => void;
  threadPageSize?: number;
  onThreadSelect?: () => void;
};

const base = (url: string) => url.replace(/\/$/, '');

const sessionRecord = (value: unknown): Session => {
  if (!value || typeof value !== 'object') throw new Error('Codex thread receipt is invalid.');
  const entry = value as Record<string, unknown>;
  if (typeof entry.id !== 'string' || typeof entry.title !== 'string') throw new Error('Codex thread receipt is invalid.');
  return entry as Session;
};

const history = (value: unknown): CodexHistory => {
  if (!value || typeof value !== 'object') throw new Error('Codex thread history is invalid.');
  const entry = value as Record<string, unknown>;
  if (!entry.thread || typeof entry.thread !== 'object' || !Array.isArray(entry.turns)) throw new Error('Codex thread history is invalid.');
  const historyState = entry.historyState;
  return {
    thread: entry.thread as Record<string, unknown>,
    turns: entry.turns.filter((turn): turn is Record<string, unknown> => Boolean(turn && typeof turn === 'object' && !Array.isArray(turn))),
    ...(historyState === 'complete' || historyState === 'unavailable' ? { historyState } : {}),
    ...(typeof entry.reason === 'string' ? { historyReason: entry.reason } : typeof entry.historyReason === 'string' ? { historyReason: entry.historyReason } : {}),
  };
};

const metadata = (session: Session) => ({
  status: typeof session.time?.archived === 'number' ? 'archived' as const : 'regular' as const, remoteId: session.id, externalId: session.id, title: session.title,
  ...(typeof session.time?.updated === 'number' ? { lastMessageAt: new Date(session.time.updated) } : {}),
});

/** The maintained remote-thread-list boundary over host-owned Codex threads. */
export function createCodexThreadListAdapter(baseUrl: string, csrfToken: string): RemoteThreadListAdapter {
  const request = createFesnyngCodexFetch(csrfToken);
  const read = async (path: string) => request(`${base(baseUrl)}${path}`).then((response) => response.json());
  return {
    async list() {
      const value: unknown = await read('/session');
      if (!Array.isArray(value)) throw new Error('Codex thread list is invalid.');
      return { threads: value.map(sessionRecord).map(metadata) };
    },
    async fetch(threadId) { return metadata(sessionRecord(await read(`/session/${encodeURIComponent(threadId)}`))); },
    async initialize() {
      const response = await request(`${base(baseUrl)}/session`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
      const created = sessionRecord(await response.json());
      return { remoteId: created.id, externalId: created.id };
    },
    async rename(threadId, title) {
      await request(`${base(baseUrl)}/session/${encodeURIComponent(threadId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) });
    },
    async archive(threadId) { await request(`${base(baseUrl)}/session/${encodeURIComponent(threadId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ time: { archived: Date.now() } }) }); },
    async unarchive(threadId) { await request(`${base(baseUrl)}/session/${encodeURIComponent(threadId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ time: { archived: null } }) }); },
    async delete() { throw new Error('Codex threads cannot be permanently deleted from this workspace.'); },
    async generateTitle() { return new ReadableStream({ start(controller) { controller.close(); } }); },
  };
}

function useCodexHistory(baseUrl: string, csrfToken: string, sessionId: string | undefined, refreshKey: number, onError: CodexConversationProps['onError'], onHistoryNotice?: (notice: string) => void) {
  const request = useMemo(() => createFesnyngCodexFetch(csrfToken), [csrfToken]);
  const [data, setData] = useState<CodexHistory>();
  const [loading, setLoading] = useState(Boolean(sessionId));
  const sequence = useRef(0);
  const activeSession = useRef(sessionId);
  useLayoutEffect(() => { activeSession.current = sessionId; sequence.current += 1; }, [sessionId]);
  const reload = useCallback(async () => {
    if (!sessionId) { setData(undefined); setLoading(false); return; }
    const current = ++sequence.current;
    setLoading(true);
    try {
      const response = await request(`${base(baseUrl)}/session/${encodeURIComponent(sessionId)}/history`);
      const next = history(await response.json());
      if (current === sequence.current && activeSession.current === sessionId) {
        setData(next);
        onHistoryNotice?.(next.historyState === 'unavailable' ? next.historyReason || 'Codex has not materialized history for this new thread yet.' : '');
      }
    } catch (cause) {
      if (current === sequence.current && activeSession.current === sessionId) await onError?.(cause);
    } finally {
      if (current === sequence.current && activeSession.current === sessionId) setLoading(false);
    }
  }, [baseUrl, onError, onHistoryNotice, request, sessionId]);
  useEffect(() => { void Promise.resolve().then(reload); }, [reload, refreshKey]);
  useEffect(() => {
    if (!sessionId || typeof EventSource === 'undefined') return;
    const source = new EventSource(`${base(baseUrl)}/event`);
    let connected = false;
    source.addEventListener('open', () => {
      if (connected) void reload();
      connected = true;
    });
    source.addEventListener('message', (raw) => {
      try {
        const event: unknown = JSON.parse((raw as MessageEvent<string>).data);
        if (!event || typeof event !== 'object') return;
        const value = event as { method?: unknown; params?: unknown };
        if (typeof value.method !== 'string' || !value.params || typeof value.params !== 'object') return;
        const notification: CodexEvent = { method: value.method, params: value.params as Record<string, unknown> };
        setData((current) => current ? applyCodexEvent(current, notification) : current);
        if (notification.method === 'turn/completed' || notification.method === 'thread/deleted' || notification.method === 'thread/archived') void reload();
      } catch { /* EventSource reconnect and completed-turn reconciliation recover malformed notices. */ }
    });
    return () => source.close();
  }, [baseUrl, reload, sessionId]);
  return { messages: data ? projectCodexHistory(data) : [], isRunning: Boolean(data?.turns.some((turn) => {
    const status = turn.status && typeof turn.status === 'object' ? (turn.status as Record<string, unknown>).type : turn.status;
    return status === 'inProgress' || status === 'running';
  })), loading, reload };
}

function useCodexThreadRuntime({ baseUrl, csrfToken, refreshKey, onError, onHistoryNotice }: Pick<CodexConversationProps, 'baseUrl' | 'csrfToken' | 'refreshKey' | 'onError'> & { onHistoryNotice?: (notice: string) => void }) {
  const sessionId = useAuiState((state) => state.threadListItem.externalId ?? state.threadListItem.remoteId);
  const state = useCodexHistory(baseUrl, csrfToken, sessionId, refreshKey ?? 0, onError, onHistoryNotice);
  const request = useMemo(() => createFesnyngCodexFetch(csrfToken), [csrfToken]);
  return useExternalStoreRuntime({
    messages: state.messages,
    isRunning: state.isRunning,
    isLoading: state.loading,
    onNew: async (message) => {
      if (!sessionId) throw new Error('Codex thread initialization did not complete before sending.');
      if (message.role !== 'user') return;
      const text = message.content.filter((part): part is { type: 'text'; text: string } => part.type === 'text').map((part) => part.text).join('');
      if (!text.trim()) return;
      await request(`${base(baseUrl)}/session/${encodeURIComponent(sessionId)}/prompt`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, mode: message.steer ? 'steering' : 'queued' }) });
    },
    onRefetchThread: state.reload,
    onCancel: async () => {
      if (!sessionId) return;
      await request(`${base(baseUrl)}/session/${encodeURIComponent(sessionId)}/abort`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
    },
  });
}

type PendingRequest = { id: string; method: string; params: Record<string, unknown> };
const pending = (value: unknown): PendingRequest[] => Array.isArray(value) ? value.filter((request): request is PendingRequest => Boolean(request && typeof request === 'object' && typeof (request as Record<string, unknown>).id === 'string' && typeof (request as Record<string, unknown>).method === 'string' && (request as Record<string, unknown>).params && typeof (request as Record<string, unknown>).params === 'object')) as PendingRequest[] : [];
const fieldOptions = (entry: Record<string, unknown>) => Array.isArray(entry.options) ? entry.options.filter((option): option is Record<string, unknown> => Boolean(option && typeof option === 'object' && !Array.isArray(option) && typeof (option as Record<string, unknown>).label === 'string')) : [];
const supportedElicitationFields = (value: unknown) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const schema = value as Record<string, unknown>;
  const properties = schema.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return undefined;
  const propertyMap = properties as Record<string, unknown>;
  const entries = Object.entries(propertyMap);
  if (!entries.every((entry) => Boolean(entry[1] && typeof entry[1] === 'object' && !Array.isArray(entry[1])))) return undefined;
  const requiredValues = schema.required;
  const required = new Set<string>(Array.isArray(requiredValues) ? requiredValues.filter((name): name is string => typeof name === 'string') : []);
  const fields = entries as Array<[string, Record<string, unknown>]>;
  const allowed = new Set(['type', 'title', 'description', 'enum', 'minimum', 'maximum', 'multipleOf']);
  if (!fields.every(([, field]) => (field.type === 'string' || field.type === 'number' || field.type === 'integer' || field.type === 'boolean' || (Array.isArray(field.enum) && field.enum.every((value) => typeof value === 'string'))) && Object.keys(field).every((key) => allowed.has(key)))) return undefined;
  if (![...required].every((name) => name in propertyMap)) return undefined;
  if (!fields.every(([, field]) => {
    if (field.type !== 'number' && field.type !== 'integer') return field.minimum === undefined && field.maximum === undefined && field.multipleOf === undefined;
    const minimum = field.minimum;
    const maximum = field.maximum;
    const multiple = field.multipleOf;
    if (![minimum, maximum, multiple].every((constraint) => constraint === undefined || (typeof constraint === 'number' && Number.isFinite(constraint)))) return false;
    if (typeof multiple === 'number' && multiple <= 0) return false;
    if (field.type === 'integer' && [minimum, maximum, multiple].some((constraint) => typeof constraint === 'number' && !Number.isInteger(constraint))) return false;
    if (typeof minimum === 'number' && typeof maximum === 'number' && minimum > maximum) return false;
    return !(typeof minimum === 'number' && typeof multiple === 'number' && !Number.isInteger(minimum / multiple));
  })) return undefined;
  return fields.map(([name, field]) => ({ name, field, required: required.has(name) }));
};
const elicitationValue = (value: string, field: Record<string, unknown>) => field.type === 'boolean' ? value === 'true' : field.type === 'number' || field.type === 'integer' ? Number(value) : value;

/** Native App Server approvals and questions remain host-owned server requests. */
function CodexPendingRequests({ baseUrl, csrfToken }: Pick<CodexConversationProps, 'baseUrl' | 'csrfToken'>) {
  const sessionId = useAuiState((state) => state.threadListItem.externalId ?? state.threadListItem.remoteId);
  const request = useMemo(() => createFesnyngCodexFetch(csrfToken), [csrfToken]);
  const [requests, setRequests] = useState<PendingRequest[]>([]);
  const [answer, setAnswer] = useState<Record<string, string>>({});
  const [reloadError, setReloadError] = useState('');
  const [replyError, setReplyError] = useState('');
  const [submitting, setSubmitting] = useState<string>();
  const activeSession = useRef(sessionId);
  useLayoutEffect(() => { activeSession.current = sessionId; }, [sessionId]);
  const reload = useCallback(async () => {
    if (!sessionId) { setRequests([]); return; }
    const response = await request(`${base(baseUrl)}/pending?sessionID=${encodeURIComponent(sessionId)}`);
    const next = pending(await response.json());
    if (activeSession.current === sessionId) {
      setRequests(next);
      setReloadError('');
    }
  }, [baseUrl, request, sessionId]);
  useEffect(() => {
    let disposed = false;
    const refresh = () => { void reload().catch((cause: unknown) => { if (!disposed) setReloadError(errorMessage(cause)); }); };
    queueMicrotask(refresh);
    const timer = window.setInterval(refresh, 2500);
    window.addEventListener('focus', refresh);
    return () => { disposed = true; window.clearInterval(timer); window.removeEventListener('focus', refresh); };
  }, [reload]);
  const reply = async (item: PendingRequest, response: Record<string, unknown>) => {
    if (!sessionId) return;
    setSubmitting(item.id); setReplyError('');
    try {
      await request(`${base(baseUrl)}/pending/${encodeURIComponent(item.id)}/reply?sessionID=${encodeURIComponent(sessionId)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ response }) });
    } catch (cause) {
      setReplyError(errorMessage(cause));
      setSubmitting(undefined);
      return;
    }
    try { await reload(); }
    catch (cause) { setReloadError(errorMessage(cause)); }
    setSubmitting(undefined);
  };
  const errors = [reloadError, replyError].filter(Boolean);
  if (requests.length === 0 && errors.length === 0) return null;
  return <section className="fesnyng-questions" aria-label="Agent questions">{requests.map((item) => {
    const question = typeof item.params.command === 'string' ? `Run command: ${item.params.command}` : typeof item.params.reason === 'string' ? item.params.reason : typeof item.params.question === 'string' ? item.params.question : typeof item.params.message === 'string' ? item.params.message : item.method;
    const approval = item.method === 'item/commandExecution/requestApproval' || item.method === 'item/fileChange/requestApproval';
    const questions = Array.isArray(item.params.questions) ? item.params.questions.filter((entry): entry is Record<string, unknown> => Boolean(entry && typeof entry === 'object' && !Array.isArray(entry))) : [];
    const busy = submitting === item.id;
    const elicitationFields = item.method === 'mcpServer/elicitation/request' ? supportedElicitationFields(item.params.requestedSchema) : undefined;
    if (item.method === 'item/permissions/requestApproval') return <article key={item.id} className="app-panel"><p>This Codex request requires a permission profile that Fesnyng cannot safely translate. Update the agent’s mandatory policy before continuing.</p></article>;
    return <article key={item.id} className="app-panel"><p>{question}</p>{approval ? <div className="app-actions"><button className="app-button primary" disabled={busy} onClick={() => void reply(item, { decision: 'accept' })}>Approve</button><button className="app-button" disabled={busy} onClick={() => void reply(item, { decision: 'decline' })}>Reject</button></div> : item.method === 'mcpServer/elicitation/request' ? elicitationFields ? <form className="app-form" onSubmit={(event) => { event.preventDefault(); void reply(item, { action: 'accept', content: Object.fromEntries(elicitationFields.flatMap(({ name, field, required }) => { const value = answer[`${item.id}:${name}`] ?? ''; return required || value ? [[name, elicitationValue(value, field)]] : []; })) }); }}>{elicitationFields.map(({ name, field, required }) => { const key = `${item.id}:${name}`; const options = Array.isArray(field.enum) ? field.enum.filter((value): value is string => typeof value === 'string') : []; return <label key={key}>{typeof field.title === 'string' ? field.title : name}{typeof field.description === 'string' && <small className="muted">{field.description}</small>}{field.type === 'boolean' ? <select aria-label={`Answer ${name}`} required={required} value={answer[key] ?? ''} onChange={(event) => setAnswer((current) => ({ ...current, [key]: event.target.value }))}><option value="" disabled>Select an option</option><option value="false">No</option><option value="true">Yes</option></select> : options.length ? <select aria-label={`Answer ${name}`} required={required} value={answer[key] ?? ''} onChange={(event) => setAnswer((current) => ({ ...current, [key]: event.target.value }))}><option value="" disabled>Select an option</option>{options.map((option) => <option key={option} value={option}>{option}</option>)}</select> : <input className="app-input" aria-label={`Answer ${name}`} required={required} type={field.type === 'number' || field.type === 'integer' ? 'number' : 'text'} min={typeof field.minimum === 'number' ? field.minimum : undefined} max={typeof field.maximum === 'number' ? field.maximum : undefined} step={typeof field.multipleOf === 'number' ? field.multipleOf : field.type === 'integer' ? 1 : field.type === 'number' ? 'any' : undefined} value={answer[key] ?? ''} onChange={(event) => setAnswer((current) => ({ ...current, [key]: event.target.value }))} />}</label>; })}<div className="app-actions"><button className="app-button primary" disabled={busy}>Continue</button><button type="button" className="app-button" disabled={busy} onClick={() => void reply(item, { action: 'decline' })}>Decline</button></div></form> : <div className="app-actions"><p className="muted">This MCP request uses an input form the workspace cannot safely render.</p><button className="app-button" disabled={busy} onClick={() => void reply(item, { action: 'decline' })}>Decline</button><button className="app-button" disabled={busy} onClick={() => void reply(item, { action: 'cancel' })}>Cancel</button></div> : <form className="app-form" onSubmit={(event) => { event.preventDefault(); const entries = questions.length ? questions : [{ id: item.id }]; void reply(item, { answers: Object.fromEntries(entries.map((entry) => { const id = typeof entry.id === 'string' ? entry.id : item.id; return [id, { answers: [answer[`${item.id}:${id}`] ?? ''] }]; })) }); }}><>{(questions.length ? questions : [{ id: item.id, question }]).map((entry, index) => { const id = typeof entry.id === 'string' ? entry.id : `${item.id}:${index}`; const label = typeof entry.question === 'string' ? entry.question : question; const key = `${item.id}:${id}`; const options = fieldOptions(entry); return <label key={key}>{label}{typeof entry.description === 'string' && <small className="muted">{entry.description}</small>}{options.length ? <select aria-label={`Answer ${label}`} required value={answer[key] ?? ''} onChange={(event) => setAnswer((current) => ({ ...current, [key]: event.target.value }))}><option value="" disabled>Select an option</option>{options.map((option) => <option key={option.label as string} value={option.label as string}>{option.label as string}{typeof option.description === 'string' ? ` — ${option.description}` : ''}</option>)}</select> : <textarea className="app-textarea" aria-label={`Answer ${label}`} required value={answer[key] ?? ''} onChange={(event) => setAnswer((current) => ({ ...current, [key]: event.target.value }))} />}</label>; })}</><button className="app-button primary" disabled={busy}>Answer</button></form>}</article>;
  })}{errors.map((message, index) => <p key={`${index}:${message}`} className="app-error" role="alert">{message}</p>)}</section>;
}

function CodexConversationView(props: CodexConversationProps) {
  const { newThreadRequest, onError, onNewThreadStarted } = props;
  const [historyNotice, setHistoryNotice] = useState('');
  const adapter = useMemo(() => createCodexThreadListAdapter(props.baseUrl, props.csrfToken), [props.baseUrl, props.csrfToken]);
  const runtimeHook = useCallback(function useCodexRemoteThreadRuntime() { return useCodexThreadRuntime({ baseUrl: props.baseUrl, csrfToken: props.csrfToken, refreshKey: props.refreshKey, onError: props.onError, onHistoryNotice: setHistoryNotice }); }, [props.baseUrl, props.csrfToken, props.onError, props.refreshKey]);
  const runtime = useRemoteThreadListRuntime({ adapter, threadId: props.sessionId, onThreadIdChange: props.onSessionChange, runtimeHook });
  const completed = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (newThreadRequest === undefined || completed.current === newThreadRequest) return;
    const request = newThreadRequest;
    void runtime.threads.switchToNewThread().then(() => { completed.current = request; onNewThreadStarted?.(request); }).catch((cause: unknown) => onError?.(cause));
  }, [newThreadRequest, onError, onNewThreadStarted, runtime]);
  const list = <ThreadList showNew={false} allowDelete={false} pageSize={props.threadPageSize} onSelect={props.onThreadSelect} />;
  return <AssistantRuntimeProvider runtime={runtime}><section className="fesnyng-conversation" aria-label="Agent conversation">{props.threadListTarget ? createPortal(list, props.threadListTarget) : props.showThreadList !== false && <aside>{list}</aside>}<div className="fesnyng-thread-pane">{historyNotice && <p className="app-notice" role="status">{historyNotice}</p>}<Thread allowAttachments={false} components={{ Composer: ({ autoFocus, allowAttachments }) => <InlineComposer autoFocus={autoFocus} allowAttachments={allowAttachments} baseUrl={props.baseUrl} csrfToken={props.csrfToken} sessionId={props.sessionId} runtime="codex" />, ToolFallback: NativeEditToolFallback, MessageFooter: ConversationMessageFooter, ThreadFooter: ConversationDeliveryRecovery }} /><CodexPendingRequests baseUrl={props.baseUrl} csrfToken={props.csrfToken} /></div></section></AssistantRuntimeProvider>;
}

/** Codex App Server view using maintained assistant-ui thread and list primitives. */
export function CodexConversation(props: CodexConversationProps) {
  return <CodexConversationView {...props} />;
}
