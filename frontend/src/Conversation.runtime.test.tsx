import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
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

test('publishes an OpenCode generated title to the header and Project inventory without losing a draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  let events: ReadableStreamDefaultController<Uint8Array> | undefined;
  let title = 'New thread';
  const onTitleChanged = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/event')) return new Response(new ReadableStream({ start(controller) { events = controller; } }), { headers: { 'content-type': 'text/event-stream' } });
    const session = { id: 'session-one', title, time: {} };
    return Response.json(url.includes('/experimental/session') ? [session] : url.endsWith('/session/session-one') ? session : []);
  }));
  render(<Conversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/opencode" csrfToken="csrf" sessionId="session-one" showThreadList={false} onTitleChanged={onTitleChanged} />);
  await screen.findByRole('heading', { name: 'New thread' });
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep this draft' } });
  await waitFor(() => expect(events).toBeTruthy());
  title = 'Review release report';
  events!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: 'session.updated', properties: { info: { id: 'session-one', title, time: {} } } })}\n\n`));
  await screen.findByRole('heading', { name: title });
  await waitFor(() => expect(onTitleChanged).toHaveBeenCalledTimes(1));
  expect((composer as HTMLTextAreaElement).value).toBe('Keep this draft');
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

test('keeps frozen OpenCode history readable without native action controls', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'frozen-session', title: 'Frozen history', time: {} }]
      : url.includes('/session/frozen-session/message') ? [{
        info: { id: 'assistant-one', role: 'assistant', sessionID: 'frozen-session', time: { created: 1 } },
        parts: [{ id: 'text-one', sessionID: 'frozen-session', messageID: 'assistant-one', type: 'text', text: 'Historical result.' }],
      }]
        : url.includes('/session/frozen-session') ? { id: 'frozen-session', title: 'Frozen history', time: {} }
          : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="frozen-session" showThreadList={false} readOnly />);

  expect(await screen.findByText('Historical result.')).toBeTruthy();
  expect(screen.getByText('This thread is permanently frozen and read-only.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Files' })).toBeNull();
  expect(screen.queryByRole('textbox', { name: 'Message input' })).toBeNull();
  expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/question') || String(input).includes('/permission'))).toBe(false);
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
    parts: [{
      id: 'tool-one', callID: 'tool-one', sessionID: 'session-one', messageID: 'assistant-one',
      type: 'tool', tool: 'read', state: { status: 'completed', input: { description: 'Read the workspace' }, output: {} },
    }],
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

  const fork = await screen.findByRole('button', { name: 'Fork conversation' });
  const activity = fork.closest('[data-slot="aui_assistant-message-root"]');
  const footer = activity?.querySelector('[data-slot="aui_assistant-message-footer"]');
  expect(activity?.getAttribute('data-activity-only')).toBe('true');
  expect(footer).not.toBeNull();
  expect(footer?.getAttribute('class')).toContain('absolute');
  expect(footer?.getAttribute('class')).toContain('top-0');
  expect(footer?.getAttribute('class')).toContain('end-10');
  expect(footer?.getAttribute('class')).toContain('max-w-[calc(100%-2.5rem)]');
  expect(footer?.getAttribute('class')).toContain('bg-background');
  expect(footer?.getAttribute('class')).toContain('opacity-0');

  fork.focus();
  expect(document.activeElement).toBe(fork);
  fireEvent.click(fork);

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

  const answer = await screen.findByRole('button', { name: 'Answer' });
  expect(answer).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Reject' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Deny' })).toBeNull();

  const message = document.querySelector('[data-slot="aui_assistant-message-root"]');
  expect(message).not.toBeNull();
  expect(message?.getAttribute('data-activity-only')).toBeNull();
  expect(message?.querySelector('[data-slot="aui_assistant-message-footer"]')).not.toBeNull();
});

test('uses the latest ordered thought and tool description in collapsed group previews', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const assistantMessage = [{
    info: {
      id: 'assistant-preview', role: 'assistant', sessionID: 'session-one', parentID: 'user-one',
      modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' },
      cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: 1 }, finish: 'stop',
    },
    parts: [
      { id: 'reasoning-first', sessionID: 'session-one', messageID: 'assistant-preview', type: 'reasoning', text: 'First thought.' },
      { id: 'reasoning-latest', sessionID: 'session-one', messageID: 'assistant-preview', type: 'reasoning', text: 'Latest thought explains the decision.' },
      { id: 'tool-first', callID: 'tool-first', sessionID: 'session-one', messageID: 'assistant-preview', type: 'tool', tool: 'read', state: { status: 'completed', input: { description: 'Read the first file' }, output: {} } },
      { id: 'tool-latest', callID: 'tool-latest', sessionID: 'session-one', messageID: 'assistant-preview', type: 'tool', tool: 'bash', state: { status: 'completed', input: { description: 'Run the latest migration' }, output: {} } },
    ],
  }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Preview', time: {} }]
      : url.includes('/session/session-one/message')
        ? assistantMessage
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Preview', time: {} }
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

  const reasoning = await screen.findByRole('button', {
    name: 'Reasoning: Latest thought explains the decision.',
  });
  const tools = screen.getByRole('button', {
    name: '2 tool calls: Run the latest migration',
  });
  const activity = reasoning.closest('[data-slot="aui_assistant-message-root"]');
  expect(reasoning.getAttribute('aria-expanded')).toBe('false');
  expect(tools.getAttribute('aria-expanded')).toBe('false');
  expect(activity?.getAttribute('data-activity-only')).toBe('true');
  const footer = activity?.querySelector('[data-slot="aui_assistant-message-footer"]');
  expect(footer?.getAttribute('class')).toContain('absolute');
  expect(footer?.getAttribute('class')).toContain('top-0');
  expect(footer?.getAttribute('class')).toContain('end-10');
  expect(footer?.getAttribute('class')).toContain('max-w-[calc(100%-2.5rem)]');
  expect(footer?.getAttribute('class')).toContain('bg-background');
  expect(footer?.getAttribute('class')).toContain('opacity-0');
  fireEvent.click(reasoning);
  fireEvent.click(tools);
  expect(screen.getByText('First thought.')).toBeTruthy();
  expect(screen.getByText('Run the latest migration')).toBeTruthy();
});

test('opens available OpenCode reasoning and failed or successful tool details from one group control', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const assistantMessage = [{
    info: {
      id: 'assistant-disclosure', role: 'assistant', sessionID: 'session-one', parentID: 'user-one',
      modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' },
      cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: 1 }, finish: 'stop',
    },
    parts: [
      { id: 'reasoning-blank', sessionID: 'session-one', messageID: 'assistant-disclosure', type: 'reasoning', text: '   ' },
      { id: 'reasoning-available', sessionID: 'session-one', messageID: 'assistant-disclosure', type: 'reasoning', text: 'Available native reasoning.' },
      { id: 'tool-success', callID: 'tool-success', sessionID: 'session-one', messageID: 'assistant-disclosure', type: 'tool', tool: 'read', state: { status: 'completed', input: { description: 'Read the report' }, output: 'Report contents' } },
      { id: 'tool-failed', callID: 'tool-failed', sessionID: 'session-one', messageID: 'assistant-disclosure', type: 'tool', tool: 'bash', state: { status: 'error', input: { description: 'Run the migration' }, error: 'Migration failed' } },
    ],
  }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Disclosure', time: {} }]
      : url.includes('/session/session-one/message')
        ? assistantMessage
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Disclosure', time: {} }
          : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));

  render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" showThreadList={false} />);

  const reasoning = await screen.findByRole('button', { name: 'Reasoning: Available native reasoning.' });
  const tools = screen.getByRole('button', { name: '2 tool calls: Run the migration' });
  fireEvent.click(reasoning);
  fireEvent.click(tools);
  expect(screen.getAllByText('Available native reasoning.')).toHaveLength(2);
  expect(screen.getByText('Report contents')).toBeTruthy();
  expect(screen.getByText('Migration failed')).toBeTruthy();
  expect(screen.getAllByRole('button', { name: /Used tool:/ }).every((tool) => tool.getAttribute('aria-expanded') === 'true')).toBe(true);

  fireEvent.click(tools);
  expect(tools.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(tools);
  expect(tools.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('Report contents')).toBeTruthy();
});

test('updates collapsed previews as streamed parts change and newer tools arrive', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const encoder = new TextEncoder();
  let events: ReadableStreamDefaultController<Uint8Array> | undefined;
  const emit = (type: string, properties: Record<string, unknown>) => {
    events?.enqueue(encoder.encode(`data: ${JSON.stringify({ type, properties })}\n\n`));
  };
  const assistantMessage = [{
    info: {
      id: 'assistant-live-preview', role: 'assistant', sessionID: 'session-one', parentID: 'user-one',
      modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' },
      cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created: 1 }, finish: 'stop',
    },
    parts: [
      { id: 'reasoning-live', sessionID: 'session-one', messageID: 'assistant-live-preview', type: 'reasoning', text: 'Initial thought' },
      { id: 'tool-live-first', callID: 'tool-live-first', sessionID: 'session-one', messageID: 'assistant-live-preview', type: 'tool', tool: 'read', state: { status: 'running', input: {}, output: {} } },
    ],
  }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/event')) return new Response(new ReadableStream({
      start(controller) { events = controller; },
    }), { headers: { 'content-type': 'text/event-stream' } });
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Live preview', time: {} }]
      : url.includes('/session/session-one/message')
        ? assistantMessage
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Live preview', time: {} }
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

  expect(await screen.findByRole('button', { name: 'Reasoning: Initial thought' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '1 tool call: read' })).toBeTruthy();
  await waitFor(() => expect(events).toBeTruthy());

  emit('message.part.delta', {
    sessionID: 'session-one', messageID: 'assistant-live-preview', partID: 'reasoning-live', field: 'text', delta: ' expands live',
  });
  expect(await screen.findByRole('button', { name: 'Reasoning: Initial thought expands live' })).toBeTruthy();

  emit('message.part.updated', {
    part: {
      id: 'tool-live-latest', callID: 'tool-live-latest', sessionID: 'session-one', messageID: 'assistant-live-preview',
      type: 'tool', tool: 'bash', state: { status: 'running', input: { description: '  Run\nlatest migration  ' }, output: {} },
    },
  });
  expect(await screen.findByRole('button', { name: '2 tool calls: Run latest migration' })).toBeTruthy();
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
  await new Promise((resolve) => setTimeout(resolve, 0));
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Unsent work instructions' } });
  threads = [{ id: 'external-thread', title: 'Colleague investigation', time: {} }];
  rerender(<Conversation {...props} refreshKey={1} />);
  expect(await screen.findByRole('button', { name: 'Colleague investigation' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Unsent work instructions');
});

test('refreshes incoming-message order on return to the app without losing the active draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const first = { id: 'session-one', title: 'First thread', time: { created: 1 } };
  const second = { id: 'session-two', title: 'Second thread', time: { created: 2 } };
  let threads = [first, second];
  let refreshGate = Promise.resolve();
  let finishRefresh = () => {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.includes('/experimental/session')) await refreshGate;
    const body = url.includes('/experimental/session') ? threads
      : url.endsWith('/session/session-one') ? first : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" />);
  await screen.findByRole('button', { name: 'Second thread' });
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep this draft' } });
  const titles = () => screen.getAllByRole('button', { name: /^(First|Second) thread$/ }).map((item) => within(item).getByText(/^(First|Second) thread$/).textContent);
  expect(titles()).toEqual(['First thread', 'Second thread']);
  // The host orders by incoming authorship even when native creation order differs.
  threads = [second, first];
  refreshGate = new Promise<void>((resolve) => { finishRefresh = resolve; });
  fireEvent.focus(window);
  expect(screen.getByRole('button', { name: 'First thread' })).toBeTruthy();
  finishRefresh();
  await waitFor(() => expect(titles()).toEqual(['Second thread', 'First thread']));
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this draft');
  expect(screen.getByRole('button', { name: 'First thread' }).closest('[data-active]')?.getAttribute('data-active')).toBe('true');
});

test('pins and unpins a thread through its menu while preserving the active draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const recent = { id: 'recent', title: 'Recent thread', time: {} };
  const older = { id: 'older', title: 'Older thread', time: {} };
  let pinned = false;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    const method = init?.method ?? request?.method ?? 'GET';
    if (url.endsWith('/thread-pins/older')) pinned = method === 'PUT';
    const body = url.includes('/thread-pins') ? { session_ids: pinned ? ['older'] : [] }
      : url.includes('/experimental/session') ? (pinned ? [older, recent] : [recent, older])
        : url.endsWith('/session/recent') ? recent : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="recent" />);
  await screen.findByRole('button', { name: 'Older thread' });
  fireEvent.change(await screen.findByRole('textbox', { name: 'Message input' }), { target: { value: 'Keep my draft' } });
  fireEvent.keyDown(screen.getAllByRole('button', { name: 'More options' })[1]!, { key: 'Enter' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Pin thread' }));
  await waitFor(() => expect(pinned).toBe(true));
  await waitFor(() => expect(screen.getAllByRole('button', { name: /(?:Recent|Older) thread/ })[0]!.textContent).toContain('Older thread'));
  expect(screen.getByRole('img', { name: 'Pinned' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep my draft');
  fireEvent.keyDown(screen.getAllByRole('button', { name: 'More options' })[0]!, { key: 'Enter' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Unpin thread' }));
  await waitFor(() => expect(pinned).toBe(false));
  await waitFor(() => expect(screen.getAllByRole('button', { name: /(?:Recent|Older) thread/ })[0]!.textContent).toContain('Recent thread'));
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep my draft');
});

test('restores a personal pin on attachment and reports a failed unpin without losing the draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const thread = { id: 'saved', title: 'Saved thread', time: {} };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/thread-pins/saved') && init?.method === 'DELETE') {
      return new Response(JSON.stringify({ detail: 'Pins are temporarily unavailable.' }), { status: 503 });
    }
    const body = url.endsWith('/thread-pins') ? { session_ids: ['saved'] }
      : url.includes('/experimental/session') ? [thread]
        : url.endsWith('/session/saved') ? thread : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="saved" />);
  expect(await screen.findByRole('img', { name: 'Pinned' })).toBeTruthy();
  fireEvent.change(await screen.findByRole('textbox', { name: 'Message input' }), { target: { value: 'Keep this too' } });
  fireEvent.keyDown(screen.getByRole('button', { name: 'More options' }), { key: 'Enter' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Unpin thread' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Pins are temporarily unavailable. Retry pins');
  expect(screen.getByRole('img', { name: 'Pinned' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this too');
});

test('steers an active native thread from its maintained composer and keeps queue and stop available', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/event')) return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({
          type: 'session.status', properties: { sessionID: 'session-one', status: { type: 'busy' } },
        })}\n\n`));
      },
    }), { headers: { 'content-type': 'text/event-stream' } });
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

  await screen.findByRole('textbox', { name: 'Message input' });
  expect(await screen.findByRole('button', { name: 'Steer agent' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Queue message' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Stop generating' })).toBeTruthy();
  const composer = screen.getByRole('textbox', { name: 'Message input' });

  fireEvent.change(composer, { target: { value: 'Keep composing.' } });
  fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true });
  fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
  expect(fetchMock.mock.calls.some(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toBe(false);

  fireEvent.change(composer, { target: { value: 'Queue this after the current work.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Queue message' }));
  await waitFor(() => {
    const dispatches = fetchMock.mock.calls.filter(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/prompt_async');
    });
    expect(dispatches).toHaveLength(1);
    const init = (dispatches[0] as unknown as [RequestInfo | URL, RequestInit?])[1] ?? {};
    expect(JSON.parse(String(init.body))).toMatchObject({
      parts: [{ type: 'text', text: 'Queue this after the current work.' }], mode: 'queued',
    });
  });

  fireEvent.change(composer, { target: { value: 'Check the migration evidence.' } });
  fireEvent.keyDown(composer, { key: 'Enter' });

  await waitFor(() => {
    const dispatches = fetchMock.mock.calls.filter(([input]) => {
      const url = input instanceof Request ? input.url : input.toString();
      return url.includes('/prompt_async');
    });
    expect(dispatches).toHaveLength(2);
    const init = (dispatches[1] as unknown as [RequestInfo | URL, RequestInit?])[1] ?? {};
    expect(JSON.parse(String(init.body))).toMatchObject({
      parts: [{ type: 'text', text: 'Check the migration evidence.' }], mode: 'steering',
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

  render(<StrictMode><Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" showThreadList={false} /></StrictMode>);
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep this draft after rejection.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep this draft after rejection.'));
  expect((await screen.findByRole('alert')).textContent).toContain('The host rejected this prompt.');
  expect(screen.getByRole('button', { name: 'Send message' })).toHaveProperty('disabled', false);
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

  const { rerender } = render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" showThreadList={false} onError={() => {}} />);
  await new Promise((resolve) => setTimeout(resolve, 0));
  const composer = await screen.findByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: '/' } });
  expect(await screen.findByRole('listbox', { name: 'Configured workflows' })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message input' }), { key: 'Enter' });
  expect(await screen.findByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Check the changed workspace.' } });
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
  const firstRequest = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[0] as unknown as [RequestInfo | URL, RequestInit];
  const firstAdmission = new Headers(firstRequest[1].headers).get('Idempotency-Key');

  rerender(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" showThreadList={false} onError={() => {}} />);
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Check the changed workspace.');
  expect(screen.getByLabelText('Remove workflow fesnyng/workspace-check')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Run workflow' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toHaveLength(2));
  const retryRequest = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[1] as unknown as [RequestInfo | URL, RequestInit];
  expect(new Headers(retryRequest[1].headers).get('Idempotency-Key')).toBe(firstAdmission);
});

test('retries the first accepted delivery mode and admission across idle and active transitions', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const encoder = new TextEncoder();
  let events: ReadableStreamDefaultController<Uint8Array> | undefined;
  const emit = (type: string, properties: Record<string, unknown>) => {
    events?.enqueue(encoder.encode(`data: ${JSON.stringify({ type, properties })}\n\n`));
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/event')) return new Response(new ReadableStream({
      start(controller) {
        events = controller;
        emit('session.status', { sessionID: 'session-one', status: { type: 'busy' } });
      },
    }), { headers: { 'content-type': 'text/event-stream' } });
    if (url.includes('/prompt_async')) return new Response(JSON.stringify({ detail: 'Retry later.' }), {
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
  expect(await screen.findByRole('button', { name: 'Steer agent' })).toBeTruthy();
  let composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Preserve steering.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Steer agent' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toHaveLength(1));
  const steeringRequest = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[0] as unknown as [RequestInfo | URL, RequestInit];

  emit('session.idle', { sessionID: 'session-one' });
  expect(await screen.findByRole('button', { name: 'Send message' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toHaveLength(2));
  const steeringRetry = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[1] as unknown as [RequestInfo | URL, RequestInit];
  expect(new Headers(steeringRetry[1].headers).get('Idempotency-Key')).toBe(new Headers(steeringRequest[1].headers).get('Idempotency-Key'));
  expect(JSON.parse(String(steeringRetry[1].body)).mode).toBe('steering');

  composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Preserve queue.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toHaveLength(3));
  const queuedRequest = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[2] as unknown as [RequestInfo | URL, RequestInit];

  emit('session.status', { sessionID: 'session-one', status: { type: 'busy' } });
  expect(await screen.findByRole('button', { name: 'Steer agent' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Steer agent' }));
  await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })).toHaveLength(4));
  const queuedRetry = fetchMock.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : input.toString();
    return url.includes('/prompt_async');
  })[3] as unknown as [RequestInfo | URL, RequestInit];
  expect(new Headers(queuedRetry[1].headers).get('Idempotency-Key')).toBe(new Headers(queuedRequest[1].headers).get('Idempotency-Key'));
  expect(JSON.parse(String(queuedRetry[1].body)).mode).toBe('queued');
});

test('keeps a replacement workflow when an earlier workflow submission settles', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  let settleFirst: (response: Response) => void = () => {};
  const firstResponse = new Promise<Response>((resolve) => { settleFirst = resolve; });
  let promptCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : undefined;
    const url = request?.url ?? input.toString();
    if (url.endsWith('/opencode/command')) return new Response(JSON.stringify([
      { name: 'fesnyng/first', description: 'first' },
      { name: 'fesnyng/replacement', description: 'replacement' },
    ]), { headers: { 'content-type': 'application/json' } });
    if (url.includes('/prompt_async')) {
      promptCalls += 1;
      return promptCalls === 1 ? firstResponse : new Response('{}');
    }
    const body = url.includes('/experimental/session')
      ? [{ id: 'session-one', title: 'Release review', time: {} }]
      : url.includes('/session/session-one/message')
        ? []
        : url.includes('/session/session-one')
          ? { id: 'session-one', title: 'Release review', time: {} }
          : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));

  render(<Conversation baseUrl="http://127.0.0.1:5175/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-one" showThreadList={false} />);
  await new Promise((resolve) => setTimeout(resolve, 0));
  fireEvent.change(await screen.findByRole('textbox', { name: 'Message input' }), { target: { value: '/first' } });
  expect(await screen.findByRole('listbox', { name: 'Configured workflows' })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message input' }), { key: 'Enter' });
  expect(await screen.findByLabelText('Remove workflow fesnyng/first')).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Start first.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Run workflow' }));
  await waitFor(() => expect(promptCalls).toBe(1));

  fireEvent.click(screen.getByLabelText('Remove workflow fesnyng/first'));
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: '/replacement' } });
  expect(await screen.findByRole('listbox', { name: 'Configured workflows' })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Message input' }), { key: 'Enter' });
  expect(await screen.findByLabelText('Remove workflow fesnyng/replacement')).toBeTruthy();

  settleFirst(new Response('{}'));
  await waitFor(() => expect(screen.getByLabelText('Remove workflow fesnyng/replacement')).toBeTruthy());
});

