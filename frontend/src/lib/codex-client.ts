import type { ThreadAssistantMessage, ThreadMessage, ThreadUserMessage } from '@assistant-ui/react';
import type { ReadonlyJSONObject } from 'assistant-stream/utils';
import { WorkspaceCreationUncertain, WorkspacePreparationFailed, workspaceCreationFailure, type NativeThreadCreation } from '../workspace-api';
import { projectGroupingWarning, publishProjectGroupingWarning } from '../project-grouping-warning';

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
const WRITE_METHODS = new Set(['DELETE', 'PATCH', 'POST', 'PUT']);

async function nativeFailure(response: Response) {
  const body: unknown = await response.clone().json().catch(() => undefined);
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  return workspaceCreationFailure(detail, `Request failed (${response.status}).`);
}

/** Browser transport for the host-owned Codex facade and its durable receipts. */
export function createFesnyngCodexFetch(csrfToken: string, fetchImpl: FetchLike = fetch): FetchLike {
  return (input, init = {}) => {
    const method = (init.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    if (WRITE_METHODS.has(method)) {
      headers.set('X-CSRF-Token', csrfToken);
      if (!headers.has('Idempotency-Key')) headers.set('Idempotency-Key', crypto.randomUUID());
    }
    return fetchImpl(input, { ...init, credentials: 'include', headers }).then(async (response) => {
      if (!response.ok) throw await nativeFailure(response);
      return response;
    });
  };
}

/** Prepare one Codex thread before the conversation runtime opens it. */
export async function createFesnyngCodexThread(baseUrl: string, csrfToken: string, creation: NativeThreadCreation) {
  try {
    const response = await createFesnyngCodexFetch(csrfToken)(`${baseUrl.replace(/\/$/, '')}/session`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(creation),
    });
    const receipt: unknown = await response.json();
    if (!receipt || typeof receipt !== 'object' || typeof (receipt as Record<string, unknown>).id !== 'string') throw new Error('Codex thread receipt is invalid.');
    publishProjectGroupingWarning(receipt);
    return { id: (receipt as Record<string, string>).id, groupingWarning: projectGroupingWarning(receipt) };
  } catch (cause) {
    if (cause instanceof WorkspaceCreationUncertain || cause instanceof WorkspacePreparationFailed) throw cause;
    throw new WorkspaceCreationUncertain('Thread preparation may have started. Check this preparation again to recover its result.', creation.creation_id);
  }
}

export type CodexHistory = {
  thread: Record<string, unknown>;
  turns: Array<Record<string, unknown>>;
  historyState?: 'complete' | 'unavailable';
  historyReason?: string;
};
export type CodexEvent = { method: string; params: Record<string, unknown> };

const record = (value: unknown): Record<string, unknown> | undefined => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
);

const text = (value: unknown): string => {
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return '';
  return value.map((part) => {
    if (typeof part === 'string') return part;
    const entry = record(part);
    return entry && typeof entry.text === 'string' ? entry.text : '';
  }).join('');
};

const createdAt = (item: Record<string, unknown>) => {
  const value = item.createdAt ?? item.created_at;
  return typeof value === 'number' ? new Date(value) : new Date(0);
};

const running = (turn: Record<string, unknown>) => {
  const nested = record(turn.status);
  const status = nested?.type ?? turn.status;
  return status === 'inProgress' || status === 'in_progress' || status === 'running';
};

const assistantStatus = (turn: Record<string, unknown>): ThreadAssistantMessage['status'] => (
  running(turn) ? { type: 'running' }
    : turn.status === 'interrupted' ? { type: 'incomplete', reason: 'cancelled' }
      : turn.status === 'failed' ? { type: 'incomplete', reason: 'error', error: typeof turn.error === 'string' ? turn.error : 'Codex turn failed' }
        : { type: 'complete', reason: 'stop' }
);

const custom = (turnId: string, item: Record<string, unknown>) => ({ codex: { turnId, item } });

