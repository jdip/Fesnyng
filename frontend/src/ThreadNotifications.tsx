import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { agentPath, api, errorMessage, type Agent } from './workspace-api';

/** A dispatch receipt, including the outcome that makes a result personally readable. */
export type Delivery = {
  id: string;
  session_id: string;
  native_message_id?: string;
  state: string;
  author: { kind: string; id: string; name: string; session_id?: string };
  payload: { text: string; mode: string; origin_id?: string };
  updated_at: number;
  error?: string;
  outcome?: { kind?: string; message_id?: string; operation_id?: string; evidence?: string; outcome?: string };
};

export type ThreadAcknowledgement = {
  session_id: string;
  delivery_id: string;
  kind: 'read' | 'failure_handled';
  outcome_id: string;
};

type Notification = { unread: boolean; attention: boolean };
type AcknowledgementIndex = Record<string, Record<string, ThreadAcknowledgement[]>>;
type ReceiptIndex = Record<string, Record<string, Delivery[]>>;
type PendingKind = 'question' | 'permission';
type PendingIndex = Record<string, Record<string, PendingKind[]>>;

export type ThreadNotifications = {
  /** The active agent is exposed so observers can scope native message reads. */
  selectedAgent?: string;
  /** Dispatch receipts are organized by agent then native session. */
  receipts: ReceiptIndex;
  /** Personal acknowledgement rows are available for contextual controls. */
  acknowledgements: AcknowledgementIndex;
  /** Degraded notification sources, supplied to the workspace shell for presentation. */
  errors: string[];
  /** Pending native questions and permissions, organized by agent and thread. */
  pending: PendingIndex;
  notificationFor: (agent: string, session?: string) => Notification;
  isAcknowledged: (agent: string, session: string, delivery: Delivery, kind: ThreadAcknowledgement['kind']) => boolean;
  /** Records a personal read or a handled failure without altering execution. */
  acknowledge: (agent: string, session: string, delivery: Delivery, kind: ThreadAcknowledgement['kind']) => Promise<boolean>;
  refresh: () => Promise<void>;
};

const ThreadNotificationsContext = createContext<ThreadNotifications | undefined>(undefined);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** Returns the durable identity for a completed actual result, if it has one. */
export function resultIdentity(delivery: Delivery): string | undefined {
  if (delivery.state !== 'completed') return undefined;
  if (delivery.outcome?.kind === 'native_run_completed' && delivery.outcome.message_id) return `native:${delivery.outcome.message_id}`;
  if (delivery.outcome?.kind === 'operator_resolution' && delivery.outcome.outcome === 'completed' && delivery.outcome.operation_id && UUID.test(delivery.outcome.operation_id)) return `resolution:${delivery.outcome.operation_id.toLowerCase()}`;
  return undefined;
}

function failureIdentity(delivery: Delivery) {
  return `failed:${delivery.id}`;
}

function indexAcknowledgements(rows: ThreadAcknowledgement[]) {
  return rows.reduce<Record<string, ThreadAcknowledgement[]>>((sessions, receipt) => {
    (sessions[receipt.session_id] ??= []).push(receipt);
    return sessions;
  }, {});
}

function acknowledgementRows(value: unknown): ThreadAcknowledgement[] {
  if (!value || typeof value !== 'object' || !('acknowledgements' in value) || !Array.isArray(value.acknowledgements)) {
    throw new Error('Could not read thread acknowledgements.');
  }
  if (!value.acknowledgements.every((receipt) => (
    receipt && typeof receipt === 'object'
    && typeof receipt.session_id === 'string'
    && typeof receipt.delivery_id === 'string'
    && (receipt.kind === 'read' || receipt.kind === 'failure_handled')
    && typeof receipt.outcome_id === 'string'
  ))) throw new Error('Could not read thread acknowledgements.');
  return value.acknowledgements as ThreadAcknowledgement[];
}

type PendingRequest = { sessionID: string; rootSessionID?: string };

function pendingRows(value: unknown): PendingRequest[] {
  if (!Array.isArray(value) || !value.every((request) => request && typeof request === 'object' && typeof request.sessionID === 'string' && (request.rootSessionID === undefined || typeof request.rootSessionID === 'string'))) {
    throw new Error('Could not read pending thread input.');
  }
  return value as PendingRequest[];
}

function replacePendingKind(previous: Record<string, PendingKind[]>, kind: PendingKind, requests: PendingRequest[]) {
  const next = Object.fromEntries(Object.entries(previous)
    .map(([session, kinds]) => [session, kinds.filter((current) => current !== kind)] as const)
    .filter(([, kinds]) => kinds.length));
  requests.forEach((request) => {
    const sessionID = request.rootSessionID ?? request.sessionID;
    const kinds = next[sessionID] ?? [];
    if (!kinds.includes(kind)) next[sessionID] = [...kinds, kind];
  });
  return next;
}

