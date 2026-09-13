import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ThreadReadReceipt } from './ThreadReadReceipt';

const fixture = vi.hoisted(() => ({
  loading: false,
  status: 'complete',
  acknowledge: vi.fn().mockResolvedValue(true),
}));
vi.mock('@assistant-ui/react', () => ({ useAuiState: (select: (state: unknown) => unknown) => select({
  thread: { isLoading: fixture.loading },
  message: { id: 'merged-tail', role: 'assistant', status: { type: fixture.status }, metadata: { custom: { opencode: {
    originalMessage: { sessionID: 'thread' }, parts: [{ messageID: 'result' }],
  } } } },
}) }));
vi.mock('./ThreadNotifications', () => ({
  useThreadNotifications: () => ({ selectedAgent: 'agent', acknowledge: fixture.acknowledge, receipts: {
    agent: { thread: [{ id: 'delivery', state: 'completed', session_id: 'thread', outcome: { kind: 'native_run_completed', message_id: 'result' } }] },
  } }),
  resultIdentity: () => 'native:result',
}));
vi.mock('./ViewedContent', () => ({ ViewedContent: ({ onView }: { onView: () => void }) => <button onClick={onView}>Visible result tail</button> }));
afterEach(() => { cleanup(); fixture.loading = false; fixture.status = 'complete'; fixture.acknowledge.mockClear(); });

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
