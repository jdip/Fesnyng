import { useAuiState } from '@assistant-ui/react';
import { createContext, useContext, useMemo, useState, type FormEvent, type PropsWithChildren } from 'react';
import { ThreadReadReceipt } from './ThreadReadReceipt';
import { resultIdentity, useThreadNotifications, type Delivery, type ThreadNotifications } from './ThreadNotifications';
import { ViewedContent } from './ViewedContent';
import { agentPath, api, errorMessage } from './workspace-api';

type DeliveryConfiguration = { organization: string; agent: string; session?: string; csrf: string; onOpen: (agent: string, session: string) => void; readOnly: boolean };
type NativeMessage = { role?: string; session?: string; ids: Set<string>; clientIds: Set<string>; turnId?: string };

const ConversationDeliveryContext = createContext<DeliveryConfiguration | undefined>(undefined);
const actionableStates = new Set(['unresolved', 'uncertain', 'submitting', 'stopping']);
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object';

/** Preserves native message identities even when assistant-ui combines message parts. */
export function nativeMessage(message: unknown): NativeMessage {
  const ids = new Set<string>();
  const clientIds = new Set<string>();
  if (!record(message)) return { ids, clientIds };
  if (typeof message.id === 'string') ids.add(message.id);
  const metadata = record(message.metadata) ? message.metadata : undefined;
  const custom = metadata && record(metadata.custom) ? metadata.custom : undefined;
  const opencode = custom && record(custom.opencode) ? custom.opencode : undefined;
  const codex = custom && record(custom.codex) ? custom.codex : undefined;
  const original = opencode && record(opencode.originalMessage) ? opencode.originalMessage : undefined;
  if (typeof original?.id === 'string') ids.add(original.id);
  if (Array.isArray(opencode?.parts)) for (const part of opencode.parts) if (record(part) && typeof part.messageID === 'string') ids.add(part.messageID);
  const codexItem = codex && record(codex.item) ? codex.item : undefined;
  if (typeof codexItem?.clientId === 'string') clientIds.add(codexItem.clientId);
  if (typeof codexItem?.id === 'string') ids.add(codexItem.id);
  return { role: typeof message.role === 'string' ? message.role : undefined, session: typeof original?.sessionID === 'string' ? original.sessionID : typeof codex?.sessionId === 'string' ? codex.sessionId : undefined, ids, clientIds, turnId: typeof codex?.turnId === 'string' ? codex.turnId : undefined };
}

export function ConversationDeliveryProvider({ organization, agent, session, csrf, onOpen, readOnly = false, children }: PropsWithChildren<{ organization: string; agent: string; session?: string; csrf: string; onOpen: (agent: string, session: string) => void; readOnly?: boolean }>) {
  const value = useMemo<DeliveryConfiguration>(() => ({ organization, agent, session, csrf, onOpen, readOnly }), [agent, csrf, onOpen, organization, readOnly, session]);
  return <ConversationDeliveryContext.Provider value={value}>{children}</ConversationDeliveryContext.Provider>;
}

export function useConversationDelivery() {
  return useContext(ConversationDeliveryContext);
}

export function matchingDeliveries(deliveries: Delivery[], message: NativeMessage) {
  if (message.role === 'user') return deliveries.filter((delivery) => Boolean(
    (delivery.native_message_id && message.ids.has(delivery.native_message_id))
    || message.clientIds.has(delivery.id),
  ));
  if (message.role === 'assistant') return deliveries.filter((delivery) => Boolean(
    (delivery.outcome?.message_id && message.ids.has(delivery.outcome.message_id))
    || (delivery.outcome?.turn_id && message.turnId === delivery.outcome.turn_id),
  ));
  return [];
}

function resolutionEvidence(delivery: Delivery) {
  return delivery.outcome?.kind === 'operator_resolution' && (delivery.outcome.outcome === 'completed' || delivery.outcome.outcome === 'failed') && delivery.outcome.evidence
    ? delivery.outcome : undefined;
}

function unreadResolution(notifications: ThreadNotifications, agent: string, session: string, delivery: Delivery) {
  return Boolean(resolutionEvidence(delivery) && resultIdentity(delivery) && !notifications.isAcknowledged(agent, session, delivery, 'read'));
}

function unhandledFailure(notifications: ThreadNotifications, agent: string, session: string, delivery: Delivery) {
  return delivery.state === 'failed' && delivery.outcome?.kind !== 'operator_resolution' && !notifications.isAcknowledged(agent, session, delivery, 'failure_handled');
}

/** Renders delivery context beside the authored native input or result. */
export function ConversationDeliveryFooter() {
  const configuration = useConversationDelivery();
  const notifications = useThreadNotifications();
  const message = useAuiState((state) => state.message);
  const loading = useAuiState((state) => state.thread.isLoading);
  const native = nativeMessage(message);
  const deliveries = configuration && notifications && !loading && native.session && (!configuration.session || configuration.session === native.session)
    ? matchingDeliveries(notifications.receipts[configuration.agent]?.[native.session] ?? [], native) : [];
  if (!configuration || !notifications || !native.session || !deliveries.length) return null;
  return <>{deliveries.map((delivery) => <DeliveryContext key={delivery.id} delivery={delivery} configuration={configuration} notifications={notifications} session={native.session!} role={native.role} />)}</>;
}