function mergeAcknowledgements(previous: ThreadAcknowledgement[], next: ThreadAcknowledgement[]) {
  const merged = new Map(previous.map((receipt) => [`${receipt.delivery_id}:${receipt.kind}:${receipt.outcome_id}`, receipt]));
  next.forEach((receipt) => merged.set(`${receipt.delivery_id}:${receipt.kind}:${receipt.outcome_id}`, receipt));
  return [...merged.values()];
}

function receiptFor(rows: ThreadAcknowledgement[], delivery: Delivery, kind: ThreadAcknowledgement['kind']) {
  const outcome = kind === 'read' ? resultIdentity(delivery) : failureIdentity(delivery);
  return outcome && rows.some((receipt) => receipt.delivery_id === delivery.id && receipt.kind === kind && receipt.outcome_id === outcome);
}

function notificationForSession(deliveries: Delivery[], receipts: ThreadAcknowledgement[]): Notification {
  const completed = deliveries.reduce<Delivery | undefined>((latest, delivery) => (
    resultIdentity(delivery) && (!latest || delivery.updated_at >= latest.updated_at) ? delivery : latest
  ), undefined);
  const unread = Boolean(completed && !receiptFor(receipts, completed, 'read'));
  const attention = deliveries.some((delivery) => (
    ['failed', 'unresolved', 'uncertain'].includes(delivery.state)
    && delivery.outcome?.kind !== 'operator_resolution'
    && !receiptFor(receipts, delivery, 'failure_handled')
  ));
  return { unread, attention };
}

/**
 * Maintains personal completion and failure acknowledgement state for every agent.
 * It retains data from sources that are temporarily unavailable, while a successful
 * empty response clears that source's dispatch inventory.
 */
