import { useAuiState } from '@assistant-ui/react';
import { resultIdentity, useThreadNotifications } from './ThreadNotifications';
import { ViewedContent } from './ViewedContent';

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object';

/** Match the rendered native response, including responses merged into one assistant message. */
export function ThreadReadReceipt() {
  const notifications = useThreadNotifications();
  const message = useAuiState((state) => state.message);
  const loading = useAuiState((state) => state.thread.isLoading);
  const native = message.metadata.custom.opencode;
  if (!notifications?.selectedAgent || loading || message.role !== 'assistant'
    || message.status?.type !== 'complete' || !record(native) || !record(native.originalMessage)
    || typeof native.originalMessage.sessionID !== 'string') return null;
  const agent = notifications.selectedAgent;
  const session = native.originalMessage.sessionID;
  const ids = new Set([message.id]);
  if (Array.isArray(native.parts)) {
    for (const part of native.parts) if (record(part) && typeof part.messageID === 'string') ids.add(part.messageID);
  }
  const receipts = notifications.receipts[agent]?.[session] ?? [];
  return <>{receipts.filter((receipt) => receipt.state === 'completed'
    && receipt.outcome?.kind === 'native_run_completed' && ids.has(receipt.outcome.message_id ?? '')).map((receipt) => (
    <ViewedContent key={receipt.id} identity={`${receipt.id}:${resultIdentity(receipt)}`}
      onView={() => notifications.acknowledge(agent, session, receipt, 'read')} />
  ))}</>;
}