/** Combines the existing completed-result read receipt with contextual delivery UI. */
export function ConversationMessageFooter() {
  return <><ThreadReadReceipt /><ConversationDeliveryFooter /></>;
}

function DeliveryContext({ delivery, configuration, notifications, session, role, excerpt = false }: { delivery: Delivery; configuration: DeliveryConfiguration; notifications: ThreadNotifications; session: string; role?: string; excerpt?: boolean }) {
  const resolution = resolutionEvidence(delivery);
  const failure = delivery.state === 'failed' && delivery.outcome?.kind !== 'operator_resolution';
  const handled = failure && notifications.isAcknowledged(configuration.agent, session, delivery, 'failure_handled');
  return <article className="delivery-context text-sm" aria-label="Delivery context">
    {role === 'assistant' ? <p className="muted">In response to <strong>{delivery.author.name}</strong></p> : <p className="muted"><strong>{delivery.author.name}</strong> {delivery.author.kind === 'agent' ? 'delivered this input' : 'authored this input'}</p>}
    {excerpt && <p>{delivery.payload.text.slice(0, 220)}{delivery.payload.text.length > 220 ? '…' : ''}</p>}
    {delivery.author.kind === 'agent' && delivery.author.session_id && <button className="app-button" onClick={() => configuration.onOpen(delivery.author.id, delivery.author.session_id!)}>Open source thread</button>}
    {delivery.payload.origin_id && <p className="muted">Linked to originating request {delivery.payload.origin_id.slice(0, 8)}</p>}
    {delivery.error && <p className="app-error">{delivery.error}</p>}
    {failure && (handled ? <p className="muted">Failure handled</p> : <button className="app-button" onClick={() => { void notifications.acknowledge(configuration.agent, session, delivery, 'failure_handled'); }}>Mark failure handled</button>)}
    {resolution?.evidence && <><p className="muted">Observed outcome: {resolution.outcome === 'failed' ? 'Failed' : 'Completed'}. {resolution.evidence}</p>{unreadResolution(notifications, configuration.agent, session, delivery) && <ViewedContent identity={`${delivery.id}:${resultIdentity(delivery)}`} onView={() => notifications.acknowledge(configuration.agent, session, delivery, 'read')} />}</>}
    {!configuration.readOnly && actionableStates.has(delivery.state) && <details><summary>Investigate outcome</summary><RecoveryActions delivery={delivery} configuration={configuration} notifications={notifications} session={session} /></details>}
  </article>;
}

/** Shows selected-thread attention that has no rendered native message. */
export function ConversationDeliveryRecovery() {
  const configuration = useConversationDelivery();
  const notifications = useThreadNotifications();
  const loading = useAuiState((state) => state.thread.isLoading);
  const messages = useAuiState((state) => state.thread.messages);
  if (!configuration || !notifications || !configuration.session || loading) return null;
  const receipts = notifications.receipts[configuration.agent]?.[configuration.session] ?? [];
  const rendered = new Set(messages.flatMap((message) => {
    const native = nativeMessage(message);
    return native.session === configuration.session ? matchingDeliveries(receipts, native).map((delivery) => delivery.id) : [];
  }));
  const deliveries = receipts.filter((delivery) => !rendered.has(delivery.id) && (actionableStates.has(delivery.state) || Boolean(resolutionEvidence(delivery)) || unhandledFailure(notifications, configuration.agent, configuration.session!, delivery)));
  if (!deliveries.length) return null;
  return <section className="delivery-recovery" aria-label="Delivery attention">{deliveries.map((delivery) => <DeliveryContext key={delivery.id} delivery={delivery} configuration={configuration} notifications={notifications} session={configuration.session!} excerpt />)}</section>;
}

function RecoveryActions({ delivery, configuration, notifications, session }: { delivery: Delivery; configuration: DeliveryConfiguration; notifications: ThreadNotifications; session: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const path = `${agentPath(configuration.organization, configuration.agent)}/sessions/${encodeURIComponent(session)}/dispatches/${encodeURIComponent(delivery.id)}`;
  const reconcile = async () => {
    setBusy(true); setError('');
    try { await api(`${path}/reconcile`, { method: 'POST', csrf: configuration.csrf }); await notifications.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  const resolve = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    setBusy(true); setError('');
    try { await api(`${path}/resolve`, { method: 'POST', csrf: configuration.csrf, body: { operation_id: crypto.randomUUID(), outcome: values.get('outcome'), evidence: values.get('evidence') } }); await notifications.refresh(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <><p className="muted">Inspect the native conversation and external effects before resolving. Resolution records the finding without repeating the work.</p>{error && <p className="app-error" role="alert">{error}</p>}<button className="app-button" disabled={busy} onClick={() => { void reconcile(); }}>Reconcile available evidence</button><form className="app-form" onSubmit={(event) => { void resolve(event); }}><label>Observed outcome<select name="outcome" className="app-select" defaultValue="completed"><option value="completed">Completed</option><option value="failed">Failed</option></select></label><label>Investigation evidence<textarea name="evidence" required className="app-textarea" aria-label="Investigation evidence" /></label><div><button className="app-button" disabled={busy}>Record resolution</button></div></form></>;
}
