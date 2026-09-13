import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { OrganizationThreadSearch } from './OrganizationThreadSearch';
import type { Agent } from './workspace-api';

vi.mock('./ThreadNotificationBadge', () => ({
  ThreadNotificationBadge: ({ agent, session }: { agent?: string; session?: string }) => <span data-testid={`badge-${agent}-${session}`} />,
}));

const agents = [
  { id: 'first', name: 'Researcher', title: 'Research' },
  { id: 'second', name: 'Operator', title: 'Operations' },
] as Agent[];

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function renderSearch(query = 'plan', onOpen = vi.fn()) {
  return { onOpen, ...render(<OrganizationThreadSearch organization="organization" agents={agents} query={query} onOpen={onOpen} />) };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('groups case-insensitive title matches by agent without changing server order', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('/agents/first/')
    ? response([{ session_id: 'second-result', title: 'Plan after review' }, { session_id: 'first-result', title: 'Planning notes' }])
    : response([{ session_id: 'operator-result', title: 'Deployment plan' }])));
  renderSearch('PLAN');
  expect(await screen.findByRole('heading', { name: 'Researcher' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Operator' })).toBeTruthy();
  expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Plan after review', 'Planning notes', 'Deployment plan']);
});

test('opens the selected result in its owning agent and preserves its notification scope', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => response(url.includes('/agents/first/')
    ? [{ session_id: 'thread-one', title: 'Release plan' }]
    : [])));
  const { onOpen } = renderSearch();
  fireEvent.click(await screen.findByRole('button', { name: 'Release plan' }));
  expect(onOpen).toHaveBeenCalledWith('first', 'thread-one');
  expect(screen.getByTestId('badge-first-thread-one')).toBeTruthy();
});

test('shows archived labels when the authorized session inventory includes them', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response([{ session_id: 'thread', title: 'Old plan', archived_at: 1 }])));
  renderSearch();
  expect((await screen.findAllByRole('button', { name: 'Old plan (archived)' }))[0]?.textContent).toContain('Archived');
});

test('shows an empty result only after every authorized inventory completes', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response([])));
  renderSearch();
  expect(await screen.findByText('No matching threads.')).toBeTruthy();
  expect(screen.queryByRole('status')).toBeNull();
});

test('retains available agent matches when another agent is offline and retries that inventory', async () => {
  let unavailable = true;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/agents/first/')) return response([{ session_id: 'thread', title: 'Plan' }]);
    return unavailable ? response({ detail: 'Host unavailable' }, 503) : response([{ session_id: 'recovered', title: 'Recovered plan' }]);
  }));
  renderSearch();
  expect(await screen.findByRole('button', { name: 'Plan' })).toBeTruthy();
  expect(screen.getByRole('alert').textContent).toContain('Operator: Host unavailable');
  unavailable = false;
  fireEvent.click(screen.getByRole('button', { name: 'Retry thread search' }));
  expect(await screen.findByRole('button', { name: 'Recovered plan' })).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});

test('shows healthy agent matches before a slower agent inventory reports its failure', async () => {
  let finishOffline: (value: Response) => void = () => { throw new Error('Offline request was not started'); };
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/agents/first/')) return Promise.resolve(response([{ session_id: 'healthy', title: 'Healthy plan' }]));
    return new Promise<Response>((resolve) => { finishOffline = resolve; });
  }));
  renderSearch();
  expect(await screen.findByRole('button', { name: 'Healthy plan' })).toBeTruthy();
  expect(screen.getByRole('status').textContent).toContain('Searching organization threads');
  finishOffline(response({ detail: 'Host unavailable' }, 503));
  expect((await screen.findByRole('alert')).textContent).toContain('Operator: Host unavailable');
  expect(screen.queryByRole('status')).toBeNull();
});

test('does not apply an older inventory after the organization scope changes', async () => {
  let finishOld: (value: Response) => void = () => { throw new Error('Old request was not started'); };
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/organizations/old/')) return new Promise<Response>((resolve) => { finishOld = resolve; });
    return Promise.resolve(response([{ session_id: 'new-thread', title: 'New plan' }]));
  }));
  const onOpen = vi.fn();
  const rendered = render(<OrganizationThreadSearch organization="old" agents={agents} query="plan" onOpen={onOpen} />);
  rendered.rerender(<OrganizationThreadSearch organization="new" agents={agents} query="plan" onOpen={onOpen} />);
  expect((await screen.findAllByRole('button', { name: 'New plan' })).length).toBe(2);
  finishOld(response([{ session_id: 'old-thread', title: 'Old plan' }]));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Old plan' })).toBeNull());
});

test('filters already loaded organization inventories as the query changes without another fetch', async () => {
  const fetch = vi.fn(async (url: string) => response(url.includes('/agents/first/')
    ? [{ session_id: 'first', title: 'Release plan' }, { session_id: 'second', title: 'Research notes' }]
    : []));
  vi.stubGlobal('fetch', fetch);
  const rendered = renderSearch('plan');
  expect(await screen.findByRole('button', { name: 'Release plan' })).toBeTruthy();
  rendered.rerender(<OrganizationThreadSearch organization="organization" agents={agents} query="notes" onOpen={vi.fn()} />);
  expect(await screen.findByRole('button', { name: 'Research notes' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Release plan' })).toBeNull();
  expect(fetch).toHaveBeenCalledTimes(2);
});
