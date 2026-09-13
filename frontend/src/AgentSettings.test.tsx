import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AgentSettings } from './AgentSettings';
import type { Agent } from './workspace-api';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const agent: Agent = { id: 'agent', organization_id: 'org', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 4, applied_version: 4, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'gpt-5.6-luna', profile_id: 'profile', instructions: 'Investigate carefully.', workspace: 'default', skills: [] } };
test('saves the expected configuration version and applies only after a successful save', async () => {
  const request = vi.fn().mockImplementation(async (url: string, options: RequestInit) => {
    if (options.method === 'PATCH') return new Response(JSON.stringify({detail: 'Configuration changed; reload before editing.'}), { status: 409 });
    return new Response(JSON.stringify(url.endsWith('/hosts') ? [{ id: 'host', name: 'Local host' }] : url.endsWith('/profiles') ? [{ id: 'profile', name: 'Subscription' }] : []));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText('Instructions'), { target: { value: 'Investigate thoroughly.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and apply' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Configuration changed; reload before editing.');
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith('/apply'))).toBe(false));
  const patch = request.mock.calls.find(([, options]) => options.method === 'PATCH');
  expect(JSON.parse(patch![1].body as string)).toMatchObject({ expected_version: 4, configuration: { instructions: 'Investigate thoroughly.' } });
});
