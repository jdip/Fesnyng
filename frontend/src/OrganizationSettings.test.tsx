import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { OrganizationSettings } from './OrganizationSettings';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('adds a member using authenticated organization scope and clears the password after success', async () => {
  const request = vi.fn().mockImplementation(async (url: string, options: RequestInit) => new Response(JSON.stringify(options.method === 'PUT' ? {user_id: 'new-member'} : url.endsWith('/policy') ? { desired_version: 1, configuration: { default_permission: 'allow', mandatory_permissions: [], allow_thread_overrides: true } } : [])));
  vi.stubGlobal('fetch', request);
  render(<OrganizationSettings organization="org" csrf="csrf-example" agents={[]} onChanged={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText('Member login'), { target: { value: 'teammate' } });
  fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Teammate' } });
  fireEvent.change(screen.getByLabelText('Initial password'), { target: { value: 'temporary-example-password' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add member' }));
  await waitFor(() => expect(screen.getByLabelText('Initial password')).toHaveProperty('value', ''));
  expect(request).toHaveBeenCalledWith('/api/organizations/org/members', expect.objectContaining({ method: 'PUT', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }), body: JSON.stringify({ login: 'teammate', display_name: 'Teammate', password: 'temporary-example-password', role: 'member' }) }));
});
