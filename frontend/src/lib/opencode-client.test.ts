import { expect, test, vi } from 'vitest';
import { createFesnyngOpenCodeFetch } from './opencode-client';

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
