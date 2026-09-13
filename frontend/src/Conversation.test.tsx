import { expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const runtime = { id: 'runtime' };
const client = { id: 'client' };
const useOpenCodeRuntime = vi.fn(() => runtime);
const createFesnyngOpenCodeClient = vi.fn(() => client);

vi.mock('@assistant-ui/react', () => ({
  AssistantRuntimeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('@assistant-ui/react-opencode', () => ({
  useOpenCodeRuntime,
  useOpenCodeQuestions: () => [],
  useOpenCodeRuntimeExtras: () => ({
    replyToQuestion: vi.fn(),
    rejectQuestion: vi.fn(),
  }),
}));
vi.mock('./lib/opencode-client', () => ({ createFesnyngOpenCodeClient }));
vi.mock('./components/assistant-ui/elements/thread.aui', () => ({
  Thread: ({ allowAttachments }: { allowAttachments: boolean }) => (
    <div data-testid="thread" data-attachments={String(allowAttachments)} />
  ),
}));
vi.mock('./components/assistant-ui/elements/thread-list.aui', () => ({
  ThreadList: () => <div data-testid="thread-list" />,
}));

test('connects the maintained runtime to the selected Fesnyng agent session', async () => {
  const { Conversation } = await import('./Conversation');
  const onSessionChange = vi.fn();
  const onError = vi.fn();

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      sessionId="session-restored"
      onSessionChange={onSessionChange}
      onError={onError}
    />,
  );

  expect(createFesnyngOpenCodeClient).toHaveBeenCalledWith(
    'http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode',
    'csrf-example',
  );
  expect(useOpenCodeRuntime).toHaveBeenCalledWith(expect.objectContaining({
    client,
    initialSessionId: 'session-restored',
    onThreadIdChange: onSessionChange,
    onError,
  }));
  expect(screen.getByTestId('thread').getAttribute('data-attachments')).toBe('false');
});
