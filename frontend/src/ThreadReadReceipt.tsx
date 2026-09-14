import { useAuiState } from '@assistant-ui/react';
import { resultIdentity, useThreadNotifications } from './ThreadNotifications';
import { ViewedContent } from './ViewedContent';

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object';

/** Match the rendered native response, including responses merged into one assistant message. */
export function ThreadReadReceipt() {
  const notifications = useThreadNotifications();
  const message = useAuiState((state) => state.message);
  const loading = useAuiState((state) => state.thread.isLoading);
  const messages = useAuiState((state) => state.thread.messages);
  const opencode = message.metadata.custom.opencode;
  const codex = message.metadata.custom.codex;
  if (!notifications?.selectedAgent || loading || message.role !== 'assistant' || message.status?.type !== 'complete') return null;
  const agent = notifications.selectedAgent;
  const original = record(opencode) && record(opencode.originalMessage) ? opencode.originalMessage : undefined;
  const session = typeof original?.sessionID === 'string' ? original.sessionID : record(codex) && typeof codex.sessionId === 'string' ? codex.sessionId : undefined;
  if (!session) return null;
  const ids = new Set([message.id]);
  if (record(opencode) && Array.isArray(opencode.parts)) {
    for (const part of opencode.parts) if (record(part) && typeof part.messageID === 'string') ids.add(part.messageID);
  }
  const turnId = record(codex) && typeof codex.turnId === 'string' ? codex.turnId : undefined;
  const codexItem = record(codex) && record(codex.item) ? codex.item : undefined;
  const finalCodexResponse = turnId && codexItem?.type === 'agentMessage'
    ? [...messages].reverse().find((candidate) => {
      if (!record(candidate) || candidate.role !== 'assistant') return false;
      const metadata = record(candidate.metadata) ? candidate.metadata : undefined;
      const custom = metadata && record(metadata.custom) ? metadata.custom : undefined;
      const native = custom && record(custom.codex) ? custom.codex : undefined;
      const item = native && record(native.item) ? native.item : undefined;
      return native?.turnId === turnId && item?.type === 'agentMessage';
    })
    : undefined;
  if (turnId && finalCodexResponse && finalCodexResponse.id !== message.id) return null;
  if (turnId && !finalCodexResponse) return null;
  const receipts = notifications.receipts[agent]?.[session] ?? [];
  return <>{receipts.filter((receipt) => receipt.state === 'completed' && (
    (receipt.outcome?.kind === 'native_run_completed' && ids.has(receipt.outcome.message_id ?? ''))
    || (receipt.outcome?.kind === 'codex_turn_completed' && receipt.outcome.turn_id === turnId)
  )).map((receipt) => (
    <ViewedContent key={receipt.id} identity={`${receipt.id}:${resultIdentity(receipt)}`}
      onView={() => notifications.acknowledge(agent, session, receipt, 'read')} />
  ))}</>;
}