const user = (turnId: string, item: Record<string, unknown>): ThreadUserMessage => ({
  id: typeof item.id === 'string' ? item.id : `${turnId}:user`,
  role: 'user',
  createdAt: createdAt(item),
  content: [{ type: 'text', text: text(item.content ?? item.text) }],
  attachments: [],
  metadata: { custom: custom(turnId, item) },
});

const assistant = (turnId: string, turn: Record<string, unknown>, item: Record<string, unknown>): ThreadAssistantMessage => ({
  id: typeof item.id === 'string' ? item.id : `${turnId}:agent`,
  role: 'assistant',
  createdAt: createdAt(item),
  content: [{ type: 'text', text: text(item.text ?? item.content) }],
  status: assistantStatus(turn),
  metadata: { unstable_state: {}, unstable_annotations: [], unstable_data: [], steps: [], custom: custom(turnId, item) },
});

const reasoning = (turnId: string, turn: Record<string, unknown>, item: Record<string, unknown>): ThreadAssistantMessage => ({
  id: typeof item.id === 'string' ? item.id : `${turnId}:reasoning`,
  role: 'assistant',
  createdAt: createdAt(item),
  content: [{ type: 'reasoning', text: text(item.text ?? item.summary ?? item.content), status: running(turn) ? { type: 'running' } : { type: 'complete' } }],
  status: assistantStatus(turn),
  metadata: { unstable_state: {}, unstable_annotations: [], unstable_data: [], steps: [], custom: custom(turnId, item) },
});

const tool = (turnId: string, turn: Record<string, unknown>, item: Record<string, unknown>, name: string, args: Record<string, unknown>, result: unknown): ThreadAssistantMessage => ({
  id: typeof item.id === 'string' ? item.id : `${turnId}:${name}`,
  role: 'assistant',
  createdAt: createdAt(item),
  content: [{
    type: 'tool-call',
    toolCallId: typeof item.id === 'string' ? item.id : `${turnId}:${name}`,
    toolName: name,
    args: args as ReadonlyJSONObject,
    argsText: JSON.stringify(args),
    ...(result === undefined ? {} : { result }),
    ...((turn.status === 'failed' || item.status === 'failed' || (typeof item.exitCode === 'number' && item.exitCode !== 0)) ? { isError: true } : {}),
  }],
  status: assistantStatus(turn),
  metadata: { unstable_state: {}, unstable_annotations: [], unstable_data: [], steps: [], custom: custom(turnId, item) },
});

/**
 * Turn the App Server's native thread/read receipt into maintained assistant-ui
 * messages. Raw item provenance stays attached for native renderers and later
 * archive capture; this adapter does not reinterpret Codex execution.
 */
export function projectCodexHistory(history: CodexHistory): ThreadMessage[] {
  const messages: ThreadMessage[] = [];
  for (const turn of history.turns) {
    const turnId = typeof turn.id === 'string' ? turn.id : 'turn';
    const items = Array.isArray(turn.items) ? turn.items : [];
    for (const value of items) {
      const item = record(value);
      if (!item) continue;
      switch (item.type) {
        case 'userMessage': messages.push(user(turnId, item)); break;
        case 'agentMessage': messages.push(assistant(turnId, turn, item)); break;
        case 'reasoning': messages.push(reasoning(turnId, turn, item)); break;
        case 'commandExecution': messages.push(tool(turnId, turn, item, 'command_execution', { command: item.command }, item.aggregatedOutput ?? item.output)); break;
        case 'fileChange': messages.push(tool(turnId, turn, item, 'apply_patch', { changes: Array.isArray(item.changes) ? item.changes : [], diff: item.diff }, item.result)); break;
        case 'mcpToolCall': messages.push(tool(turnId, turn, item, typeof item.tool === 'string' ? item.tool : 'mcp_tool', record(item.arguments) ?? {}, item.result)); break;
        default: {
          if (typeof item.type === 'string') messages.push(tool(turnId, turn, item, item.type, record(item.arguments) ?? {}, item.result ?? item.output));
        }
      }
    }
  }
  const sessionId = typeof history.thread.id === 'string' ? history.thread.id : undefined;
  return messages.map((message) => {
    const native = record(message.metadata.custom.codex);
    return native && sessionId ? {
      ...message,
      metadata: { ...message.metadata, custom: { ...message.metadata.custom, codex: { ...native, sessionId } } },
    } as ThreadMessage : message;
  });
}

