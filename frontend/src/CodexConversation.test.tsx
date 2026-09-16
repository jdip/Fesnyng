import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CodexConversation, createCodexThreadListAdapter } from './CodexConversation';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('uses the Codex facade for the writable native thread inventory and mutations', async () => {
  const request = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    if (url.endsWith('/session') && !init.method) return new Response(JSON.stringify([
      { id: 'thread-one', title: 'Investigate', runtime_type: 'codex', time: { created: 1 } },
      { id: 'old-codex', title: 'Frozen history', runtime_type: 'codex', frozen_at: 0, time: { created: 1 } },
      { id: 'old-opencode', title: 'Other harness', runtime_type: 'opencode', time: { created: 1 } },
    ]));
    if (url.endsWith('/session') && init.method === 'POST') return new Response(JSON.stringify({ id: 'thread-two', title: 'New thread' }));
    if (url.endsWith('/thread-one') && init.method === 'PATCH') return new Response(JSON.stringify({ id: 'thread-one', title: 'Renamed' }));
    if (url.endsWith('/thread-one') && init.method === 'PATCH') return new Response(JSON.stringify({ id: 'thread-one', title: 'Renamed' }));
    return new Response(JSON.stringify({ id: 'thread-one', title: 'Investigate' }));
  });
  vi.stubGlobal('fetch', request);
  const adapter = createCodexThreadListAdapter('http://workspace.test/api/organizations/org/agents/agent/codex', 'csrf-example');

  await expect(adapter.list()).resolves.toMatchObject({ threads: [{ remoteId: 'thread-one', externalId: 'thread-one', title: 'Investigate', status: 'regular' }] });
  await expect(adapter.initialize('local-thread')).resolves.toEqual({ remoteId: 'thread-two', externalId: 'thread-two' });
  await adapter.rename('thread-one', 'Renamed');
  await adapter.archive('thread-one');
  await adapter.unarchive('thread-one');
  await expect(adapter.delete('thread-one')).rejects.toThrow('cannot be permanently deleted');

  expect(request.mock.calls.map(([url, init]) => [String(url), init?.method])).toEqual([
    ['http://workspace.test/api/organizations/org/agents/agent/codex/session', undefined],
    ['http://workspace.test/api/organizations/org/agents/agent/codex/session', 'POST'],
    ['http://workspace.test/api/organizations/org/agents/agent/codex/session/thread-one', 'PATCH'],
    ['http://workspace.test/api/organizations/org/agents/agent/codex/session/thread-one', 'PATCH'],
    ['http://workspace.test/api/organizations/org/agents/agent/codex/session/thread-one', 'PATCH'],
  ]);
  expect(JSON.parse(request.mock.calls[1][1]!.body as string)).toEqual({});
  expect(JSON.parse(request.mock.calls[2][1]!.body as string)).toEqual({ title: 'Renamed' });
  expect(JSON.parse(request.mock.calls[3][1]!.body as string)).toMatchObject({ time: { archived: expect.any(Number) } });
  expect(JSON.parse(request.mock.calls[4][1]!.body as string)).toEqual({ time: { archived: null } });
});

test('sends one stable Project workspace selection on a Codex creation retry', async () => {
  const request = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return new Response(JSON.stringify({ id: 'thread-two', title: 'New thread' }));
  });
  vi.stubGlobal('fetch', request);
  const creation = { project_id: 'website', checkout_branch: 'release', creation_id: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4' };
  const adapter = createCodexThreadListAdapter('http://workspace.test/api/organizations/org/agents/agent/codex', 'csrf-example', creation);

  await adapter.initialize('local-one');
  await adapter.initialize('local-two');

  expect(request.mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string))).toEqual([creation, creation]);
});


