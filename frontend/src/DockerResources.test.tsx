import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DockerResources } from './DockerResources';
import type { Agent } from './workspace-api';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const containerId = 'a'.repeat(64);
const agent = (id = 'employee', host = 'host-one'): Agent => ({
  id, organization_id: 'org', name: id === 'employee' ? 'Developer' : 'Designer', title: 'Employee', host_id: host,
  reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied',
  configuration: { execution_type: 'docker', provider: 'openai', model: 'example', profile_id: null, workspace: 'default', instructions: '', skills: [] },
});
const resource = (running = false) => ({
  id: 'resource-one', container_id: containerId, name: 'Website preview', revision: 3,
  threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'], engine_id: 'engine-one',
  inspection: { status: 'available' as const, state: running ? 'running' : 'exited', running, mounts: [{ type: 'bind', source: '/workspaces/org/employee/thread-one', destination: '/workspace', read_only: false }], ports: [{ container_port: '3000/tcp', host_ip: '127.0.0.1', host_port: '3000' }], networks: ['preview_default'], compose_project: 'preview', compose_service: 'web' },
});

function installFetch({ enabled = true, resources = [resource()], discovery = [] as unknown[] }: { enabled?: boolean; resources?: unknown[]; discovery?: unknown[] } = {}) {
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/docker') && (!init.method || init.method === 'GET')) return Response.json({ capability: { enabled, available: enabled, reason: enabled ? null : 'Docker resources are disabled for this organization host.' }, resources });
    if (input.includes('/docker/capability?agent_id=employee')) return Response.json({ enabled, available: enabled, reason: enabled ? null : 'Docker resources are disabled for this organization host.' });
    if (input.endsWith('/docker/discovery')) return Response.json({ containers: discovery });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/projects?include_archived=true')) return Response.json([{ id: 'project-one', name: 'Website', archived: false }]);
    if (input.endsWith('/resources') && init.method === 'POST') return Response.json(resource());
    if (input.endsWith('/resources/resource-one/remove') && init.method === 'POST') return Response.json({ ...resource(), inspection: { ...resource().inspection, status: 'missing', state: 'removed', running: false } });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  return request;
}

test('shows the selected employee Docker capability and leaves a disabled host readable', async () => {
  installFetch({ enabled: false });
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Docker resources are disabled for this organization host.')).toBeTruthy();
  expect(screen.getByText('Website preview')).toBeTruthy();
  expect(screen.getByText('Website task · Developer')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Discover containers' }) as HTMLButtonElement).disabled).toBe(true);
});

test('keeps host-level resource management available when this selected employee lacks Docker access', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/docker')) return Response.json({ capability: { enabled: true, available: true, reason: null }, resources: [] });
    if (input.includes('/docker/capability?agent_id=employee')) return Response.json({ enabled: true, available: false, reason: 'Employee is not allowed to use Docker capability' });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([]);
    if (input.endsWith('/projects?include_archived=true')) return Response.json([]);
    throw new Error(`Unexpected ${input}`);
  }));
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  expect(await screen.findByText('Employee is not allowed to use Docker capability')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Discover containers' }) as HTMLButtonElement).disabled).toBe(false);
});

test('registers an exact discovered container with selected thread and project associations', async () => {
  const request = installFetch({ discovery: [{ container_id: containerId, name: 'preview-web', state: 'exited', compose_project: 'preview', compose_service: 'web' }] });
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Discover containers' }));
  fireEvent.change(await screen.findByLabelText('Discovered container'), { target: { value: containerId } });
  fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Website preview' } });
  fireEvent.click(screen.getByRole('checkbox', { name: 'Website task · Developer' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Website' }));
  fireEvent.click(screen.getByRole('button', { name: 'Register container' }));

  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/resources', expect.objectContaining({
    method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }),
    body: JSON.stringify({ container_id: containerId, name: 'Website preview', threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'] }),
  })));
});

