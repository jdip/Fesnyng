import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AgentSettings } from './AgentSettings';
import type { Agent } from './workspace-api';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const agent: Agent = { id: 'agent', organization_id: 'org', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 4, applied_version: 4, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'gpt-5.6-luna', profile_id: 'profile', instructions: 'Investigate carefully.', workspace: 'default', skills: [] } };
test('saves department membership independently of reporting', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (options.method === 'PATCH') return new Response(JSON.stringify({ detail: 'Stop after observing the save' }), { status: 409 });
    return new Response(JSON.stringify(url.endsWith('/departments') ? [{ id: 'research', name: 'Research department', parent_id: null }] : []));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf" onSaved={() => {}} />);
  await screen.findByRole('option', { name: 'Research department' });
  fireEvent.change(screen.getByLabelText('Department'), { target: { value: 'research' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and apply' }));
  await screen.findByRole('alert');
  expect(JSON.parse(request.mock.calls.find(([, options]) => options?.method === 'PATCH')![1]!.body as string)).toMatchObject({ department_id: 'research', reports_to_agent_id: null });
});
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

test('submits Astra as the default model for a new agent without resetting existing agents', async () => {
  const created = { ...agent, id: 'new-agent', name: 'New agent', desired_version: 1, configuration: { ...agent.configuration, model: 'gpt-6-astra' } };
  const request = vi.fn().mockImplementation(async (url: string, options: RequestInit = {}) => {
    if (options.method === 'POST' && String(url).endsWith('/agents')) return new Response(JSON.stringify(created));
    if (String(url).endsWith('/apply')) return new Response(JSON.stringify({}));
    if (String(url).endsWith('/new-agent')) return new Response(JSON.stringify(created));
    return new Response(JSON.stringify(String(url).endsWith('/hosts') ? [{ id: 'host', name: 'Local host' }] : String(url).endsWith('/profiles') ? [] : []));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);

  fireEvent.change(await screen.findByLabelText('Agent name'), { target: { value: 'New agent' } });
  fireEvent.change(screen.getByLabelText('Home host'), { target: { value: 'host' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and apply' }));

  await waitFor(() => expect(request.mock.calls.some(([url, options]) => String(url).endsWith('/agents') && options.method === 'POST')).toBe(true));
  const create = request.mock.calls.find(([url, options]) => String(url).endsWith('/agents') && options.method === 'POST');
  expect(JSON.parse(create![1].body as string)).toMatchObject({ configuration: { model: 'gpt-6-astra' } });
  expect(agent.configuration.model).toBe('gpt-5.6-luna');
});

test('allows selecting Codex only while creating an employee', async () => {
  const created = { ...agent, id: 'codex-agent', name: 'Codex agent', desired_version: 1, configuration: { ...agent.configuration, runtime_type: 'codex' as const } };
  const request = vi.fn().mockImplementation(async (url: string, options: RequestInit = {}) => {
    if (options.method === 'POST' && String(url).endsWith('/agents')) return new Response(JSON.stringify(created));
    if (String(url).endsWith('/apply')) return new Response(JSON.stringify({}));
    if (String(url).endsWith('/codex-agent')) return new Response(JSON.stringify(created));
    return new Response(JSON.stringify(String(url).endsWith('/hosts') ? [{ id: 'host', name: 'Local host' }] : []));
  });
  vi.stubGlobal('fetch', request);
  const { unmount } = render(<AgentSettings organization="org" agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);

  fireEvent.change(await screen.findByLabelText('Harness'), { target: { value: 'codex' } });
  fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'Codex agent' } });
  fireEvent.change(screen.getByLabelText('Home host'), { target: { value: 'host' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and apply' }));

  await waitFor(() => expect(request.mock.calls.some(([url, options]) => String(url).endsWith('/agents') && options.method === 'POST')).toBe(true));
  const create = request.mock.calls.find(([url, options]) => String(url).endsWith('/agents') && options.method === 'POST');
  expect(JSON.parse(create![1].body as string)).toMatchObject({ configuration: { runtime_type: 'codex' } });
  expect(await screen.findByLabelText('Harness')).toHaveProperty('value', 'codex');

  unmount();
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);
  expect(screen.getByLabelText('Harness')).toHaveProperty('value', 'opencode');
  expect(screen.getByRole('button', { name: 'Switch to OpenCode' })).toHaveProperty('disabled', true);
});

test('switches an existing employee through the dedicated pending harness operation', async () => {
  const switched: Agent = {
    ...agent,
    desired_version: 5,
    applied_version: 4,
    configuration_status: 'pending',
    configuration: { ...agent.configuration, runtime_type: 'codex' },
  };
  const saved = vi.fn();
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (String(url).endsWith('/harness-switch') && options.method === 'POST') {
      return new Response(JSON.stringify({
        agent: switched,
        switch_state: 'pending_apply',
        message: 'Historical threads are permanently frozen; apply the selected harness.',
        intent: { state: 'frozen', target_runtime_type: 'codex' },
      }));
    }
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf-example" onSaved={saved} />);

  fireEvent.change(await screen.findByLabelText('Harness'), { target: { value: 'codex' } });
  fireEvent.click(screen.getByRole('button', { name: 'Switch to Codex' }));

  await waitFor(() => expect(request.mock.calls.some(([url, options]) => String(url).endsWith('/harness-switch') && options?.method === 'POST')).toBe(true));
  const switchRequest = request.mock.calls.find(([url, options]) => String(url).endsWith('/harness-switch') && options?.method === 'POST')!;
  expect(JSON.parse(switchRequest[1]!.body as string)).toEqual({ expected_version: 4, target_runtime_type: 'codex' });
  expect(await screen.findByText('Historical threads are permanently frozen; apply the selected harness.')).toBeTruthy();
  expect(saved).toHaveBeenCalledWith(switched);
});

test('shows the host switch preflight error without changing the selected harness', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (String(url).endsWith('/harness-switch') && options.method === 'POST') {
      return new Response(JSON.stringify({ detail: 'Queued messages must complete or be cancelled before switching.' }), { status: 409 });
    }
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);

  fireEvent.change(await screen.findByLabelText('Harness'), { target: { value: 'codex' } });
  fireEvent.click(screen.getByRole('button', { name: 'Switch to Codex' }));

  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Queued messages must complete or be cancelled before switching.');
  expect(screen.getByLabelText('Harness')).toHaveProperty('value', 'codex');
});

test('recovers a frozen switch after a browser reload by applying its selected harness', async () => {
  const selected: Agent = { ...agent, desired_version: 5, applied_version: 4, configuration_status: 'pending', configuration: { ...agent.configuration, runtime_type: 'codex' } };
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (String(url).endsWith('/runtime')) return new Response(JSON.stringify({ switch_state: 'frozen', switch_target_runtime: 'codex' }));
    if (String(url).endsWith('/apply') && options.method === 'POST') return new Response(JSON.stringify({}));
    if (String(url).endsWith('/agents/agent')) return new Response(JSON.stringify(selected));
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={selected} agents={[selected]} csrf="csrf-example" onSaved={vi.fn()} />);

  expect(await screen.findByRole('region', { name: 'Harness switch recovery' })).toHaveProperty('textContent', expect.stringContaining('Historical threads are frozen. Apply Codex when ready.'));
  fireEvent.click(screen.getByRole('button', { name: 'Apply selected harness' }));
  await waitFor(() => expect(request.mock.calls.some(([url, options]) => String(url).endsWith('/apply') && options?.method === 'POST')).toBe(true));
});

test('retries a frozen switch whose control-plane acknowledgement was lost', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (String(url).endsWith('/runtime')) return new Response(JSON.stringify({ switch_state: 'frozen', switch_target_runtime: 'codex' }));
    if (String(url).endsWith('/harness-switch') && options.method === 'POST') return new Response(JSON.stringify({ detail: 'Retry was observed' }), { status: 409 });
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={agent} agents={[agent]} csrf="csrf-example" onSaved={vi.fn()} />);

  expect(await screen.findByRole('region', { name: 'Harness switch recovery' })).toHaveProperty('textContent', expect.stringContaining('Retry the switch to record Codex'));
  fireEvent.click(screen.getByRole('button', { name: 'Retry harness switch' }));
  await waitFor(() => expect(request.mock.calls.some(([url, options]) => String(url).endsWith('/harness-switch') && options?.method === 'POST')).toBe(true));
  expect(screen.queryByRole('button', { name: 'Apply selected harness' })).toBeNull();
});

