import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Conversation } from './Conversation';
import { ConversationDeliveryProvider } from './ConversationDelivery';
import { ThreadNotificationsProvider } from './ThreadNotifications';
import type { Agent } from './workspace-api';

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

HTMLElement.prototype.scrollTo ??= () => {};

const agent = { id: 'junior', name: 'Junior' } as Agent;
const baseUrl = 'http://127.0.0.1:5175/api/organizations/org/agents/junior/opencode';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test('anchors peer source context to a rendered native user input', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  const open = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : url.endsWith('/opencode/question') || url.endsWith('/opencode/permission') ? []
        : url.endsWith('/agents/junior/sessions') ? [{ session_id: 'session-one' }]
          : url.endsWith('/sessions/session-one/dispatches') ? [{
            id: 'delivery', session_id: 'session-one', native_message_id: 'user-one', state: 'completed', updated_at: 1,
            author: { kind: 'agent', id: 'senior', name: 'Senior engineer', session_id: 'source-thread' },
            payload: { text: 'Peer context', mode: 'queued' },
            outcome: { kind: 'native_run_completed', message_id: 'assistant-one' },
          }]
            : url.includes('/experimental/session') ? [{ id: 'session-one', title: 'Delivery context', time: {} }]
              : url.includes('/session/session-one/message') ? [{
                info: { id: 'user-one', role: 'user', sessionID: 'session-one', time: { created: 1 } },
                parts: [{ id: 'text-one', sessionID: 'session-one', messageID: 'user-one', type: 'text', text: 'Peer context' }],
              }]
                : url.includes('/session/session-one') ? { id: 'session-one', title: 'Delivery context', time: {} }
                  : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));

  render(<ThreadNotificationsProvider organization="org" agents={[agent]} csrf="csrf-example" selectedAgent="junior"><ConversationDeliveryProvider organization="org" agent="junior" session="session-one" csrf="csrf-example" onOpen={open} viewerId="owner" readOnly><Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="session-one" showThreadList={false} readOnly /></ConversationDeliveryProvider></ThreadNotificationsProvider>);

  expect(await screen.findByText('Senior engineer')).toBeTruthy();
  expect(screen.queryByText('Investigate outcome')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Open source thread' }));
  expect(open).toHaveBeenCalledWith('senior', 'source-thread');
});

test('keeps adjacent collapsed activity compact through mixed delivery context and ordinary replies', async () => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    const body = url.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : url.endsWith('/opencode/question') || url.endsWith('/opencode/permission') ? []
        : url.endsWith('/agents/junior/sessions') ? [{ session_id: 'session-one' }]
          : url.endsWith('/sessions/session-one/dispatches') ? [{
            id: 'activity-delivery', session_id: 'session-one', state: 'completed', updated_at: 1,
            author: { kind: 'human', id: 'owner', name: 'Owner' },
            payload: { text: 'Inspect the workspace', mode: 'queued' },
            outcome: { kind: 'native_run_completed', message_id: 'tool-one' },
          }]
            : url.includes('/experimental/session') ? [{ id: 'session-one', title: 'Mixed activity', time: {} }]
              : url.includes('/session/session-one/message') ? [
                {
                  info: { id: 'reasoning-one', role: 'assistant', sessionID: 'session-one', parentID: 'user-zero', modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, finish: 'stop', time: { created: 1 } },
                  parts: [{ id: 'reasoning-part', sessionID: 'session-one', messageID: 'reasoning-one', type: 'reasoning', text: 'Inspect the workspace.' }],
                },
                {
                  info: { id: 'user-one', role: 'user', sessionID: 'session-one', parentID: 'reasoning-one', time: { created: 2 } },
                  parts: [{ id: 'user-part', sessionID: 'session-one', messageID: 'user-one', type: 'text', text: 'Continue.' }],
                },
                {
                  info: { id: 'tool-one', role: 'assistant', sessionID: 'session-one', parentID: 'user-one', modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, finish: 'stop', time: { created: 3 } },
                  parts: [{ id: 'tool-part', callID: 'tool-call', sessionID: 'session-one', messageID: 'tool-one', type: 'tool', tool: 'read', state: { status: 'completed', input: { description: 'Read the report' }, output: {} } }],
                },
                {
                  info: { id: 'user-two', role: 'user', sessionID: 'session-one', parentID: 'tool-one', time: { created: 4 } },
                  parts: [{ id: 'user-part-two', sessionID: 'session-one', messageID: 'user-two', type: 'text', text: 'Finish.' }],
                },
                {
                  info: { id: 'answer-one', role: 'assistant', sessionID: 'session-one', parentID: 'user-two', modelID: 'model', providerID: 'provider', mode: 'primary', path: { cwd: '/', root: '/' }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }, finish: 'stop', time: { created: 5 } },
                  parts: [{ id: 'answer-part', sessionID: 'session-one', messageID: 'answer-one', type: 'text', text: 'The report is ready.' }],
                },
              ]
                : url.includes('/session/session-one') ? { id: 'session-one', title: 'Mixed activity', time: {} }
                  : [];
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  }));

  const { container } = render(<ThreadNotificationsProvider organization="org" agents={[agent]} csrf="csrf-example" selectedAgent="junior"><ConversationDeliveryProvider organization="org" agent="junior" session="session-one" csrf="csrf-example" onOpen={vi.fn()} viewerId="owner"><Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="session-one" showThreadList={false} /></ConversationDeliveryProvider></ThreadNotificationsProvider>);

  expect(await screen.findByText('The report is ready.')).toBeTruthy();
  const activity = container.querySelectorAll('[data-activity-only="true"]');
  expect(activity).toHaveLength(2);
  expect(activity[1]?.querySelector('.delivery-context:empty')).toBeTruthy();
  expect(screen.getByText('The report is ready.').closest('[data-slot="aui_assistant-message-root"]')?.getAttribute('data-activity-only')).toBeNull();
});