test('mounts a Codex thread with native pending input and its streamed completion', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  let completed = false;
  let historyReads = 0;
  class EventSourceStub {
    static instances: EventSourceStub[] = [];
    listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();
    constructor(readonly url: string) { EventSourceStub.instances.push(this); }
    addEventListener(type: string, listener: (event: MessageEvent<string>) => void) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]); }
    close() {}
    emit(type: string, body: unknown) { this.listeners.get(type)?.forEach((listener) => listener({ data: JSON.stringify(body) } as MessageEvent<string>)); }
  }
  vi.stubGlobal('EventSource', EventSourceStub);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    const request = input instanceof Request ? input : undefined;
    if (url.endsWith('/session')) return new Response(JSON.stringify([{ id: 'thread-one', title: 'Investigate', time: {} }]));
    if (url.includes('/history')) { historyReads += 1; return new Response(JSON.stringify({ thread: { id: 'thread-one' }, turns: [{ id: 'turn-one', status: completed ? 'completed' : 'inProgress', items: completed ? [{ id: 'assistant-one', type: 'agentMessage', text: 'Done' }] : [{ id: 'user-one', type: 'userMessage', content: 'Start' }] }], historyState: 'complete' })); }
    if (url.includes('/pending?')) return new Response(JSON.stringify([
      { id: 'question-one', method: 'item/tool/requestUserInput', params: { question: 'Proceed?', questions: [{ id: 'choice', question: 'Choose', options: [{ label: 'Yes', description: 'Continue' }] }] } },
      { id: 'elicitation-one', method: 'mcpServer/elicitation/request', params: { message: 'MCP details', mode: 'form', requestedSchema: { type: 'object', properties: { requiredText: { type: 'string' }, optionalDecimal: { type: 'number', minimum: 0.25, maximum: 2.5, multipleOf: 0.25 } }, required: ['requiredText'] } } },
      { id: 'integer-constraints', method: 'mcpServer/elicitation/request', params: { message: 'Integer constraints', mode: 'form', requestedSchema: { type: 'object', properties: { count: { type: 'integer', minimum: 0.5, multipleOf: 2 } }, required: ['count'] } } },
      { id: 'integer-even', method: 'mcpServer/elicitation/request', params: { message: 'Even count', mode: 'form', requestedSchema: { type: 'object', properties: { evenCount: { type: 'integer', minimum: 0, multipleOf: 2 } }, required: ['evenCount'] } } },
    ]));
    if (request?.method === 'POST') return new Response(JSON.stringify({ accepted: true }));
    return new Response(JSON.stringify({ id: 'thread-one', title: 'Investigate', time: {} }));
  });
  vi.stubGlobal('fetch', fetchMock);
  render(<CodexConversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/codex" csrfToken="csrf" sessionId="thread-one" showThreadList={false} />);
  expect(await screen.findByText('Proceed?')).toBeTruthy();
  expect(await screen.findByLabelText('Answer Choose')).toBeTruthy();
  const optionalDecimal = await screen.findByLabelText('Answer optionalDecimal');
  expect(optionalDecimal.hasAttribute('required')).toBe(false);
  expect(optionalDecimal.getAttribute('min')).toBe('0.25');
  expect(optionalDecimal.getAttribute('max')).toBe('2.5');
  expect(optionalDecimal.getAttribute('step')).toBe('0.25');
  expect(await screen.findByText('This MCP request uses an input form the workspace cannot safely render.')).toBeTruthy();
  expect((await screen.findByLabelText('Answer evenCount')).getAttribute('step')).toBe('2');
  await waitFor(() => expect(EventSourceStub.instances).toHaveLength(1));
  fireEvent.click(screen.getByRole('button', { name: 'Stop generating' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/abort'))).toBe(true));
  EventSourceStub.instances[0]!.emit('open', {});
  EventSourceStub.instances[0]!.emit('open', {});
  await waitFor(() => expect(historyReads).toBeGreaterThan(1));
  completed = true;
  EventSourceStub.instances[0]!.emit('message', { method: 'turn/completed', params: { threadId: 'thread-one', turn: { id: 'turn-one', status: 'completed', items: [{ id: 'assistant-one', type: 'agentMessage', text: 'Done' }] } } });
  expect(await screen.findByText('Done')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Answer Choose'), { target: { value: 'Yes' } });
  fireEvent.click(screen.getByRole('button', { name: 'Answer' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/pending/question-one/reply?'))).toBe(true));
  fireEvent.change(screen.getByLabelText('Answer requiredText'), { target: { value: 'Required only' } });
  fireEvent.click(screen.getAllByRole('button', { name: 'Continue' })[0]!);
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/pending/elicitation-one/reply?'))).toBe(true));
  const elicitation = (fetchMock.mock.calls as unknown as Array<[RequestInfo | URL, RequestInit]>).find(([input]) => String(input).includes('/pending/elicitation-one/reply?'))!;
  expect(JSON.parse(elicitation[1].body as string)).toEqual({ response: { action: 'accept', content: { requiredText: 'Required only' } } });
  fireEvent.change(screen.getByRole('textbox', { name: 'Message input' }), { target: { value: 'Continue with the selected skill.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/prompt'))).toBe(true));
});

test('clears a transient pending-request reload error after the host recovers', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  let pendingReads = 0;
  let replyAttempts = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith('/session')) return new Response(JSON.stringify([{ id: 'thread-one', title: 'Investigate', time: {} }]));
    if (url.includes('/history')) return new Response(JSON.stringify({ thread: { id: 'thread-one' }, turns: [], historyState: 'complete' }));
    if (url.includes('/pending/question-one/reply?') && init.method === 'POST') {
      replyAttempts += 1;
      return replyAttempts === 1
        ? new Response(JSON.stringify({ detail: 'Reply failed' }), { status: 503 })
        : new Response(JSON.stringify({ accepted: true }));
    }
    if (url.includes('/pending?')) {
      pendingReads += 1;
      if (pendingReads === 1 || pendingReads === 4) return new Response(JSON.stringify({ detail: 'Agent host is unreachable' }), { status: 503 });
      return new Response(JSON.stringify([{ id: 'question-one', method: 'item/tool/requestUserInput', params: { question: 'Recovered question' } }]));
    }
    return new Response(JSON.stringify({ id: 'thread-one', title: 'Investigate', time: {} }));
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<CodexConversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/codex" csrfToken="csrf" sessionId="thread-one" showThreadList={false} />);

  expect(await screen.findByText('Agent host is unreachable')).toBeTruthy();
  window.dispatchEvent(new Event('focus'));
  expect(await screen.findByRole('textbox', { name: 'Answer Recovered question' })).toBeTruthy();
  expect(screen.queryByText('Agent host is unreachable')).toBeNull();
  fireEvent.change(screen.getByRole('textbox', { name: 'Answer Recovered question' }), { target: { value: 'Yes' } });
  fireEvent.click(screen.getByRole('button', { name: 'Answer' }));
  expect(await screen.findByText('Reply failed')).toBeTruthy();
  window.dispatchEvent(new Event('focus'));
  await waitFor(() => expect(pendingReads).toBeGreaterThan(2));
  expect(screen.queryByText('Reply failed')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Answer' }));
  expect(await screen.findByText('Agent host is unreachable')).toBeTruthy();
  window.dispatchEvent(new Event('focus'));
  await waitFor(() => expect(pendingReads).toBeGreaterThan(4));
  expect(screen.queryByText('Agent host is unreachable')).toBeNull();
});

test('compacts completed Codex reasoning and tool activity while retaining the final reply spacing', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith('/session')) return new Response(JSON.stringify([{ id: 'thread-one', title: 'Inspect', time: {} }]));
    if (url.includes('/history')) return new Response(JSON.stringify({
      thread: { id: 'thread-one' }, historyState: 'complete', turns: [{ id: 'turn-one', status: 'completed', items: [
        { id: 'reasoning-one', type: 'reasoning', summary: ['Inspect the workspace.'] },
        { id: 'command-one', type: 'commandExecution', command: 'git status', status: 'completed', aggregatedOutput: 'clean', exitCode: 0 },
        { id: 'answer-one', type: 'agentMessage', text: 'The workspace is clean.' },
      ] }],
    }));
    if (url.includes('/pending?')) return new Response(JSON.stringify([]));
    return new Response(JSON.stringify({ id: 'thread-one', title: 'Inspect', time: {} }));
  }));

  const { container } = render(<CodexConversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/codex" csrfToken="csrf" sessionId="thread-one" showThreadList={false} />);

  expect(await screen.findByText('The workspace is clean.')).toBeTruthy();
  const activity = container.querySelectorAll('[data-activity-only="true"]');
  expect(activity).toHaveLength(2);
  for (const item of activity) {
    const footer = item.querySelector('[data-slot="aui_assistant-message-footer"]');
    expect(footer?.getAttribute('class')).toContain('absolute');
    expect(footer?.getAttribute('class')).toContain('opacity-0');
  }
  const reply = screen.getByText('The workspace is clean.').closest('[data-slot="aui_assistant-message-root"]');
  expect(reply?.getAttribute('data-activity-only')).toBeNull();
  expect(reply?.querySelector('[data-slot="aui_assistant-message-footer"]')).toBeTruthy();
});

