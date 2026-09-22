import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CodexModelSettings } from './CodexModelSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const configuration = { execution_type: 'docker' as const, runtime_type: 'codex' as const, provider: 'openai' as const, model: 'current-model', profile_id: 'profile', workspace: 'default', instructions: '', skills: [] };
const models = { data: [
  { model: 'current-model', displayName: 'Current model', defaultReasoningEffort: 'medium', supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: 'Balanced' }] },
  { model: 'account-model', displayName: 'Account model', defaultReasoningEffort: 'low', supportedReasoningEfforts: [{ reasoningEffort: 'low', description: 'Quick' }, { reasoningEffort: 'high', description: 'Thorough' }] },
], nextCursor: null };

test('lists account models and only supported thinking levels', async () => {
  const request = vi.fn(async () => new Response(JSON.stringify(models)));
  vi.stubGlobal('fetch', request);
  const changed = vi.fn();
  const { rerender } = render(<CodexModelSettings organization="org" host="host" configuration={configuration} onChange={changed} />);
  await screen.findByRole('option', { name: 'Account model' });
  expect(request.mock.calls[0]).toEqual(expect.arrayContaining(['/api/organizations/org/hosts/host/profiles/profile/codex/models']));
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'account-model' } });
  expect(changed).toHaveBeenLastCalledWith({ ...configuration, model: 'account-model', reasoning_effort: null });
  rerender(<CodexModelSettings organization="org" host="host" configuration={{ ...configuration, model: 'account-model' }} onChange={changed} />);
  expect(screen.getByRole('option', { name: 'high — Thorough' })).toBeTruthy();
  expect(screen.queryByRole('option', { name: 'medium — Balanced' })).toBeNull();
  fireEvent.change(screen.getByLabelText('Default thinking level'), { target: { value: 'high' } });
  expect(changed).toHaveBeenLastCalledWith({ ...configuration, model: 'account-model', reasoning_effort: 'high' });
});

test('discards a late catalog after the credential profile changes', async () => {
  let finishOld!: (response: Response) => void;
  const request = vi.fn((url: string) => url.includes('/profile/') ? new Promise<Response>((resolve) => { finishOld = resolve; }) : Promise.resolve(new Response(JSON.stringify({ data: [{ ...models.data[1], model: 'other-account', displayName: 'Other account model' }] }))));
  vi.stubGlobal('fetch', request);
  const { rerender } = render(<CodexModelSettings organization="org" host="host" configuration={configuration} onChange={vi.fn()} />);
  rerender(<CodexModelSettings organization="org" host="host" configuration={{ ...configuration, profile_id: 'other-profile' }} onChange={vi.fn()} />);
  await screen.findByRole('option', { name: 'Other account model' });
  finishOld(new Response(JSON.stringify(models)));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('option', { name: 'Account model' })).toBeNull();
  expect(screen.getByLabelText('Model')).toHaveProperty('value', 'current-model');
});

test('shows discovery errors and retries without losing the saved configuration', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Profile needs authentication.' }), { status: 409 })).mockResolvedValueOnce(new Response(JSON.stringify(models))));
  const changed = vi.fn();
  render(<CodexModelSettings organization="org" host="host" configuration={configuration} onChange={changed} />);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Profile needs authentication.'));
  expect(screen.getByLabelText('Model')).toHaveProperty('value', 'current-model');
  fireEvent.click(screen.getByRole('button', { name: 'Retry model discovery' }));
  await screen.findByRole('option', { name: 'Account model' });
  expect(changed).not.toHaveBeenCalled();
});

test('requires an explicit supported choice when a saved thinking level is incompatible', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(models))));
  render(<CodexModelSettings organization="org" host="host" configuration={{ ...configuration, reasoning_effort: 'high' }} onChange={vi.fn()} />);
  await screen.findByRole('option', { name: 'Current model' });
  await waitFor(() => expect(screen.getByLabelText('Default thinking level')).toHaveProperty('validationMessage', 'Choose a supported thinking level or Model default.'));
  expect(screen.getByLabelText('Default thinking level')).toHaveProperty('value', 'high');
});

test('does not query without a profile and preserves the selected model', () => {
  const request = vi.fn();
  vi.stubGlobal('fetch', request);
  render(<CodexModelSettings organization="org" host="host" configuration={{ ...configuration, profile_id: null }} onChange={vi.fn()} />);
  expect(request).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Model')).toHaveProperty('value', 'current-model');
});
