import { useEffect, useState } from 'react';
import { agentPath, api, errorMessage, type Agent } from './workspace-api';
import type { Delivery } from './ThreadContext';
type Item = { id: string; agent: string; agentName: string; session: string; title: string; state: string; text: string; updated: number; author: string };
export function Activity({ organization, agents, onOpen, compact = false }: { organization: string; agents: Agent[]; onOpen: (agent: string, session: string) => void; compact?: boolean }) {
  const [items, setItems] = useState<Item[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [notification, setNotification] = useState('');
  const [search, setSearch] = useState('');
  const roster = JSON.stringify(agents.map(({ id, name }) => ({ id, name })));
  useEffect(() => {
    const controller = new AbortController();
    const names = JSON.parse(roster) as { id: string; name: string }[];
    let timer: number | undefined;
    let previous: Map<string, Item> | undefined;
    async function poll() {
      const next: Item[] = []; const failed: string[] = [];
      const refreshedSessionLists = new Set<string>();
      const refreshedSessions = new Set<string>();
      const refreshedDispatches = new Set<string>();
      const refreshedPending = new Set<string>();
      await Promise.all(names.map(async (agent) => {
        const path = agentPath(organization, agent.id);
        try {
          const sessions = await api<{ session_id: string; title: string }[]>(`${path}/sessions`, { signal: controller.signal });
          refreshedSessionLists.add(agent.id);
          for (const session of sessions) refreshedSessions.add(`${agent.id}:${session.session_id}`);
          const threads = await Promise.allSettled(sessions.map(async (session) => {
            const deliveries = await api<Delivery[]>(`${path}/sessions/${session.session_id}/dispatches`, { signal: controller.signal });
            const latest = deliveries.at(-1);
            if (latest) next.push({ id: latest.id, agent: agent.id, agentName: agent.name, session: session.session_id, title: session.title, state: latest.state, text: latest.error || latest.payload.text, author: latest.author.name, updated: latest.updated_at });
          }));
          threads.forEach((result, index) => {
            if (result.status === 'fulfilled') refreshedDispatches.add(`${agent.id}:${sessions[index]!.session_id}`);
          });
          if (threads.some((result) => result.status === 'rejected')) failed.push(`${agent.name}: Some thread activity could not be refreshed.`);
          const pending = await Promise.allSettled(['question', 'permission'].map(async (kind) => {
            const requests = await api<{ id: string; sessionID: string }[]>(`${path}/opencode/${kind}`, { signal: controller.signal });
            for (const request of requests) next.push({ id: `${kind}:${request.id}`, agent: agent.id, agentName: agent.name, session: request.sessionID, title: sessions.find((entry) => entry.session_id === request.sessionID)?.title ?? 'Thread needs input', state: 'needs input', text: kind === 'question' ? 'A question is waiting for your answer.' : 'A permission request needs attention.', author: agent.name, updated: 0 });
          }));
          pending.forEach((result, index) => {
            if (result.status === 'fulfilled') refreshedPending.add(`${agent.id}:${['question', 'permission'][index]}`);
          });
          if (pending.some((result) => result.status === 'rejected')) failed.push(`${agent.name}: Pending input could not be refreshed.`);
        } catch (cause) { failed.push(`${agent.name}: ${errorMessage(cause)}`); }
      }));
      if (controller.signal.aborted) return;
      const retained = previous ? [...previous.values()].filter((item) => {
        const pendingKind = item.id.startsWith('question:') ? 'question' : item.id.startsWith('permission:') ? 'permission' : undefined;
        if (pendingKind) return !refreshedPending.has(`${item.agent}:${pendingKind}`);
        const session = `${item.agent}:${item.session}`;
        if (!refreshedSessionLists.has(item.agent)) return true;
        return refreshedSessions.has(session) && !refreshedDispatches.has(session);
      }) : [];
      const items = [...retained, ...next];
      const meaningful = items.filter((item) => ['completed', 'failed', 'unresolved', 'uncertain', 'needs input'].includes(item.state));
      const changes = meaningful.filter((item) => previous ? previous.get(item.id)?.state !== item.state : item.state === 'needs input' || item.state === 'unresolved' || item.state === 'uncertain');
      const changedThreads = new Set(changes.map((item) => `${item.agent}:${item.session}`)).size;
      if (changedThreads) setNotification(`${changedThreads} ${changedThreads === 1 ? 'thread has a result or needs' : 'threads have results or need'} attention.`);
      previous = new Map(items.map((item) => [item.id, item]));
      setItems(items.sort((a, b) => (a.state === 'needs input' ? -1 : b.state === 'needs input' ? 1 : b.updated - a.updated))); setErrors(failed);
      timer = window.setTimeout(() => { void poll(); }, 10000);
    }
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [organization, roster]);
  if (compact) return notification ? <div className="app-notice" role="status">{notification} <button className="app-button quiet" onClick={() => setNotification('')}>Dismiss</button></div> : null;
  return <div className="workspace-page"><h2>Activity</h2><p className="page-intro">Results, current work, and requests that need your attention.</p>{notification && <p className="app-notice" role="status">{notification}</p>}<input className="app-input" aria-label="Search threads" placeholder="Search agents, threads, and results…" value={search} onChange={(event) => setSearch(event.target.value)} />{errors.map((error) => <p key={error} className="app-error">{error}</p>)}{items.filter((item) => `${item.agentName} ${item.title} ${item.text}`.toLowerCase().includes(search.toLowerCase())).map((item) => <article className="activity-item" key={item.id}><div className="app-actions"><strong>{item.agentName}</strong><span className="app-badge">{item.state}</span></div><button className="app-button quiet" onClick={() => onOpen(item.agent, item.session)}>{item.title}</button><p>{item.text.slice(0, 220)}{item.text.length > 220 ? '…' : ''}</p><p className="muted">From {item.author}</p></article>)}{!items.length && !errors.length && <p className="app-empty">No thread activity yet.</p>}</div>;
}
