import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ThreadContext } from './ThreadContext';

const notifications = {
  acknowledge: vi.fn(() => Promise.resolve(true)),
  isAcknowledged: vi.fn((agent: string, session: string, delivery: { id: string }, kind: string): boolean => (
    Boolean(agent && session && delivery && kind) && Boolean(false)
  )),
};

vi.mock('./ThreadNotifications', () => ({
  useThreadNotifications: () => notifications,
  resultIdentity: (delivery: { state: string; outcome?: { kind?: string; message_id?: string; operation_id?: string } }) => {
    if (delivery.state !== 'completed') return undefined;
    if (delivery.outcome?.kind === 'native_run_completed') return `native:${delivery.outcome.message_id}`;
    if (delivery.outcome?.kind === 'operator_resolution') return `resolution:${delivery.outcome.operation_id}`;
    return undefined;
  },
}));
vi.mock('./ViewedContent', () => ({
  ViewedContent: ({ onView }: { onView: () => void }) => (
    <button onClick={onView}>Visible result tail</button>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  notifications.acknowledge.mockClear();
  notifications.isAcknowledged.mockReset();
  notifications.isAcknowledged.mockReturnValue(false);
});
test('shows agent authorship and navigable source thread for peer contributions', async () => {
  const open = vi.fn();
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify([{ id: 'delivery', session_id: 'receiving-thread', state: 'completed', payload: { text: 'Release evidence is ready', mode: 'queued', origin_id: 'origin' }, author: { kind: 'agent', id: 'senior', name: 'Senior engineer', session_id: 'source-thread' }, updated_at: 1 }]))));
  render(<ThreadContext organization="org" agent="junior" session="receiving-thread" csrf="csrf-example" onOpen={open} />);
  fireEvent.click(screen.getByRole('button', { name: 'Thread activity' }));
  expect(await screen.findByText('Senior engineer')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open source thread' }));
  expect(open).toHaveBeenCalledWith('senior', 'source-thread');
});

test('requires an explicit personal acknowledgement before clearing a terminal failure', async () => {
  const failure = {
    id: 'delivery', session_id: 'thread', state: 'failed', payload: { text: 'Release evidence', mode: 'queued' },
    author: { kind: 'agent', id: 'senior', name: 'Senior engineer' }, updated_at: 1,
    outcome: { kind: 'native_error', message_id: 'msg_failure' }, error: 'The check failed.',
  };
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify([failure])));
  vi.stubGlobal('fetch', fetch);
  render(<ThreadContext organization="org" agent="junior" session="thread" csrf="csrf-example" onOpen={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Thread activity' }));
  const handle = await screen.findByRole('button', { name: 'Mark failure handled' });
  expect(notifications.acknowledge).not.toHaveBeenCalled();
  fireEvent.click(handle);
  expect(notifications.acknowledge).toHaveBeenCalledWith('junior', 'thread', failure, 'failure_handled');
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('shows a persisted handled state for a terminal failure', async () => {
  const failure = {
    id: 'delivery', session_id: 'thread', state: 'failed', payload: { text: 'Release evidence', mode: 'queued' },
    author: { kind: 'agent', id: 'senior', name: 'Senior engineer' }, updated_at: 1,
    outcome: { kind: 'native_error', message_id: 'msg_failure' }, error: 'The check failed.',
  };
  notifications.isAcknowledged.mockReturnValue(true);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([failure]))));
  render(<ThreadContext organization="org" agent="junior" session="thread" csrf="csrf-example" onOpen={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Thread activity' }));
  expect(await screen.findByText('Failure handled')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Mark failure handled' })).toBeNull();
  expect(notifications.acknowledge).not.toHaveBeenCalled();
});

test('acknowledges visible operator-resolution evidence', async () => {
  const resolution = {
    id: 'delivery', session_id: 'thread', state: 'completed', payload: { text: 'Release evidence', mode: 'queued' },
    author: { kind: 'human', id: 'owner', name: 'Owner' }, updated_at: 1,
    outcome: { kind: 'operator_resolution', operation_id: '00000000-0000-4000-8000-000000000001', evidence: 'Verified in production.', outcome: 'completed' },
  };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([resolution]))));
  render(<ThreadContext organization="org" agent="junior" session="thread" csrf="csrf-example" onOpen={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Thread activity' }));
  expect(await screen.findByText(/Operator resolution:/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Visible result tail' }));
  expect(notifications.acknowledge).toHaveBeenCalledWith('junior', 'thread', resolution, 'read');
  expect(screen.queryByRole('button', { name: 'Mark failure handled' })).toBeNull();
});