test('requires confirmation and a stopped container before removal while preserving volumes', async () => {
  const request = installFetch({ resources: [resource(false)] });
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Remove container' }));
  expect(screen.getByText('Remove this stopped container? Files in its writable layer will be lost. Volumes and bind-mounted host files are retained.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm remove container' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/resources/resource-one/remove', expect.objectContaining({ method: 'POST', body: JSON.stringify({ expected_revision: 3 }) })));
});

test('does not offer removal for a running container', async () => {
  installFetch({ resources: [resource(true)] });
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  const remove = await screen.findByRole('button', { name: 'Remove container' }) as HTMLButtonElement;
  expect(remove.disabled).toBe(true);
  expect(screen.getByText('Stop the container before removing it.')).toBeTruthy();
});

test('ignores a prior host inventory after the selected employee changes', async () => {
  let settleFirst: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn((input: string) => {
    if (input.includes('/hosts/host-one/docker') && !input.includes('/capability')) return new Promise<Response>((resolve) => { settleFirst = resolve; });
    if (input.includes('/hosts/host-one/docker/capability')) return Promise.resolve(Response.json({ enabled: true, available: true }));
    if (input.includes('/hosts/host-two/docker') && !input.includes('/capability')) return Promise.resolve(Response.json({ capability: { enabled: true, available: true }, resources: [] }));
    if (input.includes('/hosts/host-two/docker/capability')) return Promise.resolve(Response.json({ enabled: true, available: true }));
    if (input.includes('/agents/employee/sessions')) return Promise.resolve(Response.json([]));
    if (input.includes('/agents/designer/sessions')) return Promise.resolve(Response.json([]));
    if (input.endsWith('/projects?include_archived=true')) return Promise.resolve(Response.json([]));
    throw new Error(`Unexpected ${input}`);
  }));
  const rendered = render(<DockerResources organization="org" agent={agent()} agents={[agent(), agent('designer', 'host-two')]} csrf="csrf-example" />);
  rendered.rerender(<DockerResources organization="org" agent={agent('designer', 'host-two')} agents={[agent(), agent('designer', 'host-two')]} csrf="csrf-example" />);

  expect(await screen.findByText('No Docker resources are registered on this host.')).toBeTruthy();
  settleFirst?.(Response.json({ capability: { enabled: true, available: true }, resources: [resource()] }));
  await Promise.resolve();
  expect(screen.queryByText('Website preview')).toBeNull();
});


test('unavailable inspection does not imply a stopped container or permit engine operations', async () => {
  installFetch({ resources: [{ ...resource(), inspection: { status: 'unavailable', reason: 'Engine is unavailable' } }] });
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);
  expect(await screen.findByText('Unavailable')).toBeTruthy();
  for (const name of ['Start container', 'Stop container', 'Remove container']) {
    expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});

test('clears pending operations and confirmations when the employee scope changes', async () => {
  const original = installFetch();
  let finish: ((value: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn((input: string, init: RequestInit = {}) => {
    if (input.endsWith('/resources/resource-one/stop')) return new Promise<Response>((resolve) => { finish = resolve; });
    if (input.endsWith('/docker')) return Promise.resolve(Response.json({ capability: { enabled: true, available: true }, resources: [resource(true)] }));
    if (input.includes('/capability')) return Promise.resolve(Response.json({ enabled: true, available: true }));
    if (input.endsWith('/agents/designer/sessions')) return Promise.resolve(Response.json([]));
    return original(input, init);
  }));
  const team = [agent(), agent('designer', 'host-two')];
  const rendered = render(<DockerResources organization="org" agent={team[0]!} agents={team} csrf="csrf-example" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Stop container' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm stop container' }));
  rendered.rerender(<DockerResources organization="org" agent={team[1]!} agents={team} csrf="csrf-example" />);
  await screen.findByText('Website preview');
  expect(screen.queryByRole('button', { name: 'Confirm stop container' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Stop container' }));
  expect((screen.getByRole('button', { name: 'Confirm stop container' }) as HTMLButtonElement).disabled).toBe(false);
  finish?.(Response.json(resource(false)));
});

test('refreshes resource revisions and clears a stale removal confirmation', async () => {
  let reads = 0;
  const request = vi.fn(async (input: string) => {
    if (input.endsWith('/docker')) return Response.json({ capability: { enabled: true, available: true }, resources: [{ ...resource(false), revision: ++reads + 2 }] });
    if (input.includes('/docker/capability')) return Response.json({ enabled: true, available: true });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/projects?include_archived=true')) return Response.json([{ id: 'project-one', name: 'Website', archived: false }]);
    if (input.endsWith('/resources/resource-one/remove')) return Response.json({ ...resource(false), revision: reads + 2, inspection: { ...resource(false).inspection, status: 'missing' } });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Remove container' }));
  expect(screen.getByRole('button', { name: 'Confirm remove container' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh resources' }));
  await waitFor(() => expect(reads).toBe(2));
  expect(screen.queryByRole('button', { name: 'Confirm remove container' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Remove container' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm remove container' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/resources/resource-one/remove', expect.objectContaining({ body: JSON.stringify({ expected_revision: 4 }) })));
});

test('keeps unknown threads and archived or deleted Projects visible until they are explicitly detached', async () => {
  const associated = { ...resource(), threads: [{ agent_id: 'employee', session_id: 'thread-one' }, { agent_id: 'departed-agent', session_id: 'deleted-thread' }], project_ids: ['project-one', 'project-archived', 'project-deleted'] };
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/docker')) return Response.json({ capability: { enabled: true, available: true }, resources: [associated] });
    if (input.includes('/docker/capability')) return Response.json({ enabled: true, available: true });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/projects?include_archived=true')) return Response.json([{ id: 'project-one', name: 'Website', archived: false }, { id: 'project-archived', name: 'Old website', archived: true }]);
    if (input.endsWith('/resources/resource-one') && init.method === 'PUT') return Response.json({ ...associated, revision: 4, threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'] });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  render(<DockerResources organization="org" agent={agent()} agents={[agent()]} csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Edit associations' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Unavailable or deleted thread · departed-agent / deleted-thread' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Old website (archived Project)' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'Deleted Project · project-deleted' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save associations' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/hosts/host-one/docker/resources/resource-one', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ name: 'Website preview', threads: [{ agent_id: 'employee', session_id: 'thread-one' }], project_ids: ['project-one'], expected_revision: 3 }),
  })));
});

test('retains shared resource inventory when one host-local employee thread inventory is unavailable', async () => {
  const team = [agent(), agent('designer')];
  const associated = { ...resource(), threads: [{ agent_id: 'designer', session_id: 'unavailable-thread' }] };
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/docker')) return Response.json({ capability: { enabled: true, available: true }, resources: [associated] });
    if (input.includes('/docker/capability')) return Response.json({ enabled: true, available: true });
    if (input.endsWith('/agents/employee/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Website task' }]);
    if (input.endsWith('/agents/designer/sessions')) return Response.json({ detail: 'Designer is offline.' }, { status: 503 });
    if (input.endsWith('/projects?include_archived=true')) return Response.json([]);
    throw new Error(`Unexpected ${input}`);
  }));
  render(<DockerResources organization="org" agent={team[0]!} agents={team} csrf="csrf-example" />);

  expect(await screen.findByText('Website preview')).toBeTruthy();
  expect(screen.getByText('Some host-local thread inventories are unavailable: Designer. Existing associations remain available to edit or detach.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Edit associations' }));
  expect(screen.getByRole('checkbox', { name: 'Unavailable or deleted thread · designer / unavailable-thread' })).toBeTruthy();
});