test('portals the maintained list into the sidebar with pins, pages and the selected thread without a duplicate new-thread control', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const threads = Array.from({ length: 10 }, (_, index) => ({ id: `session-${index}`, title: `Sidebar thread ${index}`, time: {} }));
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.endsWith('/thread-pins') ? { session_ids: ['session-8'] }
      : url.includes('/experimental/session') ? [threads[8], ...threads.filter((_, index) => index !== 8)]
        : url.endsWith('/session/session-9') ? threads[9] : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  const selected = vi.fn();
  const target = document.createElement('aside');
  target.setAttribute('aria-label', 'Sidebar threads');
  document.body.append(target);
  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" sessionId="session-9" threadListTarget={target} threadPageSize={6} onThreadSelect={selected} />);
  await screen.findByRole('button', { name: 'Sidebar thread 9' });
  await screen.findByRole('img', { name: 'Pinned' });
  expect(target.querySelector('[data-slot="aui_thread-list-root"]')).toBeTruthy();
  expect(screen.queryByRole('searchbox', { name: 'Search threads' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Sidebar thread 7' })).toBeNull();
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Keep sidebar draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Show more threads' }));
  expect(await screen.findByRole('button', { name: 'Sidebar thread 7' })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep sidebar draft');
  fireEvent.click(screen.getByRole('button', { name: 'Sidebar thread 9' }));
  expect(selected).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'New Thread' })).toBeNull();
  target.remove();
});

