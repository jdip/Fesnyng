import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Conversation } from './Conversation';

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

test('mounts the maintained thread before an OpenCode-backed session exists', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([]), {
    headers: { 'content-type': 'application/json' },
  })));

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      showThreadList={false}
    />,
  );

  expect(await screen.findByRole('heading', { name: 'How can I help you today?' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toBeTruthy();
});

test('uses the maintained archived thread collection so an archived thread can be restored', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'archived-session', title: 'Archived proof', time: { archived: 1 } }]
      : [];
    return new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });
  }));

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
    />,
  );

  const archived = await screen.findByRole('button', { name: 'Archived (1)' });
  fireEvent.click(archived);

  expect(await screen.findByRole('button', { name: 'Archived proof' })).toBeTruthy();
});

test('sends a first message without invoking OpenCode history compaction for a title', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    const method = request?.method ?? 'GET';
    const body = url.includes('/experimental/session')
      ? []
      : method === 'POST' && url.endsWith('/session')
        ? { id: 'session-one', title: 'New session', time: {} }
        : [];
    return new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      showThreadList={false}
    />,
  );

  fireEvent.change(await screen.findByRole('textbox', { name: 'Message input' }), {
    target: { value: 'Start the work.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/prompt_async');
    })).toBe(true);
  });
  await Promise.resolve();

  expect(fetchMock.mock.calls.some(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/summarize');
  })).toBe(false);
});

test('forks an assistant message through native extras and selects the returned session', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const onSessionChange = vi.fn();
  const assistantMessage = [{
    info: {
      id: 'assistant-one', role: 'assistant', sessionID: 'session-one', parentID: 'user-one',
      modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' },
      cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: 1 }, finish: 'stop',
    },
    parts: [{ id: 'text-one', sessionID: 'session-one', messageID: 'assistant-one', type: 'text', text: 'Ready.' }],
  }];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Source', time: {} }]
      : url.includes('/session/session-one/message')
        ? assistantMessage
      : url.includes('/session/forked-session/message')
          ? []
          : url.includes('/session/forked-session')
            ? { id: 'forked-session', title: 'Forked', time: {} }
            : url.includes('/session/session-one/fork')
              ? { id: 'forked-session', title: 'Forked', time: {} }
            : url.includes('/session/session-one')
              ? { id: 'session-one', title: 'Source', time: {} }
              : [];
    return new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      sessionId="session-one"
      onSessionChange={onSessionChange}
      showThreadList={false}
    />,
  );

  fireEvent.click(await screen.findByRole('button', { name: 'Fork conversation' }));

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/session/session-one/fork');
    })).toBe(true);
    expect(onSessionChange).toHaveBeenLastCalledWith('forked-session');
  });
});

test('renders native question controls without generic Allow or Deny actions', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const assistantMessage = [{
    info: {
      id: 'assistant-question', role: 'assistant', sessionID: 'session-one', parentID: 'user-one',
      modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' },
      cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: 1 }, finish: 'stop',
    },
    parts: [{
      id: 'tool-question', callID: 'question-call', sessionID: 'session-one', messageID: 'assistant-question',
      type: 'tool', tool: 'request_user_input', state: { status: 'pending', input: {}, raw: '' },
    }],
  }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Question', time: {} }]
      : url.includes('/session/session-one/message')
        ? assistantMessage
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Question', time: {} }
          : url.endsWith('/question')
            ? [{
              id: 'question-one', sessionID: 'session-one',
              tool: { messageID: 'assistant-question', callID: 'question-call' },
              questions: [{
                header: 'Direction', question: 'Which path?', multiple: false, custom: false,
                options: [{ label: 'Continue', description: 'Proceed with the plan' }],
              }],
            }]
            : [];
    return new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });
  }));

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      sessionId="session-one"
      showThreadList={false}
    />,
  );

  expect(await screen.findByRole('button', { name: 'Answer' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Reject' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Deny' })).toBeNull();
});