const turnId = (params: Record<string, unknown>) => typeof params.turnId === 'string' ? params.turnId : undefined;
const itemId = (params: Record<string, unknown>) => typeof params.itemId === 'string' ? params.itemId : undefined;
const replaceTurn = (history: CodexHistory, updated: Record<string, unknown>) => {
  const id = typeof updated.id === 'string' ? updated.id : undefined;
  return id ? { ...history, turns: history.turns.map((turn) => turn.id === id ? { ...turn, ...updated, items: Array.isArray(updated.items) && updated.items.length > 0 ? updated.items : turn.items } : turn) } : history;
};
const updateItem = (history: CodexHistory, id: string, update: (item: Record<string, unknown>) => Record<string, unknown>) => ({
  ...history,
  turns: history.turns.map((turn) => Array.isArray(turn.items) ? { ...turn, items: turn.items.map((value) => {
    const item = record(value);
    return item?.id === id ? update(item) : value;
  }) } : turn),
});

/** Applies an App Server notification without refetching on each streamed token. */
export function applyCodexEvent(history: CodexHistory, event: CodexEvent): CodexHistory {
  const params = event.params;
  const scopedThread = typeof params.threadId === 'string' ? params.threadId : undefined;
  if (!scopedThread || scopedThread !== history.thread.id) return history;
  const turn = record(params.turn);
  const item = record(params.item);
  if (event.method === 'turn/started' && turn) {
    const id = typeof turn.id === 'string' ? turn.id : undefined;
    return id && history.turns.some((candidate) => candidate.id === id) ? replaceTurn(history, turn) : { ...history, turns: [...history.turns, turn] };
  }
  if (event.method === 'turn/completed' && turn) return replaceTurn(history, turn);
  if (event.method === 'item/started' && item) {
    const id = turnId(params);
    return id ? { ...history, turns: history.turns.map((turn) => turn.id === id ? { ...turn, items: Array.isArray(turn.items) && turn.items.some((candidate) => record(candidate)?.id === item.id) ? turn.items.map((candidate) => record(candidate)?.id === item.id ? item : candidate) : [...(Array.isArray(turn.items) ? turn.items : []), item] } : turn) } : history;
  }
  if (event.method === 'item/completed' && item) {
    const id = typeof item.id === 'string' ? item.id : undefined;
    return id ? updateItem(history, id, () => item) : history;
  }
  const id = itemId(params);
  if (!id) return history;
  if (event.method === 'item/agentMessage/delta' && typeof params.delta === 'string') return updateItem(history, id, (item) => ({ ...item, text: `${typeof item.text === 'string' ? item.text : ''}${params.delta}` }));
  if (event.method === 'item/commandExecution/outputDelta' && typeof params.delta === 'string') return updateItem(history, id, (item) => ({ ...item, aggregatedOutput: `${typeof item.aggregatedOutput === 'string' ? item.aggregatedOutput : ''}${params.delta}` }));
  if (event.method === 'item/fileChange/patchUpdated' && Array.isArray(params.changes)) return updateItem(history, id, (item) => ({ ...item, changes: params.changes }));
  if (event.method === 'item/reasoning/summaryTextDelta' && typeof params.delta === 'string') return updateItem(history, id, (item) => ({ ...item, summary: [...(Array.isArray(item.summary) ? item.summary : []), params.delta] }));
  if (event.method === 'item/reasoning/textDelta' && typeof params.delta === 'string') return updateItem(history, id, (item) => ({ ...item, text: `${typeof item.text === 'string' ? item.text : ''}${params.delta}` }));
  return history;
}
