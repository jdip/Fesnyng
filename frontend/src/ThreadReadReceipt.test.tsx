import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ThreadReadReceipt } from './ThreadReadReceipt';

const fixture = vi.hoisted(() => ({
  loading: false,
  status: 'complete',
  codex: false,
  acknowledge: vi.fn().mockResolvedValue(true),
}));
vi.mock('@assistant-ui/react', () => ({ useAuiState: (select: (state: unknown) => unknown) => select({
  thread: { isLoading: fixture.loading, messages: fixture.codex ? [
    { id: 'reasoning', role: 'assistant', metadata: { custom: { codex: { sessionId: 'thread', turnId: 'turn-one', item: { type: 'reasoning' } } } } },
    { id: 'final-tail', role: 'assistant', metadata: { custom: { codex: { sessionId: 'thread', turnId: 'turn-one', item: { type: 'agentMessage' } } } } },
  ] : [] },
  message: fixture.codex
    ? { id: fixture.status === 'reasoning' ? 'reasoning' : 'final-tail', role: 'assistant', status: { type: 'complete' }, metadata: { custom: { codex: { sessionId: 'thread', turnId: 'turn-one', item: { type: fixture.status === 'reasoning' ? 'reasoning' : 'agentMessage' } } } } }
    : { id: 'merged-tail', role: 'assistant', status: { type: fixture.status }, metadata: { custom: { opencode: { originalMessage: { sessionID: 'thread' }, parts: [{ messageID: 'result' }] } } } },
}) }));
vi.mock('./ThreadNotifications', () => ({
  useThreadNotifications: () => ({ selectedAgent: 'agent', acknowledge: fixture.acknowledge, receipts: {
    agent: { thread: [{ id: 'delivery', state: 'completed', session_id: 'thread', outcome: fixture.codex ? { kind: 'codex_turn_completed', turn_id: 'turn-one' } : { kind: 'native_run_completed', message_id: 'result' } }] },
  } }),
  resultIdentity: () => 'native:result',
}));
vi.mock('./ViewedContent', () => ({ ViewedContent: ({ onView }: { onView: () => void }) => <button onClick={onView}>Visible result tail</button> }));
afterEach(() => { cleanup(); fixture.loading = false; fixture.status = 'complete'; fixture.codex = false; fixture.acknowledge.mockClear(); });

test('acknowledges a native response retained inside a merged assistant message only when viewed', () => {
  render(<ThreadReadReceipt />);
  expect(fixture.acknowledge).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Visible result tail' }));
  expect(fixture.acknowledge).toHaveBeenCalledWith('agent', 'thread', expect.objectContaining({ id: 'delivery' }), 'read');
});

test('does not acknowledge history placeholders or an unfinished assistant message', () => {
  fixture.loading = true;
  const { rerender } = render(<ThreadReadReceipt />);
  expect(screen.queryByRole('button')).toBeNull();
  fixture.loading = false;
  fixture.status = 'running';
  rerender(<ThreadReadReceipt />);
  expect(screen.queryByRole('button')).toBeNull();
});


test('does not mark a Codex turn read from an earlier reasoning or tool item', () => {
  fixture.codex = true;
  fixture.status = 'reasoning';
  const { rerender } = render(<ThreadReadReceipt />);
  expect(screen.queryByRole('button')).toBeNull();
  fixture.status = 'complete';
  rerender(<ThreadReadReceipt />);
  expect(screen.getByRole('button', { name: 'Visible result tail' })).toBeTruthy();
});
