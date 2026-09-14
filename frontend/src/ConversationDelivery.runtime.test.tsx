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

  render(<ThreadNotificationsProvider organization="org" agents={[agent]} csrf="csrf-example" selectedAgent="junior"><ConversationDeliveryProvider organization="org" agent="junior" session="session-one" csrf="csrf-example" onOpen={open} readOnly><Conversation baseUrl={baseUrl} csrfToken="csrf-example" sessionId="session-one" showThreadList={false} readOnly /></ConversationDeliveryProvider></ThreadNotificationsProvider>);

  expect(await screen.findByText('Senior engineer')).toBeTruthy();
  expect(screen.queryByText('Investigate outcome')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Open source thread' }));
  expect(open).toHaveBeenCalledWith('senior', 'source-thread');
});