test('renders frozen Codex snapshot history without native write controls', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith('/session')) return new Response(JSON.stringify([{ id: 'old-codex-thread', title: 'Frozen Codex thread', frozen: true, time: {} }]));
    if (url.includes('/history')) return new Response(JSON.stringify({
      thread: { id: 'old-codex-thread', title: 'Frozen Codex thread' },
      turns: [{ id: 'turn-one', status: 'completed', items: [
        { id: 'assistant-one', type: 'agentMessage', text: 'Frozen Codex analysis.' },
        { id: 'change-one', type: 'fileChange', changes: [{ path: 'src/frozen.ts', kind: { type: 'delete' }, diff: '@@ -1 +0,0 @@\n-export const frozen = true;' }], result: { output: 'Done!' } },
      ] }], historyState: 'complete',
    }));
    if (url.includes('/pending?')) throw new Error('Frozen history must not poll pending requests');
    return new Response(JSON.stringify({ id: 'old-codex-thread', title: 'Frozen Codex thread', time: {} }));
  });
  vi.stubGlobal('fetch', fetchMock);
  class EventSourceStub { constructor() { throw new Error('Frozen history must not open a live native event stream'); } }
  vi.stubGlobal('EventSource', EventSourceStub);

  render(<CodexConversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/codex" csrfToken="csrf" sessionId="old-codex-thread" showThreadList={false} readOnly />);

  expect(await screen.findByText('Frozen Codex analysis.')).toBeTruthy();
  fireEvent.click(await screen.findByRole('button', { name: '1 tool call: apply_patch' }));
  expect(await screen.findByLabelText('deleted src/frozen.ts')).toBeTruthy();
  expect(screen.getByText('This thread is permanently frozen and read-only.')).toBeTruthy();
  expect(screen.queryByRole('textbox', { name: 'Message input' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Stop generating' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/pending?'))).toBe(false);
});

test('keeps a removed managed Codex history mounted without native write controls', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith('/session')) return new Response(JSON.stringify([{ id: 'old-codex-thread', title: 'Removed Codex thread', time: {} }]));
    if (url.includes('/history')) return new Response(JSON.stringify({ thread: { id: 'old-codex-thread', title: 'Removed Codex thread' }, turns: [], historyState: 'complete' }));
    if (url.includes('/pending?')) throw new Error('Removed workspaces must not poll pending requests');
    return new Response(JSON.stringify({ id: 'old-codex-thread', title: 'Removed Codex thread', time: {} }));
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<CodexConversation baseUrl="http://workspace.test/api/organizations/org/agents/agent/codex" csrfToken="csrf" sessionId="old-codex-thread" showThreadList={false} executionBlocked executionBlockedState="removed" />);

  expect(await screen.findByText('This workspace was removed. Prepare its replacement before continuing.')).toBeTruthy();
  expect(screen.queryByRole('textbox', { name: 'Message input' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Stop generating' })).toBeNull();
  expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/pending?'))).toBe(false);
});
