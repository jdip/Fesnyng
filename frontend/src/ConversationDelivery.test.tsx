import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ConversationDeliveryFooter, ConversationDeliveryProvider, ConversationDeliveryRecovery } from './ConversationDelivery';

const fixture = vi.hoisted(() => ({
  message: {
    id: 'merged-tail', role: 'assistant', status: { type: 'complete' }, metadata: { custom: { opencode: {
      originalMessage: { id: 'response-tail', sessionID: 'receiving-thread' }, parts: [{ messageID: 'response-head' }],
    } } },
  },
  acknowledge: vi.fn().mockResolvedValue(true),
  refresh: vi.fn().mockResolvedValue(undefined),
  isAcknowledged: vi.fn().mockReturnValue(false),
  receipts: {} as Record<string, Record<string, unknown[]>>,
  messages: [] as unknown[],
  loading: false,
}));

vi.mock('@assistant-ui/react', () => ({ useAuiState: (select: (state: unknown) => unknown) => select({ message: fixture.message, thread: { isLoading: fixture.loading, messages: fixture.messages } }) }));
vi.mock('./ThreadNotifications', () => ({
  useThreadNotifications: () => ({
    receipts: fixture.receipts,
    acknowledge: fixture.acknowledge,
    refresh: fixture.refresh,
    isAcknowledged: fixture.isAcknowledged,
  }),
  resultIdentity: (delivery: { state: string; outcome?: { kind?: string; message_id?: string; operation_id?: string } }) => {
    if (delivery.state !== 'completed') return undefined;
    if (delivery.outcome?.kind === 'native_run_completed') return `native:${delivery.outcome.message_id}`;
    if (delivery.outcome?.kind === 'operator_resolution') return `resolution:${delivery.outcome.operation_id}`;
    return undefined;
  },
}));
vi.mock('./ThreadReadReceipt', () => ({ ThreadReadReceipt: () => null }));
vi.mock('./ViewedContent', () => ({ ViewedContent: ({ onView }: { onView: () => void }) => <button onClick={onView}>Visible delivery result</button> }));

const peerDelivery = {
  id: 'delivery', session_id: 'receiving-thread', state: 'completed', updated_at: 1,
  author: { kind: 'agent', id: 'senior', name: 'Senior engineer', session_id: 'source-thread' },
  payload: { text: 'Release evidence is ready', mode: 'queued', origin_id: 'origin' },
  outcome: { kind: 'native_run_completed', message_id: 'response-head' },
};

function renderDelivery(children: ReactNode, onOpen = vi.fn()) {
  return render(<ConversationDeliveryProvider organization="org" agent="junior" session="receiving-thread" csrf="csrf-example" onOpen={onOpen}>{children}</ConversationDeliveryProvider>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  fixture.message = {
    id: 'merged-tail', role: 'assistant', status: { type: 'complete' }, metadata: { custom: { opencode: {
      originalMessage: { id: 'response-tail', sessionID: 'receiving-thread' }, parts: [{ messageID: 'response-head' }],
    } } },
  };
  fixture.receipts = {};
  fixture.messages = [];
  fixture.loading = false;
  fixture.acknowledge.mockClear();
  fixture.refresh.mockClear();
  fixture.isAcknowledged.mockReset();
  fixture.isAcknowledged.mockReturnValue(false);
});

