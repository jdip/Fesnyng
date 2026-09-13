import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Conversation } from './Conversation';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
HTMLElement.prototype.scrollTo ??= () => {};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('opens permissions for a non-selected thread without replacing the conversation or its draft', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.endsWith('/sessions/session-other/policy') ? { desired_revision: 2, applied_revision: 2, rules: [], effective_rules: [] } : url.includes('/experimental/session') ? [
      { id: 'session-active', title: 'Active conversation', time: {} },
      { id: 'session-other', title: 'Other workspace', time: {} },
    ] : url.includes('/session/session-active/message') ? [{
      info: { id: 'message-one', role: 'user', sessionID: 'session-active', time: { created: 1 } },
      parts: [{ id: 'part-one', messageID: 'message-one', sessionID: 'session-active', type: 'text', text: 'Keep this conversation visible.' }],
    }] : url.endsWith('/session/session-active') ? { id: 'session-active', title: 'Active conversation', time: {} } : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));
  const selected = vi.fn();
  render(<Conversation baseUrl="http://localhost/api/organizations/org/agents/agent/opencode" csrfToken="csrf-example" sessionId="session-active" onSessionChange={selected} />);
  await screen.findByText('Keep this conversation visible.');
  const composer = screen.getByRole('textbox', { name: 'Message input' });
  fireEvent.change(composer, { target: { value: 'Unsent draft' } });
  await screen.findByRole('button', { name: 'Other workspace' });
  const menu = screen.getAllByRole('button', { name: 'More options' })[1]!;
  fireEvent.keyDown(menu, { key: 'Enter' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Thread permissions' }));
  expect(await screen.findByRole('dialog', { name: 'Thread permissions' })).toBeTruthy();
  expect(await screen.findByText(/Desired revision 2/)).toBeTruthy();
  expect(screen.getByText('Keep this conversation visible.')).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Message input', hidden: true })).toBe(composer);
  expect((composer as HTMLTextAreaElement).value).toBe('Unsent draft');
  expect(selected.mock.calls.some(([id]) => id === 'session-other')).toBe(false);
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  await waitFor(() => expect(document.activeElement).toBe(menu));
  expect((composer as HTMLTextAreaElement).value).toBe('Unsent draft');
});