test('saves the authenticated account model and per-agent thinking choice together', async () => {
  const codexAgent: Agent = { ...agent, configuration: { ...agent.configuration, runtime_type: 'codex' } };
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    if (options.method === 'PATCH') return new Response(JSON.stringify({ detail: 'Save observed' }), { status: 409 });
    if (url.endsWith('/codex/models')) return new Response(JSON.stringify({ data: [
      { model: 'account-model', displayName: 'Account model', defaultReasoningEffort: 'low', supportedReasoningEfforts: [{ reasoningEffort: 'high', description: 'Thorough' }, { reasoningEffort: 'low', description: 'Quick' }] },
    ], nextCursor: null }));
    return new Response(JSON.stringify(url.endsWith('/hosts') ? [{ id: 'host', name: 'Local host' }] : url.endsWith('/profiles') ? [{ id: 'profile', name: 'Subscription' }] : []));
  });
  vi.stubGlobal('fetch', request);
  render(<AgentSettings organization="org" agent={codexAgent} agents={[codexAgent]} csrf="csrf" onSaved={vi.fn()} />);
  await screen.findByRole('option', { name: 'Account model' });
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'account-model' } });
  fireEvent.change(screen.getByLabelText('Default thinking level'), { target: { value: 'high' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and apply' }));
  await screen.findByText('Save observed');
  expect(JSON.parse(request.mock.calls.find(([, options]) => options?.method === 'PATCH')![1]!.body as string)).toMatchObject({ expected_version: 4, configuration: { model: 'account-model', reasoning_effort: 'high', instructions: 'Investigate carefully.' } });
});
