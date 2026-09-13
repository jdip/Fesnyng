import { useEffect, useMemo, useState } from 'react';
import { ThreadNotificationBadge } from './ThreadNotificationBadge';
import { agentPath, api, errorMessage, type Agent } from './workspace-api';

type Thread = { session_id: string; title: string; archived: boolean };
type SearchState = {
  scope: string;
  threads: Record<string, Thread[]>;
  errors: Record<string, string>;
  loading: boolean;
};

function sessionRows(value: unknown): Thread[] {
  if (!Array.isArray(value) || !value.every((session) => (
    session && typeof session === 'object'
    && typeof session.session_id === 'string'
    && typeof session.title === 'string'
  ))) throw new Error('Could not read organization threads.');
  return value.map((session) => {
    const row = session as { session_id: string; title: string; archived?: unknown; archived_at?: unknown; time?: unknown };
    const archivedAt = row.time && typeof row.time === 'object' && 'archived' in row.time
      ? row.time.archived
      : undefined;
    return {
      session_id: row.session_id,
      title: row.title,
      archived: row.archived === true || row.archived_at !== null && row.archived_at !== undefined || archivedAt !== null && archivedAt !== undefined,
    };
  });
}

/**
 * Searches the complete, authorized thread inventories already owned by each
 * agent. Results retain the server's per-agent order so pins and incoming
 * recency remain meaningful outside the compact sidebar page.
 */
export function OrganizationThreadSearch({
  organization,
  agents,
  query,
  onOpen,
}: {
  organization: string;
  agents: Agent[];
  query: string;
  onOpen: (agent: string, session: string) => void;
}) {
  const roster = agents.map((agent) => agent.id).join(':');
  const scope = `${organization}:${roster}`;
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<SearchState>({ scope, threads: {}, errors: {}, loading: true });

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    const load = async (agent: Agent) => {
      try {
        const threads = sessionRows(await api<unknown>(`${agentPath(organization, agent.id)}/sessions`, { signal: controller.signal }));
        if (!current || controller.signal.aborted) return;
        setState((previous) => {
          const threadsByAgent = previous.scope === scope ? { ...previous.threads } : {};
          const errors = previous.scope === scope ? { ...previous.errors } : {};
          threadsByAgent[agent.id] = threads;
          delete errors[agent.id];
          return { scope, threads: threadsByAgent, errors, loading: true };
        });
      } catch (cause) {
        if (!current || controller.signal.aborted) return;
        setState((previous) => {
          const threads = previous.scope === scope ? previous.threads : {};
          const errors = previous.scope === scope ? { ...previous.errors } : {};
          errors[agent.id] = errorMessage(cause);
          return { scope, threads, errors, loading: true };
        });
      }
    };
    void Promise.all(agents.map(load)).then(() => {
      if (!current || controller.signal.aborted) return;
      setState((previous) => previous.scope === scope
        ? { ...previous, loading: false }
        : { scope, threads: {}, errors: {}, loading: false });
    });
    return () => {
      current = false;
      controller.abort();
    };
  }, [agents, organization, reload, scope]);

  const trimmed = query.trim().toLocaleLowerCase();
  const groups = useMemo(() => agents.map((agent) => ({
    agent,
    threads: (state.scope === scope ? state.threads[agent.id] ?? [] : [])
      .filter((thread) => thread.title.toLocaleLowerCase().includes(trimmed)),
  })).filter((group) => group.threads.length), [agents, scope, state.scope, state.threads, trimmed]);
  const errors = state.scope === scope ? state.errors : {};
  const loading = state.scope !== scope || state.loading;
  const hasResults = groups.length > 0;

  if (!trimmed) return null;
  return <section className="organization-thread-search" aria-label="Organization thread results">
    {loading && <p role="status" className="muted">Searching organization threads…</p>}
    {groups.map(({ agent, threads }) => <section key={agent.id} className="organization-thread-search-group" aria-label={`${agent.name} threads`}>
      <h2>{agent.name}</h2>
      {threads.map((thread) => <button key={thread.session_id} type="button" className="organization-thread-search-result" aria-label={`${thread.title}${thread.archived ? ' (archived)' : ''}`} onClick={() => onOpen(agent.id, thread.session_id)}>
        <span>{thread.title}{thread.archived && <small>Archived</small>}</span>
        <ThreadNotificationBadge agent={agent.id} session={thread.session_id} />
      </button>)}
    </section>)}
    {Object.entries(errors).map(([agentId, message]) => <p key={agentId} className="app-error" role="alert">{agents.find((agent) => agent.id === agentId)?.name ?? agentId}: {message}</p>)}
    {Object.keys(errors).length > 0 && <button type="button" className="app-button quiet" onClick={() => {
      setState((previous) => previous.scope === scope ? { ...previous, loading: true } : previous);
      setReload((value) => value + 1);
    }}>Retry thread search</button>}
    {!loading && !hasResults && Object.keys(errors).length === 0 && <p className="app-empty">No matching threads.</p>}
  </section>;
}
