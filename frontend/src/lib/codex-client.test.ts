import { expect, test } from 'vitest';
import { applyCodexEvent, projectCodexHistory } from './codex-client';

test('projects native Codex turns into maintained text and diff tool messages', () => {
  const messages = projectCodexHistory({
    thread: { id: 'thread-one', title: 'Investigate' },
    turns: [{ id: 'turn-one', status: 'inProgress', items: [
      { id: 'user-one', type: 'userMessage', content: [{ type: 'text', text: 'Inspect the change.' }] },
      { id: 'agent-one', type: 'agentMessage', text: 'I found the relevant file.' },
      { id: 'diff-one', type: 'fileChange', changes: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@' }] },
    ] }],
  });

  expect(messages).toHaveLength(3);
  expect(messages[0]).toMatchObject({ id: 'user-one', role: 'user', content: [{ type: 'text', text: 'Inspect the change.' }] });
  expect(messages[1]).toMatchObject({ id: 'agent-one', role: 'assistant', content: [{ type: 'text', text: 'I found the relevant file.' }], status: { type: 'running' } });
  expect(messages[2]).toMatchObject({
    id: 'diff-one',
    role: 'assistant',
    content: [{ type: 'tool-call', toolCallId: 'diff-one', toolName: 'apply_patch', args: { changes: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@' }] } }],
    metadata: { custom: { codex: { turnId: 'turn-one', item: { id: 'diff-one', type: 'fileChange', changes: [{ path: 'src/app.ts', diff: '@@ -1 +1 @@' }] } } } },
  });
});

test('keeps completed native tool output readable after a turn completes', () => {
  const [message] = projectCodexHistory({
    thread: { id: 'thread-one' },
    turns: [{ id: 'turn-one', status: 'completed', items: [
      { id: 'command-one', type: 'commandExecution', command: 'git status', aggregatedOutput: 'working tree clean', exitCode: 0 },
    ] }],
  });

  expect(message).toMatchObject({
    status: { type: 'complete', reason: 'stop' },
    content: [{ type: 'tool-call', toolName: 'command_execution', args: { command: 'git status' }, result: 'working tree clean' }],
  });
});

test('applies an agent message delta before the final native receipt', () => {
  const streamed = applyCodexEvent({
    thread: { id: 'thread-one' },
    turns: [{ id: 'turn-one', status: 'inProgress', items: [{ id: 'message-one', type: 'agentMessage', text: 'Hello' }] }],
  }, { method: 'item/agentMessage/delta', params: { threadId: 'thread-one', turnId: 'turn-one', itemId: 'message-one', delta: ' world' } });

  expect(projectCodexHistory(streamed)).toMatchObject([{ id: 'message-one', content: [{ type: 'text', text: 'Hello world' }], status: { type: 'running' } }]);
});

test('keeps the active thread isolated from another Codex thread event', () => {
  const history = { thread: { id: 'thread-one' }, turns: [{ id: 'turn-one', status: 'inProgress', items: [] }] };
  const next = applyCodexEvent(history, { method: 'turn/started', params: { threadId: 'thread-two', turn: { id: 'turn-two', status: 'inProgress', items: [] } } });
  expect(next).toEqual(history);
});

test('renders native reasoning summaries and failed command output accurately', () => {
  const messages = projectCodexHistory({ thread: { id: 'thread-one' }, turns: [{ id: 'turn-one', status: 'failed', items: [
    { id: 'reasoning-one', type: 'reasoning', summary: ['Checked ', 'the logs.'] },
    { id: 'command-one', type: 'commandExecution', command: 'false', aggregatedOutput: 'failed', exitCode: 1, status: 'failed' },
  ] }] });
  expect(messages[0]).toMatchObject({ content: [{ type: 'reasoning', text: 'Checked the logs.' }], status: { type: 'incomplete', reason: 'error' } });
  expect(messages[1]).toMatchObject({ content: [{ type: 'tool-call', isError: true, result: 'failed' }], status: { type: 'incomplete', reason: 'error' } });
});
