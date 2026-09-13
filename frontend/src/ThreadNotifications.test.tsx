import { afterEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThreadNotificationsProvider, useThreadNotifications, type Delivery } from './ThreadNotifications';
import type { Agent } from './workspace-api';

const agent = { id: 'agent', name: 'Reviewer' } as Agent;
const completed = (id = 'delivery', message = 'message'): Delivery => ({
  id,
  session_id: 'thread',
  state: 'completed',
  author: { kind: 'human', id: 'owner', name: 'Owner' },
  payload: { text: 'Run the review', mode: 'queued' },
  updated_at: 1,
  outcome: { kind: 'native_run_completed', message_id: message },
});

function Probe({ delivery = completed() }: { delivery?: Delivery }) {
  const notifications = useThreadNotifications();
  if (!notifications) throw new Error('Missing notifications provider');
  const status = notifications.notificationFor('agent', 'thread');
  return <>
    <p data-testid="status">{JSON.stringify(status)}</p>
    <p data-testid="acknowledgements">{notifications.acknowledgements.agent?.thread?.length ?? 0}</p>
    <p data-testid="deliveries">{notifications.receipts.agent?.thread?.length ?? 0}</p>
    {notifications.errors.map((error) => <p key={error} role="alert">{error}</p>)}
    <button onClick={() => { void notifications.acknowledge('agent', 'thread', delivery, 'read'); }}>Read</button>
    <button onClick={() => { void notifications.acknowledge('agent', 'thread', delivery, 'failure_handled'); }}>Handle failure</button>
    <button onClick={() => { void notifications.refresh(); }}>Refresh</button>
  </>;
}

function renderProvider(delivery?: Delivery) {
  return render(<ThreadNotificationsProvider organization="org" agents={[agent]} csrf="csrf-example" selectedAgent="agent"><Probe delivery={delivery} /></ThreadNotificationsProvider>);
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('shows a completed result until the user personally reads its exact receipt', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return response({ acknowledgements: [{ session_id: 'thread', delivery_id: 'delivery', kind: 'read', outcome_id: 'native:message' }] });
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([completed()]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"unread":true'));
  fireEvent.click(screen.getByRole('button', { name: 'Read' }));
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"unread":false'));
  expect(screen.getByTestId('acknowledgements').textContent).toBe('1');
});

test('keeps a newer completion unread when an older acknowledgement snapshot arrives later', async () => {
  let latest = completed('first', 'first');
  let acknowledgementReads = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return response({ acknowledgements: [{ session_id: 'thread', delivery_id: 'first', kind: 'read', outcome_id: 'native:first' }] });
    if (url.endsWith('/thread-acknowledgements')) {
      acknowledgementReads += 1;
      return response({ acknowledgements: acknowledgementReads === 1 ? [{ session_id: 'thread', delivery_id: 'first', kind: 'read', outcome_id: 'native:first' }] : [] });
    }
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([latest]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":false}'));
  await waitFor(() => expect(screen.getByTestId('acknowledgements').textContent).toBe('1'));
  latest = completed('second', 'second');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"unread":true'));
  expect(screen.getByTestId('acknowledgements').textContent).toBe('1');
});

test('keeps a failed result visible when saving a handled acknowledgement fails', async () => {
  const failed: Delivery = { ...completed(), state: 'failed', outcome: { kind: 'native_error', message_id: 'message' } };
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return response({ detail: 'Acknowledgement unavailable' }, 503);
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([failed]);
  }));
  renderProvider(failed);
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"attention":true'));
  fireEvent.click(screen.getByRole('button', { name: 'Handle failure' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Acknowledgement unavailable');
  expect(screen.getByTestId('status').textContent).toContain('"attention":true');
});

test('retains an older unresolved failure when a newer delivery is still in progress', async () => {
  const oldFailure: Delivery = { ...completed('failed'), state: 'uncertain', outcome: undefined };
  const pending: Delivery = { ...completed('pending'), state: 'active', outcome: undefined, updated_at: 2 };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([oldFailure, pending]);
  }));
  renderProvider(oldFailure);
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"attention":true'));
});

test('does not notify for queued or active progress', async () => {
  const queued: Delivery = { ...completed(), state: 'queued', outcome: undefined };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([queued]);
  }));
  renderProvider(queued);
  await waitFor(() => expect(screen.getByTestId('deliveries').textContent).toBe('1'));
  expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":false}');
});

test('retains a result through a partial refresh and clears it after a successful empty refresh', async () => {
  let dispatches = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    dispatches += 1;
    if (dispatches === 2) return response({ detail: 'Host unavailable' }, 503);
    return response(dispatches === 3 ? [] : [completed()]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"unread":true'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByTestId('status').textContent).toContain('"unread":true');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":false}'));
});

test('treats pending native questions as attention even without a completed dispatch', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question')) return response([{ id: 'question', sessionID: 'thread' }]);
    if (url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":true}'));
});

test('retains pending input through a failed refresh and clears it after its source succeeds empty', async () => {
  let questions = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response({ acknowledgements: [] });
    if (url.endsWith('/opencode/question')) {
      questions += 1;
      if (questions === 2) return response({ detail: 'Host unavailable' }, 503);
      return response(questions === 3 ? [] : [{ id: 'question', sessionID: 'thread' }]);
    }
    if (url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([]);
    return response([]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":true}'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":true}');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('{"unread":false,"attention":false}'));
});

test('reports malformed acknowledgements without losing the existing dispatch result', async () => {
  let malformed = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/thread-acknowledgements')) return response(malformed ? [] : { acknowledgements: [] });
    if (url.endsWith('/opencode/question') || url.endsWith('/opencode/permission')) return response([]);
    if (url.endsWith('/sessions')) return response([{ session_id: 'thread' }]);
    return response([completed()]);
  }));
  renderProvider();
  await waitFor(() => expect(screen.getByTestId('status').textContent).toContain('"unread":true'));
  malformed = true;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Could not read thread acknowledgements.');
  expect(screen.getByTestId('status').textContent).toContain('"unread":true');
});