test('shows peer authorship and source navigation for a native response merged into one assistant message', () => {
  fixture.receipts = { junior: { 'receiving-thread': [peerDelivery] } };
  const open = vi.fn();
  renderDelivery(<ConversationDeliveryFooter />, open);
  expect(screen.getByText('Senior engineer')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open source thread' }));
  expect(open).toHaveBeenCalledWith('senior', 'source-thread');
});

test('maps a peer delivery to its authored native input', () => {
  fixture.message = {
    id: 'input-one', role: 'user', status: { type: 'complete' }, metadata: { custom: { opencode: {
      originalMessage: { id: 'input-one', sessionID: 'receiving-thread' }, parts: [{ messageID: 'input-one' }],
    } } },
  };
  fixture.receipts = { junior: { 'receiving-thread': [{ ...peerDelivery, id: 'input-delivery', native_message_id: 'input-one' }] } };
  renderDelivery(<ConversationDeliveryFooter />);
  expect(screen.getByText('Senior engineer')).toBeTruthy();
});

test('requires a personal acknowledgement before clearing a matched terminal failure', () => {
  const failure = { ...peerDelivery, state: 'failed', outcome: { kind: 'native_error', message_id: 'response-head' }, error: 'The check failed.' };
  fixture.receipts = { junior: { 'receiving-thread': [failure] } };
  renderDelivery(<ConversationDeliveryFooter />);
  fireEvent.click(screen.getByRole('button', { name: 'Mark failure handled' }));
  expect(fixture.acknowledge).toHaveBeenCalledWith('junior', 'receiving-thread', failure, 'failure_handled');
});

test('shows a persisted handled state for a matched terminal failure', () => {
  const failure = { ...peerDelivery, state: 'failed', outcome: { kind: 'native_error', message_id: 'response-head' }, error: 'The check failed.' };
  fixture.receipts = { junior: { 'receiving-thread': [failure] } };
  fixture.isAcknowledged.mockReturnValue(true);
  renderDelivery(<ConversationDeliveryFooter />);
  expect(screen.getByText('Failure handled')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Mark failure handled' })).toBeNull();
});

test('acknowledges visible matched operator-resolution evidence', () => {
  const resolution = { ...peerDelivery, outcome: { kind: 'operator_resolution', operation_id: '00000000-0000-4000-8000-000000000001', evidence: 'Verified in production.', outcome: 'completed' } };
  fixture.message = {
    id: 'input-one', role: 'user', status: { type: 'complete' }, metadata: { custom: { opencode: {
      originalMessage: { id: 'input-one', sessionID: 'receiving-thread' }, parts: [{ messageID: 'input-one' }],
    } } },
  };
  fixture.receipts = { junior: { 'receiving-thread': [{ ...resolution, native_message_id: 'input-one' }] } };
  const mounted = renderDelivery(<ConversationDeliveryFooter />);
  expect(screen.getByText(/Operator resolution:/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Visible delivery result' }));
  expect(fixture.acknowledge).toHaveBeenCalledWith('junior', 'receiving-thread', expect.objectContaining({ id: resolution.id }), 'read');
  fixture.isAcknowledged.mockReturnValue(true);
  mounted.rerender(<ConversationDeliveryProvider organization="org" agent="junior" session="receiving-thread" csrf="csrf-example" onOpen={vi.fn()}><ConversationDeliveryFooter /></ConversationDeliveryProvider>);
  expect(screen.getByText(/Operator resolution:/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Visible delivery result' })).toBeNull();
});

test('keeps unmatched uncertain delivery recovery in the selected thread and refreshes after actions', async () => {
  const unresolved = { ...peerDelivery, state: 'uncertain', outcome: undefined, error: 'The host result is uncertain.' };
  fixture.receipts = { junior: { 'receiving-thread': [unresolved] } };
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({}), { headers: { 'content-type': 'application/json' } })));
  vi.stubGlobal('fetch', fetchMock);
  renderDelivery(<ConversationDeliveryRecovery />);
  expect(screen.getByText('Release evidence is ready')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile available evidence' }));
  await waitFor(() => expect(fixture.refresh).toHaveBeenCalledTimes(1));
  expect(fetchMock).toHaveBeenLastCalledWith('/api/organizations/org/agents/junior/sessions/receiving-thread/dispatches/delivery/reconcile', expect.objectContaining({ method: 'POST' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Investigation evidence' }), { target: { value: 'Verified against the native thread.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record resolution' }));
  await waitFor(() => expect(fixture.refresh).toHaveBeenCalledTimes(2));
  expect(fetchMock).toHaveBeenLastCalledWith('/api/organizations/org/agents/junior/sessions/receiving-thread/dispatches/delivery/resolve', expect.objectContaining({ method: 'POST' }));
  expect(screen.getByText('Release evidence is ready')).toBeTruthy();
});

test('retains unmatched operator-resolution evidence after its read acknowledgement without replaying normal progress', () => {
  const resolution = { ...peerDelivery, id: 'resolution', outcome: { kind: 'operator_resolution', operation_id: '00000000-0000-4000-8000-000000000001', evidence: 'Verified in production.', outcome: 'completed' } };
  const queued = { ...peerDelivery, id: 'queued', state: 'queued', outcome: undefined };
  fixture.receipts = { junior: { 'receiving-thread': [resolution, queued] } };
  const mounted = renderDelivery(<ConversationDeliveryRecovery />);
  expect(screen.getByText(/Operator resolution:/)).toBeTruthy();
  expect(screen.queryByText('Delivery needs review')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Visible delivery result' }));
  expect(fixture.acknowledge).toHaveBeenCalledWith('junior', 'receiving-thread', resolution, 'read');
  fixture.isAcknowledged.mockReturnValue(true);
  mounted.rerender(<ConversationDeliveryProvider organization="org" agent="junior" session="receiving-thread" csrf="csrf-example" onOpen={vi.fn()}><ConversationDeliveryRecovery /></ConversationDeliveryProvider>);
  expect(screen.getByText(/Operator resolution:/)).toBeTruthy();
  expect(screen.queryByText('Delivery needs review')).toBeNull();
});

test('keeps an unmatched terminal failure available for explicit handling', () => {
  const failure = { ...peerDelivery, id: 'failure', state: 'failed', outcome: { kind: 'native_error' }, error: 'The check failed.' };
  fixture.receipts = { junior: { 'receiving-thread': [failure] } };
  renderDelivery(<ConversationDeliveryRecovery />);
  expect(screen.getByText('The check failed.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Mark failure handled' }));
  expect(fixture.acknowledge).toHaveBeenCalledWith('junior', 'receiving-thread', failure, 'failure_handled');
});

test('puts resolution controls beside a mapped unresolved native response', () => {
  const unresolved = { ...peerDelivery, state: 'unresolved', outcome: { kind: 'native_error', message_id: 'response-head' } };
  fixture.receipts = { junior: { 'receiving-thread': [unresolved] } };
  renderDelivery(<ConversationDeliveryFooter />);
  expect(screen.getByText('In response to')).toBeTruthy();
  expect(screen.getByText('Investigate outcome')).toBeTruthy();
});

test('does not bind stale delivery context while the native thread is loading', () => {
  fixture.loading = true;
  fixture.receipts = { junior: { 'receiving-thread': [peerDelivery] } };
  renderDelivery(<><ConversationDeliveryFooter /><ConversationDeliveryRecovery /></>);
  expect(screen.queryByText('Senior engineer')).toBeNull();
});
