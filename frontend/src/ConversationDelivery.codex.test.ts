import { expect, test } from 'vitest';
import { matchingDeliveries, nativeMessage } from './ConversationDelivery';
import type { Delivery } from './ThreadNotifications';

test('matches the durable Codex client message ID to its host-owned thread', () => {
  const native = nativeMessage({
    id: 'rendered-user-message',
    role: 'user',
    metadata: { custom: { codex: { sessionId: 'thread-one', item: { id: 'native-user-message', clientId: 'receipt-one', type: 'userMessage' } } } },
  });

  expect(native.session).toBe('thread-one');
  expect(native.ids).toEqual(new Set(['rendered-user-message', 'native-user-message']));
  expect(native.clientIds).toEqual(new Set(['receipt-one']));
  const delivery = { id: 'receipt-one', session_id: 'thread-one', state: 'completed', author: { kind: 'human', id: 'owner', name: 'Owner' }, payload: { text: 'Review', mode: 'queued' }, updated_at: 1 } satisfies Delivery;
  expect(matchingDeliveries([delivery], native)).toEqual([delivery]);
});


test('matches a Codex result by its native turn rather than an unrelated rendered item', () => {
  const delivery = { id: 'receipt-one', session_id: 'thread-one', state: 'completed', author: { kind: 'human', id: 'owner', name: 'Owner' }, payload: { text: 'Review', mode: 'queued' }, updated_at: 1, outcome: { kind: 'codex_turn_completed', turn_id: 'turn-one' } } satisfies Delivery;
  const native = nativeMessage({ role: 'assistant', metadata: { custom: { codex: { sessionId: 'thread-one', turnId: 'turn-one', item: { id: 'unrelated-item' } } } } });
  expect(matchingDeliveries([delivery], native)).toEqual([delivery]);
});
