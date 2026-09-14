import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CodexConversation, createCodexThreadListAdapter } from './CodexConversation';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('uses the Codex facade for the maintained remote thread inventory and mutations', async () => {
  const request = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    if (url.endsWith('/session') && !init.method) return new Response(JSON.stringify([{ id: 'thread-one', title: 'Investigate', time: { created: 1 } }]));
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
