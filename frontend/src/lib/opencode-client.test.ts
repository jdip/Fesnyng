import { expect, test, vi } from 'vitest';
import { createFesnyngOpenCodeClient, createFesnyngOpenCodeFetch, createFesnyngOpenCodeThread } from './opencode-client';
import { WorkspaceCreationUncertain, WorkspacePreparationFailed } from '../workspace-api';

vi.mock('@assistant-ui/react-opencode', () => ({ createOpencodeClient: (options: unknown) => options }));

test('adds browser credentials, CSRF, and a distinct idempotency key to native writes', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
  const fetchWithFesnyngAuth = createFesnyngOpenCodeFetch('csrf-example', fetchMock);

  await fetchWithFesnyngAuth('/session/example/prompt_async', { method: 'POST' });
  await fetchWithFesnyngAuth('/session/example/abort', { method: 'POST' });

  expect(fetchMock).toHaveBeenCalledTimes(2);
  const first = fetchMock.mock.calls[0][1] as RequestInit;
  const second = fetchMock.mock.calls[1][1] as RequestInit;
  expect(first.credentials).toBe('include');
  expect(new Headers(first.headers).get('X-CSRF-Token')).toBe('csrf-example');
  expect(new Headers(first.headers).get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
  expect(new Headers(second.headers).get('Idempotency-Key')).not.toBe(
    new Headers(first.headers).get('Idempotency-Key'),
  );
});

test('does not invent idempotency for a native read', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
  const fetchWithFesnyngAuth = createFesnyngOpenCodeFetch('csrf-example', fetchMock);

  await fetchWithFesnyngAuth('/session/example/message');

  const request = fetchMock.mock.calls[0][1] as RequestInit;
  expect(request.credentials).toBe('include');
  expect(new Headers(request.headers).get('X-CSRF-Token')).toBeNull();
  expect(new Headers(request.headers).get('Idempotency-Key')).toBeNull();
});

test('hides frozen and Codex-bound roots from the writable OpenCode list', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([
    { id: 'active-opencode', runtime_type: 'opencode' },
    { id: 'old-opencode', runtime_type: 'opencode', frozen_at: 0 },
    { id: 'old-codex', runtime_type: 'codex', frozen: true },
    { id: 'legacy-active' },
  ])));
  const fetchWithFesnyngAuth = createFesnyngOpenCodeFetch('csrf-example', fetchMock);

  const response = await fetchWithFesnyngAuth('/experimental/session');

  await expect(response.json()).resolves.toEqual([
    { id: 'active-opencode', runtime_type: 'opencode' },
    { id: 'legacy-active' },
  ]);
});

test('preserves a caller-supplied idempotency key for a durable retry', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
  const fetchWithFesnyngAuth = createFesnyngOpenCodeFetch('csrf-example', fetchMock);

  await fetchWithFesnyngAuth('/session/example/prompt_async', {
    method: 'POST', headers: { 'Idempotency-Key': 'retained-admission-id' },
  });

  const request = fetchMock.mock.calls[0][1] as RequestInit;
  expect(new Headers(request.headers).get('Idempotency-Key')).toBe('retained-admission-id');
});

test('sends one stable Project workspace selection on an OpenCode creation retry', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'thread-two' })));
  vi.stubGlobal('fetch', fetchMock);
  const creation = { project_id: 'website', checkout_branch: 'release', creation_id: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4' };
  const client = createFesnyngOpenCodeClient('http://workspace.test/api/organizations/org/agents/agent/opencode', 'csrf-example', creation) as unknown as { fetch: (input: string, init: RequestInit) => Promise<Response> };

  await client.fetch('http://workspace.test/api/organizations/org/agents/agent/opencode/session', { method: 'POST', body: JSON.stringify({ title: 'New thread' }) });
  await client.fetch('http://workspace.test/api/organizations/org/agents/agent/opencode/session', { method: 'POST', body: JSON.stringify({ title: 'New thread' }) });

  expect(fetchMock.mock.calls.map(([, init]) => JSON.parse((init as RequestInit).body as string))).toEqual([
    { title: 'New thread', ...creation }, { title: 'New thread', ...creation },
  ]);
});

test('keeps a structured uncertain workspace outcome recoverable', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'workspace_creation_uncertain', creation_id: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4', detail: 'Check the existing preparation request.' } }), { status: 503 }));
  const fetchWithFesnyngAuth = createFesnyngOpenCodeFetch('csrf-example', fetchMock);

  await expect(fetchWithFesnyngAuth('/session', { method: 'POST' })).rejects.toMatchObject({
    message: 'Check the existing preparation request.', creationId: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4',
  });
  await expect(fetchWithFesnyngAuth('/session', { method: 'POST' })).rejects.toBeInstanceOf(WorkspaceCreationUncertain);
});

test('keeps a lost OpenCode creation response recoverable with its original identity', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network connection lost.')));
  const creation = { creation_id: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4' };

  await expect(createFesnyngOpenCodeThread('http://workspace.test/api/organizations/org/agents/agent/opencode', 'csrf-example', creation)).rejects.toMatchObject({
    creationId: creation.creation_id,
  });
});

test('permits a new intent only after an explicit pre-native OpenCode rejection', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: { code: 'workspace_preparation_failed', detail: 'The configured branch does not exist.' } }, { status: 422 })));

  await expect(createFesnyngOpenCodeThread('http://workspace.test/api/organizations/org/agents/agent/opencode', 'csrf-example', { creation_id: 'f6940d82-c96d-4e36-8dd5-ebd63a4999c4' })).rejects.toBeInstanceOf(WorkspacePreparationFailed);
});
