import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Services } from './Services';
import type { Agent } from './workspace-api';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const agent = (id = 'employee', host = 'host-one'): Agent => ({
  id, organization_id: 'org', name: id === 'employee' ? 'Developer' : 'Designer', title: 'Employee', host_id: host,
  reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied',
  configuration: { execution_type: 'docker', provider: 'openai', model: 'example', profile_id: null, workspace: 'default', instructions: '', skills: [] },
});
const service = (overrides: Record<string, unknown> = {}) => ({
  id: 'service-one', name: 'Website preview', endpoint_url: 'https://preview.example.test', route: 'tailscale', revision: 3,
  target: { kind: 'resource', id: 'resource-one', status: 'available', running: true },
  threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'],
  route_status: { status: 'configured', network_reachability: 'unverified' },
  ...overrides,
});

function installFetch({ services = [service()], resources = [{ id: 'resource-one', name: 'Website container', container_id: 'a'.repeat(64), revision: 1, threads: [], project_ids: [], engine_id: 'engine', inspection: { status: 'available', state: 'running', running: true } }] }: { services?: unknown[]; resources?: unknown[] } = {}) {
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/docker/services') && (!init.method || init.method === 'GET')) return Response.json({ services });
    if (input.endsWith('/docker') && (!init.method || init.method === 'GET')) return Response.json({ capability: { enabled: false, available: false }, resources });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/agents/designer/sessions')) return Response.json([{ session_id: 'thread-two', title: 'Design task' }]);
    if (input.endsWith('/projects?include_archived=true')) return Response.json([{ id: 'project-one', name: 'Website', archived: false }]);
    if (input.endsWith('/docker/services') && init.method === 'POST') return Response.json(service());
    if (input.endsWith('/docker/services/service-one') && init.method === 'PUT') return Response.json(service({ revision: 4 }));
    if (input.endsWith('/docker/services/service-one/unregister') && init.method === 'POST') return Response.json({ removed: true });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  return request;
}

test('lists services while Docker capability is disabled and states route configuration without claiming reachability', async () => {
  installFetch();
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Website preview')).toBeTruthy();
  expect(screen.getByText('Docker capability is not required to register or view services.')).toBeTruthy();
  expect(screen.getByText('Serve route configured · Client network reachability is unverified.')).toBeTruthy();
  expect(screen.getByText('Project and thread associations organize work. Organization membership does not grant tailnet or network access.')).toBeTruthy();
  const link = screen.getByRole('link', { name: 'Open Website preview' });
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toBe('noreferrer noopener');
});

test('registers an employee or registered resource service with associations', async () => {
  const request = installFetch();
  render(<Services organization="org" agent={agent()} agents={[agent(), agent('designer')]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Register service' }));
  fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Preview site' } });
  fireEvent.change(screen.getByLabelText('Service target'), { target: { value: 'resource:resource-one' } });
  fireEvent.change(screen.getByLabelText('HTTP(S) endpoint'), { target: { value: 'https://preview.example.test' } });
  fireEvent.change(screen.getByLabelText('Route'), { target: { value: 'tailscale' } });
  fireEvent.click(screen.getByRole('checkbox', { name: 'Website task · Developer' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Website' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save service' }));

  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/services', expect.objectContaining({
    method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }),
    body: JSON.stringify({ name: 'Preview site', target_kind: 'resource', target_id: 'resource-one', endpoint_url: 'https://preview.example.test', route: 'tailscale', threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'] }),
  })));
});

test('rejects unsafe malformed service URLs from input and server data', async () => {
  installFetch({ services: [service({ endpoint_url: 'javascript:alert(1)' })] });
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Website preview')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Open Website preview' })).toBeNull();
  expect(screen.getByText('Endpoint is not a safe HTTP(S) URL.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Register service' }));
  fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Unsafe' } });
  fireEvent.change(screen.getByLabelText('HTTP(S) endpoint'), { target: { value: 'javascript:alert(1)' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save service' }));
  expect(screen.getByText('Enter a valid HTTP(S) endpoint.')).toBeTruthy();
});

test.each([
  'http://docker:2375',
  'https://127.0.0.1:4096',
  'https://user:secret@preview.example.test',
  'https://preview.example.test/\u0000hidden',
])('does not render unsafe malformed server endpoint %s', async (endpoint_url) => {
  installFetch({ services: [service({ endpoint_url })] });
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Website preview')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Open Website preview' })).toBeNull();
  expect(screen.getByText('Endpoint is not a safe HTTP(S) URL.')).toBeTruthy();
});

