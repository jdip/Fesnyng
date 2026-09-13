import { expect, test } from 'vitest';
import { OpenCodeThreadController, type OpenCodeServerEvent } from '@assistant-ui/react-opencode';

type PendingRequest = { id: string; sessionID: string; permission?: string; patterns?: string[] };

function createController() {
  let permissions: PendingRequest[] = [];
  let questions: PendingRequest[] = [];
  let permissionFailure: Error | undefined;
  let pendingQuestionList: Promise<{ data: PendingRequest[] }> | undefined;
  let resolveQuestionList: ((response: { data: PendingRequest[] }) => void) | undefined;
  let eventListener: ((event: OpenCodeServerEvent) => void) | undefined;
  const client = {
    session: {
      get: async () => ({ data: { id: 'session-one' } }),
      messages: async () => ({ data: [] }),
      status: async () => ({ data: { 'session-one': { type: 'idle' } } }),
    },
    permission: {
      list: async () => {
        if (permissionFailure) throw permissionFailure;
        return { data: permissions };
      },
    },
    question: {
      list: async () => pendingQuestionList ?? { data: questions },
    },
  };
  const controller = new OpenCodeThreadController(client as never, () => ({
    subscribe(listener) {
      eventListener = listener;
      return () => { eventListener = undefined; };
    },
  }), 'session-one');

  return {
    controller,
    setPermissions: (next: PendingRequest[]) => { permissions = next; },
    setQuestions: (next: PendingRequest[]) => { questions = next; },
    failPermissions: (error: Error | undefined) => { permissionFailure = error; },
    deferQuestionList: () => {
      pendingQuestionList = new Promise((resolve) => { resolveQuestionList = resolve; });
    },
    resolveQuestionList: (snapshot: PendingRequest[]) => {
      resolveQuestionList?.({ data: snapshot });
      pendingQuestionList = undefined;
      resolveQuestionList = undefined;
    },
    emitQuestionReplied: (requestID: string) => {
      eventListener?.({
        type: 'question.replied',
        sessionId: 'session-one',
        properties: { requestID, answers: [['answered elsewhere']] },
      } as unknown as OpenCodeServerEvent);
    },
    emitQuestionAsked: (request: PendingRequest) => {
      eventListener?.({
        type: 'question.asked',
        sessionId: 'session-one',
        properties: request,
      } as unknown as OpenCodeServerEvent);
    },
    reconnect: async () => {
      eventListener?.({ type: 'stream.reconnected' } as OpenCodeServerEvent);
      await Promise.resolve();
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
    },
  };
}

test('loads the authoritative pending requests for the attached session', async () => {
  const fixture = createController();
  fixture.setPermissions([
    { id: 'permission-current', sessionID: 'session-one', permission: 'bash', patterns: [] },
    { id: 'permission-other', sessionID: 'session-other', permission: 'bash', patterns: [] },
  ]);
  fixture.setQuestions([
    { id: 'question-current', sessionID: 'session-one' },
    { id: 'question-other', sessionID: 'session-other' },
  ]);

  await fixture.controller.load();

  expect(Object.keys(fixture.controller.getState().interactions.permissions.pending)).toEqual([
    'permission-current',
  ]);
  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([
    'question-current',
  ]);
});

test('reconnection removes requests answered elsewhere without clearing another request kind', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  fixture.setPermissions([{ id: 'permission-current', sessionID: 'session-one', permission: 'bash', patterns: [] }]);
  fixture.setQuestions([{ id: 'question-current', sessionID: 'session-one' }]);
  await fixture.reconnect();

  fixture.setPermissions([]);
  await fixture.reconnect();

  expect(Object.keys(fixture.controller.getState().interactions.permissions.pending)).toEqual([]);
  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([
    'question-current',
  ]);
});

test('preserves a failed interaction kind while reconciling the other kind', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  fixture.setPermissions([{ id: 'permission-current', sessionID: 'session-one', permission: 'bash', patterns: [] }]);
  fixture.setQuestions([{ id: 'question-current', sessionID: 'session-one' }]);
  await fixture.controller.load();

  fixture.failPermissions(new Error('permission endpoint unavailable'));
  fixture.setQuestions([]);
  await fixture.reconnect();

  expect(Object.keys(fixture.controller.getState().interactions.permissions.pending)).toEqual([
    'permission-current',
  ]);
  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([]);
});

test('does not resurrect a question from a snapshot superseded by an SSE reply', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  const question = { id: 'question-current', sessionID: 'session-one' };
  fixture.setQuestions([question]);
  await fixture.controller.load();

  fixture.deferQuestionList();
  await fixture.reconnect();
  fixture.emitQuestionReplied(question.id);
  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([]);
  fixture.resolveQuestionList([question]);
  await Promise.resolve();
  await Promise.resolve();

  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([]);
});

test('does not remove an SSE question from a snapshot that started earlier', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  const question = { id: 'question-current', sessionID: 'session-one' };

  fixture.deferQuestionList();
  await fixture.reconnect();
  fixture.emitQuestionAsked(question);
  fixture.resolveQuestionList([]);
  await Promise.resolve();
  await Promise.resolve();

  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([
    'question-current',
  ]);
});

test('merges a snapshot request with a different SSE question that arrives while loading', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  const loadingQuestion = { id: 'question-one', sessionID: 'session-one' };
  const streamedQuestion = { id: 'question-two', sessionID: 'session-one' };
  fixture.deferQuestionList();

  const loading = fixture.controller.load();
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.emitQuestionAsked(streamedQuestion);
  fixture.resolveQuestionList([loadingQuestion]);
  await loading;

  expect(Object.keys(fixture.controller.getState().interactions.questions.pending).sort()).toEqual([
    'question-one',
    'question-two',
  ]);
});

test('reconnect overlays an SSE answer and a different SSE question onto its stale snapshot', async () => {
  const fixture = createController();
  fixture.controller.subscribe(() => undefined);
  const answeredQuestion = { id: 'question-one', sessionID: 'session-one' };
  const streamedQuestion = { id: 'question-two', sessionID: 'session-one' };
  fixture.setQuestions([answeredQuestion]);
  await fixture.controller.load();

  fixture.deferQuestionList();
  await fixture.reconnect();
  fixture.emitQuestionReplied(answeredQuestion.id);
  fixture.emitQuestionAsked(streamedQuestion);
  fixture.resolveQuestionList([answeredQuestion]);
  await Promise.resolve();
  await Promise.resolve();

  expect(Object.keys(fixture.controller.getState().interactions.questions.pending)).toEqual([
    'question-two',
  ]);
});
