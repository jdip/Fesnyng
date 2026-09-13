import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Conversation } from './Conversation';
import { ConversationDraftsProvider } from './ConversationDrafts';

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

HTMLElement.prototype.scrollTo ??= () => {};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const baseUrl = 'http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode';

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function installFetch(prompt: () => Response | Promise<Response>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.includes('/prompt_async')) return prompt();
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Release review', time: {} }]
      : url.includes('/session/session-one/message')
        ? []
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Release review', time: {} }
          : [];
    return response(body);
  });
}

const isPrompt = ([input]: [RequestInfo | URL]) => (input instanceof Request ? input.url : input.toString()).includes('/prompt_async');

function ComposerHarness({ show }: { show: boolean }) {
  return <ConversationDraftsProvider organization="org-one">
    {show && <Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="session-one" showThreadList={false} />}
  </ConversationDraftsProvider>;
}

async function currentComposer() {
  await screen.findByRole('textbox', { name: 'Message input' });
  // The maintained runtime replaces its startup composer as it selects the
  // native thread. Interact with the currently rendered composer, not that
  // transitional node.
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  return screen.getByRole('textbox', { name: 'Message input' });
}

test('restores an unsent draft after an agent runtime remount', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', installFetch(() => response({})));
  const { rerender } = render(<ComposerHarness show />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Keep this investigation draft.' } });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this investigation draft.'));

  rerender(<ComposerHarness show={false} />);
  rerender(<ComposerHarness show />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this investigation draft.'));
});

test('clears a delivered draft so a later runtime remount does not resurrect it', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', installFetch(() => response({ accepted: true })));
  const { rerender } = render(<ComposerHarness show />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Send this once.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', ''));

  rerender(<ComposerHarness show={false} />);
  rerender(<ComposerHarness show />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', ''));
});

test('reuses a failed admission idempotency key after a runtime remount', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = installFetch(() => response({ detail: 'Retry later.' }, 422));
  vi.stubGlobal('fetch', fetchMock);
  const { rerender } = render(<ComposerHarness show />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Retry this exact request.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(1));
  const first = fetchMock.mock.calls.find(isPrompt) as unknown as [RequestInfo | URL, RequestInit];
  const firstAdmission = new Headers(first[1].headers).get('Idempotency-Key');

  rerender(<ComposerHarness show={false} />);
  rerender(<ComposerHarness show />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Retry this exact request.'));
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(2));
  const retry = fetchMock.mock.calls.filter(isPrompt)[1] as unknown as [RequestInfo | URL, RequestInit];
  expect(new Headers(retry[1].headers).get('Idempotency-Key')).toBe(firstAdmission);
});