test('keeps unavailable targets and unknown associations visible until explicitly detached', async () => {
  const request = installFetch({ services: [service({ target: { kind: 'employee', id: 'departed', status: 'unavailable' }, threads: [{ agent_id: 'departed', session_id: 'deleted-thread' }], project_ids: ['project-one', 'deleted-project'], route_status: { status: 'unavailable', reason: 'No matching Tailscale Serve route.', network_reachability: 'unverified' } })] });
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Target unavailable')).toBeTruthy();
  expect(screen.getByText('Serve route unavailable · No matching Tailscale Serve route. Client network reachability is unverified.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Edit service' }));
  expect(screen.getByRole('checkbox', { name: 'Unavailable or deleted thread · departed / deleted-thread' })).toBeTruthy();
  expect(screen.getByRole('checkbox', { name: 'Unavailable or deleted Project · deleted-project' })).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox', { name: 'Unavailable or deleted thread · departed / deleted-thread' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Unavailable or deleted Project · deleted-project' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save service' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/services/service-one', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ name: 'Website preview', target_kind: 'employee', target_id: 'departed', endpoint_url: 'https://preview.example.test', route: 'tailscale', threads: [], project_ids: ['project-one'], expected_revision: 3 }),
  })));
});

test('describes custom records as registered endpoints without claiming route health', async () => {
  installFetch({ services: [service({ route: 'custom' })] });
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Endpoint registered · Client network reachability is unverified.')).toBeTruthy();
});

test('reports an available target with no running evidence as unknown', async () => {
  installFetch({ services: [service({ target: { kind: 'employee', id: 'employee', status: 'available' } })] });
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Target state unknown')).toBeTruthy();
});

test('requires an explicit revision-confirmed metadata-only unregister', async () => {
  const request = installFetch();
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Unregister service' }));
  expect(screen.getByText('Unregister Website preview? This removes its Fesnyng service record only. It does not stop, remove, expose, or reconfigure the target.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm unregister service' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/services/service-one/unregister', expect.objectContaining({ method: 'POST', body: JSON.stringify({ expected_revision: 3 }) })));
});

test('keeps service records readable when one host-local thread inventory is unavailable', async () => {
  const team = [agent(), agent('designer')];
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/docker/services')) return Response.json({ services: [service()] });
    if (input.endsWith('/docker')) return Response.json({ capability: { enabled: false, available: false }, resources: [] });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/agents/designer/sessions')) return Response.json({ detail: 'Designer is offline.' }, { status: 503 });
    if (input.endsWith('/projects?include_archived=true')) return Response.json([]);
    throw new Error(`Unexpected ${input}`);
  }));
  render(<Services organization="org" agent={team[0]!} agents={team} csrf="csrf-example" />);

  expect(await screen.findByText('Website preview')).toBeTruthy();
  expect(screen.getByText('Some host-local thread inventories are unavailable: Designer. Existing associations remain available to detach.')).toBeTruthy();
});

test('ignores a superseded employee host service response', async () => {
  let settleFirst: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn((input: string) => {
    if (input.includes('/hosts/host-one/docker/services')) return new Promise<Response>((resolve) => { settleFirst = resolve; });
    if (input.includes('/hosts/host-one/docker')) return Promise.resolve(Response.json({ resources: [] }));
    if (input.includes('/hosts/host-two/docker/services')) return Promise.resolve(Response.json({ services: [] }));
    if (input.includes('/hosts/host-two/docker')) return Promise.resolve(Response.json({ resources: [] }));
    if (input.includes('/agents/employee/sessions') || input.includes('/agents/designer/sessions')) return Promise.resolve(Response.json([]));
    if (input.endsWith('/projects?include_archived=true')) return Promise.resolve(Response.json([]));
    throw new Error(`Unexpected ${input}`);
  }));
  const team = [agent(), agent('designer', 'host-two')];
  const rendered = render(<Services organization="org" agent={team[0]!} agents={team} csrf="csrf-example" />);
  rendered.rerender(<Services organization="org" agent={team[1]!} agents={team} csrf="csrf-example" />);

  expect(await screen.findByText('No services are registered on this host.')).toBeTruthy();
  settleFirst?.(Response.json({ services: [service()] }));
  await Promise.resolve();
  expect(screen.queryByText('Website preview')).toBeNull();
});

test('does not call retained Projects deleted when their inventory is unavailable', async () => {
  const request = installFetch();
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (input, init = {}) => input.endsWith('/projects?include_archived=true')
    ? Response.json({ detail: 'Unavailable' }, { status: 503 }) : original(input, init));
  render(<Services organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);
  expect(await screen.findByText('Unavailable or deleted Project · project-one')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Edit service' }));
  expect(screen.getByRole('checkbox', { name: 'Unavailable or deleted Project · project-one' })).toBeTruthy();
  expect(screen.queryByText('Deleted Project · project-one')).toBeNull();
});
