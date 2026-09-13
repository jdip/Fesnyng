import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Conversation } from './Conversation';
import { useState } from 'react';
import { ConversationDraftsProvider } from './ConversationDrafts';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
HTMLElement.prototype.scrollTo ??= () => {};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const baseUrl = 'http://localhost/api/organizations/org/agents/agent/opencode';
const metadata = { repository: { state: 'available', name: 'sample-project' }, branch: { state: 'available', name: 'feature' }, changes: { state: 'available', added: 7, deleted: 3, untracked: 2, binaryFiles: 0 }, subagents: { state: 'available', count: 0 }, backgroundProcesses: { state: 'unavailable' } };

function serve(withOther = false) {
  let title = 'Layout work';
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const requests = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : undefined;
    const url = new URL(request?.url ?? input.toString());
    const method = init?.method ?? request?.method ?? 'GET';
    if (method === 'PATCH' && url.pathname.endsWith('/session/one')) {
      const body = init?.body ? JSON.parse(String(init.body)) : await request?.json();
      title = body.title;
    }
    const body = url.pathname.endsWith('/context') ? (url.pathname.includes('/two/') ? { ...metadata, repository: { state: 'absent' } } : metadata)
      : url.pathname.endsWith('/thread-pins') ? { session_ids: withOther ? ['one'] : [] }
      : url.pathname.endsWith('/session') || url.pathname.endsWith('/experimental/session') ? [{ id: 'one', title, time: {} }, ...(withOther ? [{ id: 'two', title: 'Other workspace', time: {} }] : [])]
      : url.pathname.endsWith('/session/one') ? { id: 'one', title, time: {} }
      : [];
    return Response.json(body);
  });
  vi.stubGlobal('fetch', requests);
  return requests;
}

test('active thread context renames through the maintained runtime without replacing its composer', async () => {
  const requests = serve();
  render(<Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="one" />);
  const info = await screen.findByRole('region', { name: 'Thread information' });
  expect(await within(info).findByText('sample-project')).toBeTruthy();
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Retain my draft' } });
  fireEvent.click(within(info).getByRole('button', { name: 'Rename thread' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Thread title' }), { target: { value: 'Renamed work' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }));
  await waitFor(() => expect(within(info).getByRole('heading', { name: 'Renamed work' })).toBeTruthy());
  expect(screen.getByRole('textbox', { name: 'Message input' })).toBe(composer);
  expect((composer as HTMLTextAreaElement).value).toBe('Retain my draft');
  expect(requests.mock.calls.some(([input, init]) => (init?.method ?? (input instanceof Request ? input.method : 'GET')) === 'PATCH')).toBe(true);
});

test('pinned and unpinned thread subtitles use each thread workspace and keep their title actions', async () => {
  serve(true);
  render(<Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="one" />);
  const pinned = await screen.findByRole('button', { name: /Layout work/ });
  const unpinned = await screen.findByRole('button', { name: 'Other workspace' });
  expect(await within(pinned).findByText('sample-project')).toBeTruthy();
  expect(await within(unpinned).findByText('No repository')).toBeTruthy();
  expect(within(pinned).getByRole('img', { name: 'Pinned' })).toBeTruthy();
  expect(screen.getAllByRole('button', { name: 'More options' })).toHaveLength(2);
});

test('explicit refresh reloads the active workspace context without remounting the composer', async () => {
  const requests = serve();
  const view = render(<Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="one" refreshKey={0} />);
  await within(await screen.findByRole('region', { name: 'Thread information' })).findByText('sample-project');
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep through refresh' } });
  const contextCalls = () => requests.mock.calls.filter(([input]) => (input instanceof Request ? input.url : input.toString()).endsWith('/context')).length;
  const before = contextCalls();
  view.rerender(<Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="one" refreshKey={1} />);
  await waitFor(() => expect(contextCalls()).toBeGreaterThan(before));
  expect(screen.getByRole('textbox', { name: 'Message input' })).toBe(composer);
  expect((composer as HTMLTextAreaElement).value).toBe('Keep through refresh');
});

test('the role-row plus uses maintained new-thread navigation without activating the surrounding row', async () => {
  serve();
  const outer = vi.fn();
  function Harness() {
    const [target, setTarget] = useState<HTMLDivElement | null>(null);
    return <ConversationDraftsProvider organization="org"><div onClick={outer} role="group" aria-label="Agent role"><div ref={setTarget} /></div><Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="one" newThreadTarget={target} /></ConversationDraftsProvider>;
  }
  render(<Harness />);
  await screen.findByRole('region', { name: 'Thread information' });
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Keep the old draft' } });
  const plus = within(screen.getByRole('group', { name: 'Agent role' })).getByRole('button', { name: 'New thread' });
  fireEvent.click(plus);
  expect(outer).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'New Thread' })).toBeNull();
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Thread information' })).toBeNull());
  fireEvent.click(screen.getByRole('button', { name: 'Layout work' }));
  await waitFor(() => expect((screen.getByRole('textbox', { name: 'Message input' }) as HTMLTextAreaElement).value).toBe('Keep the old draft'));
});
