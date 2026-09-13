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

function ComposerHarness({ show, sessionId }: { show: boolean; sessionId?: string }) {
  return <ConversationDraftsProvider organization="org-one">
    {show && <Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId={sessionId} showThreadList={false} />}
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
  const { rerender } = render(<ComposerHarness show sessionId="session-one" />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Keep this investigation draft.' } });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this investigation draft.'));

  rerender(<ComposerHarness show={false} sessionId="session-one" />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this investigation draft.'));
});

test('clears a delivered draft so a later runtime remount does not resurrect it', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', installFetch(() => response({ accepted: true })));
  const { rerender } = render(<ComposerHarness show sessionId="session-one" />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Send this once.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', ''));

  rerender(<ComposerHarness show={false} sessionId="session-one" />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', ''));
});

test('reuses a failed admission idempotency key after a runtime remount', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = installFetch(() => response({ detail: 'Retry later.' }, 422));
  vi.stubGlobal('fetch', fetchMock);
  const { rerender } = render(<ComposerHarness show sessionId="session-one" />);
  const composer = await currentComposer();
  fireEvent.change(composer, { target: { value: 'Retry this exact request.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(1));
  const first = fetchMock.mock.calls.find(isPrompt) as unknown as [RequestInfo | URL, RequestInit];
  const firstAdmission = new Headers(first[1].headers).get('Idempotency-Key');

  rerender(<ComposerHarness show={false} sessionId="session-one" />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Retry this exact request.'));
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(2));
  const retry = fetchMock.mock.calls.filter(isPrompt)[1] as unknown as [RequestInfo | URL, RequestInit];
  expect(new Headers(retry[1].headers).get('Idempotency-Key')).toBe(firstAdmission);
});

test('restores a selected workflow after an agent runtime remount', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/opencode/command')) return response([
      { name: 'fesnyng/workspace-check', description: 'workspace-check' },
    ]);
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Release review', time: {} }]
      : url.includes('/session/session-one/message')
        ? []
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Release review', time: {} }
          : [];
    return response(body);
  }));
  const { rerender } = render(<ComposerHarness show sessionId="session-one" />);
  fireEvent.change(await currentComposer(), { target: { value: '/' } });
  expect(await screen.findByRole('listbox', { name: 'Configured workflows' })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message input' }), { key: 'Enter' });
  expect(await screen.findByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();

  rerender(<ComposerHarness show={false} sessionId="session-one" />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  expect(await screen.findByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();
});

test('keeps an edit made while native-session initialization is in flight', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  let resolveInitialization: (response: Response) => void = () => {};
  const initialization = new Promise<Response>((resolve) => { resolveInitialization = resolve; });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (request?.method === 'POST' && url.endsWith('/session')) return initialization.then((result) => result.clone());
    if (url.includes('/prompt_async')) return response({ detail: 'Retry later.' }, 422);
    return response(url.includes('/experimental/session') ? [] : []);
  });
  vi.stubGlobal('fetch', fetchMock);
  const { rerender } = render(<ComposerHarness show sessionId={undefined} />);
  fireEvent.change(await currentComposer(), { target: { value: 'First wording.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return input instanceof Request && input.method === 'POST' && url.includes('/session') && !url.includes('/prompt_async');
  })).toHaveLength(1));

  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Newer wording.' } });
  resolveInitialization(response({ id: 'session-one', title: 'New session', time: {} }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(1));

  rerender(<ComposerHarness show={false} sessionId={undefined} />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Newer wording.'));
});

test('reuses an in-flight new-thread initialization after a remount retry', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  let resolveInitialization: (response: Response) => void = () => {};
  const initialization = new Promise<Response>((resolve) => { resolveInitialization = resolve; });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (request?.method === 'POST' && url.endsWith('/session')) return initialization.then((result) => result.clone());
    if (url.includes('/prompt_async')) return response({ detail: 'Retry later.' }, 422);
    if (url.includes('/session/session-one/message')) return response([{ info: { id: 'user-one', sessionID: 'session-one', role: 'user', time: { created: 1 } }, parts: [{ id: 'part-one', sessionID: 'session-one', messageID: 'user-one', type: 'text', text: 'Native session is attached.' }] }]);
    if (url.endsWith('/session/session-one')) return response({ id: 'session-one', title: 'New session', time: {} });
    return response([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  const { rerender } = render(<ComposerHarness show sessionId={undefined} />);
  fireEvent.change(await currentComposer(), { target: { value: 'Initialize once.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return input instanceof Request && input.method === 'POST' && url.includes('/session') && !url.includes('/prompt_async');
  })).toHaveLength(1));

  rerender(<ComposerHarness show={false} sessionId={undefined} />);
  rerender(<ComposerHarness show sessionId={undefined} />);
  fireEvent.click(await currentComposer().then(() => screen.getByRole('button', { name: 'Send message' })));
  expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return input instanceof Request && input.method === 'POST' && url.includes('/session') && !url.includes('/prompt_async');
  })).toHaveLength(1);

  resolveInitialization(response({ id: 'session-one', title: 'New session', time: {} }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(isPrompt)).toHaveLength(2));
  expect(fetchMock.mock.calls.filter(([input]) => input instanceof Request && input.method === 'POST' && input.url.endsWith('/session'))).toHaveLength(1);
  const admissions = fetchMock.mock.calls.filter(isPrompt).map((call) => {
    const [input, init] = call as unknown as [RequestInfo | URL, RequestInit];
    return (input instanceof Request ? input.headers : new Headers(init.headers)).get('Idempotency-Key');
  });
  expect(new Set(admissions).size).toBe(1);
  expect(await screen.findByText('Native session is attached.')).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Continue in the created session.' } });
  rerender(<ComposerHarness show={false} sessionId="session-one" />);
  rerender(<ComposerHarness show sessionId="session-one" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Continue in the created session.'));
});