test('opens same-agent search selections through the maintained runtime without losing another draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const threads = [{ id: 'first', title: 'First selection', time: {} }, { id: 'second', title: 'Second selection', time: {} }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.endsWith('/thread-pins') ? { session_ids: [] }
      : url.includes('/experimental/session') ? threads
        : url.endsWith('/session/first') ? threads[0] : url.endsWith('/session/second') ? threads[1] : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  const props = { baseUrl: 'http://localhost/api/organizations/org-one/agents/agent-one/opencode', csrfToken: 'csrf-example' };
  const { rerender } = render(<Conversation {...props} sessionId="first" />);
  await screen.findByRole('button', { name: 'First selection' });
  fireEvent.change(await screen.findByRole('textbox', { name: 'Message input' }), { target: { value: 'Keep first selection draft' } });
  rerender(<Conversation {...props} sessionId="second" />);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Second selection' }).closest('[data-active]')?.getAttribute('data-active')).toBe('true'));
  rerender(<Conversation {...props} sessionId="first" />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message input' })).toHaveProperty('value', 'Keep first selection draft'));
});

test('renders a frozen OpenCode history without native mutation controls', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/question')) throw new Error('Frozen history must not request native questions');
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" showThreadList={false} readOnly />);

  expect(await screen.findByText('This thread is permanently frozen and read-only.')).toBeTruthy();
  expect(screen.queryByRole('textbox', { name: 'Message input' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Fork conversation' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Stop generating' })).toBeNull();
  expect(fetchMock.mock.calls.some(([input]) => (input instanceof Request ? input.url : input.toString()).endsWith('/question'))).toBe(false);
});

test('keeps a removed managed OpenCode history mounted without execution or recovery controls', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/question')) throw new Error('Removed workspaces must not request native questions');
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<Conversation baseUrl="http://localhost/api/organizations/org-one/agents/agent-one/opencode" csrfToken="csrf-example" showThreadList={false} executionBlocked executionBlockedState="removed" />);

  expect(await screen.findByText('This workspace was removed. Prepare its replacement before continuing.')).toBeTruthy();
  expect(screen.queryByRole('textbox', { name: 'Message input' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Fork conversation' })).toBeNull();
  expect(screen.queryByText('Investigate outcome')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Files' })).toBeNull();
  expect(fetchMock.mock.calls.some(([input]) => (input instanceof Request ? input.url : input.toString()).endsWith('/question'))).toBe(false);
});