test('refreshes externally created threads without discarding composer text', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  let threads: { id: string; title: string; time: object }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    return new Response(JSON.stringify(url.includes('/experimental/session') ? threads : []), {
      headers: { 'content-type': 'application/json' },
    });
  }));
  const props = { baseUrl: 'http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode', csrfToken: 'csrf-example' };
  const { rerender } = render(<Conversation {...props} refreshKey={0} />);
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Unsent work instructions' } });
  threads = [{ id: 'external-thread', title: 'Colleague investigation', time: {} }];
  rerender(<Conversation {...props} refreshKey={1} />);
  expect(await screen.findByRole('button', { name: 'Colleague investigation' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Unsent work instructions');
});

test('steers an active native thread from its maintained composer and keeps queue and stop available', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/event')) return new Response(`data: ${JSON.stringify({
      type: 'session.status', properties: { sessionID: 'session-one', status: { type: 'busy' } },
    })}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Release review', time: {} }]
      : url.includes('/session/session-one/message')
        ? []
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Release review', time: {} }
          : [];
    return new Response(JSON.stringify(body), {
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <Conversation
      baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode"
      csrfToken="csrf-example"
      sessionId="session-one"
      showThreadList={false}
    />,
  );

  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  expect(await screen.findByRole('button', { name: 'Steer agent' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Queue message' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Stop generating' })).toBeTruthy();

  fireEvent.change(composer, { target: { value: 'Keep composing.' } });
  fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true });
  fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
  expect(fetchMock.mock.calls.some(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/dispatches');
  })).toBe(false);

  fireEvent.change(composer, { target: { value: 'Queue this after the current work.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Queue message' }));
  await waitFor(() => {
    const dispatches = fetchMock.mock.calls.filter(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/dispatches');
    });
    expect(dispatches).toHaveLength(1);
    const init = (dispatches[0] as unknown as [RequestInfo | URL, RequestInit?])[1] ?? {};
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: 'Queue this after the current work.', mode: 'queued', command: null,
    });
  });

  fireEvent.change(composer, { target: { value: 'Check the migration evidence.' } });
  fireEvent.keyDown(composer, { key: 'Enter' });

  await waitFor(() => {
    const dispatches = fetchMock.mock.calls.filter(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/dispatches');
    });
    expect(dispatches).toHaveLength(2);
    const init = (dispatches[1] as unknown as [RequestInfo | URL, RequestInit?])[1] ?? {};
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: 'Check the migration evidence.', mode: 'steering', command: null,
    });
  });
});

test('restores an idle native-send draft when the host rejects it', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.includes('/prompt_async')) return new Response(JSON.stringify({ detail: 'The host rejected this prompt.' }), {
      status: 422, headers: { 'content-type': 'application/json' },
    });
    const body = url.includes('/experimental/session')
      ? []
      : request?.method === 'POST' && url.endsWith('/session')
        ? { id: 'session-one', title: 'New session', time: {} }
        : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));

  render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" showThreadList={false} />);
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep this draft after rejection.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this draft after rejection.'));
});

test('selects a namespaced workflow with slash keyboard input and preserves it with its draft after rejection', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/opencode/command')) return new Response(JSON.stringify([
      { name: 'fesnyng/workspace-check', description: 'workspace-check' },
    ]), { headers: { 'content-type': 'application/json' } });
    if (url.includes('/prompt_async')) return new Response(JSON.stringify({ detail: 'Workflow arguments were rejected.' }), {
      status: 422, headers: { 'content-type': 'application/json' },
    });
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Release review', time: {} }]
      : url.includes('/session/session-one/message')
        ? []
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Release review', time: {} }
          : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" showThreadList={false} />);
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: '/' } });
  expect(await screen.findByRole('listbox', { name: 'Configured workflows' })).toBeTruthy();
  fireEvent.keyDown(composer, { key: 'Enter' });
  expect(await screen.findByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();
  fireEvent.change(composer, { target: { value: 'Check the changed workspace.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Run workflow' }));

  await waitFor(() => {
    const command = fetchMock.mock.calls.find(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/prompt_async');
    });
    expect(command).toBeTruthy();
    const init = (command as unknown as [RequestInfo | URL, RequestInit?])[1] ?? {};
    expect(JSON.parse(String(init.body))).toMatchObject({
      command: 'fesnyng/workspace-check',
      parts: [{ type: 'text', text: 'Check the changed workspace.' }],
    });
  });
  expect((await screen.findByRole('alert')).textContent).toContain('Workflow arguments were rejected.');
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Check the changed workspace.');
  expect(screen.getByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();
});
