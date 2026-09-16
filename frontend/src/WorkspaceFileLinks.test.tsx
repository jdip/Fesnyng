import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Conversation } from './Conversation';
import { CodexConversation } from './CodexConversation';
import { workspaceFilePath } from './WorkspaceFileLinks';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test.each(['../secret', '/docs/%2e%2e/secret', 'docs%5csecret', '/%00secret', 'bad%escape'])('rejects unsafe workspace link %s', (href) => {
  expect(workspaceFilePath(href)).toBeNull();
});
test('preserves normal web and fragment links while decoding workspace filenames', () => {
  expect(workspaceFilePath('https://example.com/report')).toBeUndefined();
  expect(workspaceFilePath('//example.com/report')).toBeUndefined();
  expect(workspaceFilePath('#summary')).toBeUndefined();
  expect(workspaceFilePath('./docs/My%20Report.md#results')).toBe('docs/My Report.md');
});

test.each(['opencode', 'codex'] as const)('%s report links open the authorized Files panel without losing the draft', async (harness) => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  HTMLElement.prototype.scrollTo ??= () => {};
  let session = { id: 'session-one', title: 'Review reports', time: {} };
  const text = '[Root report](/report.md), [Nested report](docs/report.md), [External](https://example.com/report), [Unsafe](../secret.md).';
  const request = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const path = url.pathname;
    if (path.endsWith('/event')) return new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } });
    if (path.endsWith('/session') || path.endsWith('/experimental/session')) return Response.json([session]);
    if (path.endsWith(`/session/${session.id}`)) return Response.json(session);
    if (path.endsWith('/message')) return Response.json([{ info: { id: 'answer', role: 'assistant', sessionID: session.id, parentID: 'user', modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, time: { created: 1, completed: 2 }, finish: 'stop' }, parts: [{ id: 'text', messageID: 'answer', sessionID: session.id, type: 'text', text }] }]);
    if (path.endsWith('/history')) return Response.json({ thread: { id: session.id }, historyState: 'complete', turns: [{ id: 'turn', status: 'completed', items: [{ id: 'answer', type: 'agentMessage', text }] }] });
    if (path.endsWith('/file/content')) return Response.json({ rootSessionID: session.id, sessionID: session.id, path: url.searchParams.get('path'), type: 'text', encoding: 'utf-8', content: `Preview ${url.searchParams.get('path')}`, size: 12, truncated: false, contentType: 'text/plain' });
    if (path.endsWith('/file')) return Response.json({ rootSessionID: session.id, sessionID: session.id, path: url.searchParams.get('path'), entries: [] });
    return Response.json([]);
  });
  vi.stubGlobal('fetch', request);
  const Component = harness === 'opencode' ? Conversation : CodexConversation;
  const view = render(<Component key={session.id} baseUrl={`http://workspace.test/api/organizations/org/agents/agent/${harness}`} csrfToken="csrf" sessionId={session.id} showThreadList={false} />);
  const root = await screen.findByRole('link', { name: 'Root report' });
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep this draft' } });
  fireEvent.click(root);
  expect(await screen.findByText('Preview report.md')).toBeTruthy();
  fireEvent.click(screen.getByRole('link', { name: 'Nested report' }));
  expect(await screen.findByText('Preview docs/report.md')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Download' }).getAttribute('href')).toContain(`/${harness}/file/download?sessionID=session-one&path=docs%2Freport.md`);
  expect((composer as HTMLTextAreaElement).value).toBe('Keep this draft');
  expect(screen.getByRole('link', { name: 'External' }).getAttribute('href')).toBe('https://example.com/report');
  const unsafe = screen.getByText('Unsafe');
  expect(unsafe.getAttribute('aria-disabled')).toBe('true');
  fireEvent.click(unsafe);
  expect(request.mock.calls.some(([input]) => String(input).includes('secret'))).toBe(false);
  session = { ...session, id: 'session-two', title: 'Second workspace' };
  view.rerender(<Component key={session.id} baseUrl={`http://workspace.test/api/organizations/org/agents/agent/${harness}`} csrfToken="csrf" sessionId={session.id} showThreadList={false} />);
  expect(screen.queryByText('Preview docs/report.md')).toBeNull();
  fireEvent.click(await screen.findByRole('link', { name: 'Root report' }));
  expect(await screen.findByText('Preview report.md')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Download' }).getAttribute('href')).toContain('sessionID=session-two&path=report.md');
});