export function ThreadNotificationsProvider({
  organization,
  agents,
  csrf,
  selectedAgent,
  children,
}: PropsWithChildren<{ organization: string; agents: Agent[]; csrf: string; selectedAgent?: string }>) {
  const [acknowledgements, setAcknowledgements] = useState<AcknowledgementIndex>({});
  const [receipts, setReceipts] = useState<ReceiptIndex>({});
  const [pending, setPending] = useState<PendingIndex>({});
  const [errors, setErrors] = useState<string[]>([]);
  const roster = useMemo(() => agents.map((agent) => agent.id), [agents]);
  const rosterKey = roster.join(':');
  const scopeKey = `${organization}:${rosterKey}`;
  const scope = useRef('');
  const refreshGeneration = useRef(0);
  useEffect(() => {
    scope.current = scopeKey;
    return () => { if (scope.current === scopeKey) scope.current = ''; };
  }, [scopeKey]);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const generation = ++refreshGeneration.current;
    const failures: string[] = [];
    const current = () => !signal?.aborted && scope.current === scopeKey && generation === refreshGeneration.current;
    await Promise.all(roster.map(async (agent) => {
      const path = agentPath(organization, agent);
      const acknowledgementRequest = api<{ acknowledgements: ThreadAcknowledgement[] }>(`${path}/thread-acknowledgements`, { signal });
      const sessionsRequest = api<{ session_id: string }[]>(`${path}/sessions`, { signal });
      const pendingRequests = Promise.allSettled((['question', 'permission'] as const).map((kind) => api<unknown>(`${path}/opencode/${kind}`, { signal })));
      const [[acknowledgements, sessions], pendingResults] = await Promise.all([Promise.allSettled([acknowledgementRequest, sessionsRequest]), pendingRequests]);

      if (acknowledgements.status === 'fulfilled') {
        try {
          const rows = acknowledgementRows(acknowledgements.value);
          if (current()) setAcknowledgements((stored) => ({
            ...stored,
            [agent]: indexAcknowledgements(mergeAcknowledgements(
              Object.values(stored[agent] ?? {}).flat(), rows,
            )),
          }));
        } catch (cause) {
          if (current()) failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: ${errorMessage(cause)}`);
        }
      } else if (current()) {
        failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: ${errorMessage(acknowledgements.reason)}`);
      }

      pendingResults.forEach((result, index) => {
        const kind = (['question', 'permission'] as const)[index]!;
        if (result.status === 'fulfilled') {
          try {
            const rows = pendingRows(result.value);
            if (current()) setPending((stored) => ({
              ...stored,
              [agent]: replacePendingKind(stored[agent] ?? {}, kind, rows),
            }));
          } catch (cause) {
            if (current()) failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: ${errorMessage(cause)}`);
          }
        } else if (current()) {
          failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: Pending ${kind} could not be refreshed.`);
        }
      });

      if (sessions.status === 'rejected') {
        if (current()) failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: ${errorMessage(sessions.reason)}`);
        return;
      }
      const rows = sessions.value;
      const dispatches = await Promise.allSettled(rows.map((session) => api<Delivery[]>(`${path}/sessions/${encodeURIComponent(session.session_id)}/dispatches`, { signal })));
      const failedDispatches = dispatches.filter((result) => result.status === 'rejected');
      if (failedDispatches.length && current()) failures.push(`${agents.find((entry) => entry.id === agent)?.name ?? agent}: Some thread results could not be refreshed.`);
      if (!current()) return;
      setReceipts((stored) => {
        const previous = stored[agent] ?? {};
        const next: Record<string, Delivery[]> = {};
        rows.forEach((session, index) => {
          const result = dispatches[index];
          if (result?.status === 'fulfilled') next[session.session_id] = result.value;
          else if (previous[session.session_id]) next[session.session_id] = previous[session.session_id];
        });
        return { ...stored, [agent]: next };
      });
    }));
    if (current()) setErrors(failures);
  }, [agents, organization, roster, scopeKey]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let inFlight = false;
    let disposed = false;
    const poll = async () => {
      if (inFlight || disposed || controller.signal.aborted) return;
      window.clearTimeout(timer);
      timer = undefined;
      inFlight = true;
      try { await refresh(controller.signal); } finally {
        inFlight = false;
        if (!disposed && !controller.signal.aborted && document.visibilityState !== 'hidden') timer = window.setTimeout(() => { void poll(); }, 10000);
      }
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState !== 'hidden') void poll();
    };
    void poll();
    window.addEventListener('focus', refreshWhenVisible);
    window.addEventListener('online', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timer);
      window.removeEventListener('focus', refreshWhenVisible);
      window.removeEventListener('online', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [organization, rosterKey, refresh, scopeKey]);

  const acknowledge = useCallback(async (agent: string, session: string, delivery: Delivery, kind: ThreadAcknowledgement['kind']) => {
    const outcomeId = kind === 'read' ? resultIdentity(delivery) : failureIdentity(delivery);
    if (!outcomeId) {
      setErrors(['This delivery does not have a completed result to acknowledge.']);
      return false;
    }
    const path = `${agentPath(organization, agent)}/sessions/${encodeURIComponent(session)}/acknowledgements`;
    try {
      const response = acknowledgementRows(await api<unknown>(path, {
        method: 'POST',
        csrf,
        body: { delivery_id: delivery.id, kind, outcome_id: outcomeId },
      }));
      setAcknowledgements((current) => ({
        ...current,
        [agent]: indexAcknowledgements(mergeAcknowledgements(
          Object.values(current[agent] ?? {}).flat(),
          response,
        )),
      }));
      setErrors([]);
      return true;
    } catch (cause) {
      setErrors([errorMessage(cause)]);
      return false;
    }
  }, [csrf, organization]);

  const value = useMemo<ThreadNotifications>(() => ({
    selectedAgent,
    receipts,
    acknowledgements,
    errors,
    pending,
    notificationFor: (agent, session) => {
      const agentReceipts = receipts[agent] ?? {};
      const agentAcknowledgements = acknowledgements[agent] ?? {};
      const agentPending = pending[agent] ?? {};
      if (session) {
        const status = notificationForSession(agentReceipts[session] ?? [], agentAcknowledgements[session] ?? []);
        return { ...status, attention: status.attention || (agentPending[session]?.length ?? 0) > 0 };
      }
      return [...new Set([...Object.keys(agentReceipts), ...Object.keys(agentPending)])].reduce<Notification>((summary, current) => {
        const status = notificationForSession(agentReceipts[current] ?? [], agentAcknowledgements[current] ?? []);
        const next = { ...status, attention: status.attention || (agentPending[current]?.length ?? 0) > 0 };
        return { unread: summary.unread || next.unread, attention: summary.attention || next.attention };
      }, { unread: false, attention: false });
    },
    isAcknowledged: (agent, session, delivery, kind) => Boolean(receiptFor(acknowledgements[agent]?.[session] ?? [], delivery, kind)),
    acknowledge,
    refresh,
  }), [acknowledge, acknowledgements, errors, pending, receipts, refresh, selectedAgent]);

  return <ThreadNotificationsContext.Provider value={value}>{children}</ThreadNotificationsContext.Provider>;
}

export function useThreadNotifications() {
  return useContext(ThreadNotificationsContext);
}
