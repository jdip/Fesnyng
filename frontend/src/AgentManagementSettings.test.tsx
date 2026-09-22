import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AgentManagementSettings } from './AgentManagementSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('loads and saves the separate human-only grant without saving agent configuration', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ enabled: false }))).mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true })));
  vi.stubGlobal('fetch', fetcher);
  render(<AgentManagementSettings organization="org" agent="agent" csrf="csrf" />);
  const toggle = await screen.findByRole('checkbox', { name: 'Allow organization management' });
  await waitFor(() => expect((toggle as HTMLInputElement).disabled).toBe(false));
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole('button', { name: 'Save management access' }));
  await screen.findByText('Management access saved.');
  expect(fetcher).toHaveBeenLastCalledWith('/api/organizations/org/agents/agent/management', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ enabled: true }), headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf' }) }));
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('keeps access disabled when the grant cannot be read', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 })));
  render(<AgentManagementSettings organization="org" agent="agent" csrf="csrf" />);
  await screen.findByRole('alert');
  expect((screen.getByRole('button', { name: 'Save management access' }) as HTMLButtonElement).disabled).toBe(true);
});
