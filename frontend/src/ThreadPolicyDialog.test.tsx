import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThreadPolicyDialog } from './ThreadPolicyDialog';

const baseUrl = 'http://workspace.test/api/organizations/org/agents/agent/opencode';
const session = { id: 'thread-other', title: 'Other investigation' };
const policy = { desired_revision: 3, applied_revision: 2, rules: [{ permission: 'bash', pattern: '*', action: 'ask' as const }], effective_rules: [{ permission: 'read', pattern: '*', action: 'allow' as const }] };

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('opens the selected thread policy through the agent facade and restores menu focus on close', async () => {
  const request = vi.fn(async () => response(policy));
  vi.stubGlobal('fetch', request);
  const close = vi.fn();
  const restore = vi.fn();
  render(<ThreadPolicyDialog baseUrl={baseUrl} csrfToken="csrf-example" session={session} onClose={close} onRestoreFocus={restore} />);
  expect(screen.getByRole('dialog', { name: 'Thread permissions' })).toBeTruthy();
  expect(await screen.findByDisplayValue('bash')).toBeTruthy();
  expect(screen.getByText(/Desired revision 3 · Applied 2/)).toBeTruthy();
  expect(request).toHaveBeenCalledWith('http://workspace.test/api/organizations/org/agents/agent/sessions/thread-other/policy', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(restore).toHaveBeenCalledOnce());
  expect(close).toHaveBeenCalledOnce();
});

test('saves the selected revision and keeps edited rules after a policy conflict', async () => {
  const request = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
    ? response({ detail: 'Thread policy revision conflict' }, 409)
    : response(policy));
  vi.stubGlobal('fetch', request);
  render(<ThreadPolicyDialog baseUrl={baseUrl} csrfToken="csrf-example" session={session} onClose={vi.fn()} />);
  const permission = await screen.findByDisplayValue('bash');
  fireEvent.change(permission, { target: { value: 'git' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save thread permissions' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Thread policy revision conflict');
  expect(screen.getByDisplayValue('git')).toBeTruthy();
  const write = request.mock.calls.find(([, init]) => init?.method === 'PUT')?.[1];
  expect(write).toBeDefined();
  if (!write) throw new Error('Expected a policy write.');
  expect(write.body).toBe(JSON.stringify({ expected_revision: 3, rules: [{ permission: 'git', pattern: '*', action: 'ask' }] }));
  expect(new Headers(write.headers).get('X-CSRF-Token')).toBe('csrf-example');
});

test('renders the server-returned revision and effective rules after a successful save', async () => {
  const saved = { desired_revision: 4, applied_revision: 4, rules: [{ permission: 'git', pattern: '*', action: 'allow' as const }], effective_rules: [{ permission: 'read', pattern: 'src/**', action: 'allow' as const }] };
  vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(init?.method === 'PUT' ? saved : policy)));
  render(<ThreadPolicyDialog baseUrl={baseUrl} csrfToken="csrf-example" session={session} onClose={vi.fn()} />);
  fireEvent.change(await screen.findByDisplayValue('bash'), { target: { value: 'git' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save thread permissions' }));
  expect(await screen.findByText(/Desired revision 4 · Applied 4/)).toBeTruthy();
  fireEvent.click(screen.getByText('Effective permissions'));
  expect(screen.getByText('read · src/** · allow')).toBeTruthy();
});
