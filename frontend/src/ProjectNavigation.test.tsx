import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { EmployeeNewThreadChooser, ProjectNavigation } from './ProjectNavigation';
import type { Agent } from './workspace-api';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const agents: Agent[] = [
  { id: 'junior', name: 'Junior developer', title: 'Developer', organization_id: 'org', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } },
  { id: 'reviewer', name: 'Reviewer', title: 'Reviewer', organization_id: 'org', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } },
];

test('shows Projects with reciprocal employee labels, Ungrouped threads, filtering, and a Project-guided new thread', async () => {
  const request = vi.fn(async (input: string) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: 'Public site', target_repository_url: null, default_checkout_branch: null, archived: false }]);
    if (input.endsWith('/agents/junior/thread-projects')) return Response.json({ threads: [{ session_id: 'build', project_id: 'website' }, { session_id: 'loose', project_id: null }] });
    if (input.endsWith('/agents/reviewer/thread-projects')) return Response.json({ threads: [{ session_id: 'review', project_id: 'website' }] });
    if (input.endsWith('/agents/junior/sessions')) return Response.json([{ session_id: 'build', title: 'Build landing page' }, { session_id: 'loose', title: 'Explore colors' }]);
    if (input.endsWith('/agents/reviewer/sessions')) return Response.json([{ session_id: 'review', title: 'Review copy' }]);
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const open = vi.fn();
  const create = vi.fn();
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={agents} csrf="csrf-example" manager={false} detailsTarget={detailsTarget} onOpenThread={open} onNewThread={create} />);

  const website = await screen.findByRole('button', { name: 'Website' });
  fireEvent.click(website);
  const details = await screen.findByRole('region', { name: 'Website project' });
  expect(within(details).getByRole('button', { name: 'Edit Project' })).toBeTruthy();
  const build = within(details).getByRole('button', { name: 'Build landing page Junior developer' });
  const review = within(details).getByRole('button', { name: 'Review copy Reviewer' });
  expect(build.closest('[data-thread-card]')).toBeTruthy();
  expect(review.closest('[data-thread-card]')).toBeTruthy();
  expect(build.closest('[data-thread-card]')?.querySelectorAll('[data-slot="thread-card-subtitle"]')).toHaveLength(1);
  expect(review.closest('[data-thread-card]')?.querySelectorAll('[data-slot="thread-card-subtitle"]')).toHaveLength(1);
  expect(build.className).toContain('pe-44');
  expect(build.className).not.toContain('group-hover:pe-9');
  fireEvent.change(within(details).getByRole('searchbox', { name: 'Search Website threads' }), { target: { value: 'copy' } });
  expect(within(details).queryByText('Build landing page')).toBeNull();
  expect(within(details).getByText('Review copy')).toBeTruthy();
  fireEvent.click(within(details).getByRole('button', { name: 'New thread' }));
  fireEvent.change(await screen.findByRole('combobox', { name: /^Employee$/ }), { target: { value: 'reviewer' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(create).toHaveBeenCalledWith({ agent: 'reviewer', project: 'website' });

  fireEvent.click(screen.getByRole('button', { name: 'Ungrouped' }));
  expect(await screen.findByText('Explore colors')).toBeTruthy();
});

test('keeps an archived Project visible on its assigned thread and requires restore before new work', async () => {
  const request = vi.fn(async (input: string) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([
      { id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: null, default_checkout_branch: null, archived: true },
      { id: 'roadmap', organization_id: 'org', name: 'Roadmap', description: '', target_repository_url: null, default_checkout_branch: null, archived: false },
    ]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [{ session_id: 'build', project_id: 'website' }] });
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'build', title: 'Build landing page' }]);
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager detailsTarget={detailsTarget} onOpenThread={vi.fn()} onNewThread={vi.fn()} />);
  fireEvent.click(await screen.findByText('Archived Projects'));
  fireEvent.click(screen.getByRole('button', { name: 'Website' }));
  const details = await screen.findByRole('region', { name: 'Website project' });
  expect((within(details).getByRole('option', { name: 'Website (archived)' }) as HTMLOptionElement).selected).toBe(true);
  expect((within(details).getByRole('option', { name: 'Website (archived)' }) as HTMLOptionElement).disabled).toBe(true);
  expect((within(details).getByRole('button', { name: 'New thread' }) as HTMLButtonElement).disabled).toBe(true);
  expect(within(details).getByText('Restore this Project before starting a new thread.')).toBeTruthy();
});

test('moves an existing Ungrouped thread into a Project without changing its native session', async () => {
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [{ session_id: 'loose', project_id: null }] });
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'loose', title: 'Explore colors' }]);
    if (init.method === 'PUT' && input.endsWith('/sessions/loose/project')) return Response.json({ project_id: 'website' });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager={false} detailsTarget={detailsTarget} onOpenThread={vi.fn()} onNewThread={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Ungrouped' }));
  fireEvent.change(await screen.findByLabelText('Project for Explore colors'), { target: { value: 'website' } });
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/agents/junior/sessions/loose/project', expect.objectContaining({ method: 'PUT', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }), body: JSON.stringify({ project_id: 'website' }) })));
});

test('uses an optional branch override while retaining the Project checkout as the default', async () => {
  const request = vi.fn(async (input: string) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: 'https://example.test/website.git', default_checkout_branch: 'test', archived: false }]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [] });
    if (input.endsWith('/sessions')) return Response.json([]);
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const create = vi.fn();
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager={false} detailsTarget={detailsTarget} onOpenThread={vi.fn()} onNewThread={create} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Website' }));
  const details = await screen.findByRole('region', { name: 'Website project' });
  fireEvent.click(within(details).getByRole('button', { name: 'New thread' }));
  expect(await screen.findByText('This thread will prepare an independent workspace from the Project default checkout test.')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('Starting branch'), { target: { value: 'release' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(create).toHaveBeenCalledWith({ agent: 'junior', project: 'website', checkoutBranch: 'release' });
});

test('leaves the optional branch override out of employee-first Project creation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: 'https://example.test/website.git', default_checkout_branch: 'test', archived: false }])));
  const start = vi.fn();
  render(<EmployeeNewThreadChooser organization="org" agent={agents[0]} onStart={start} onCancel={vi.fn()} />);
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'website' } });
  expect(await screen.findByText('This thread will prepare an independent workspace from the Project default checkout test.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(start).toHaveBeenCalledWith({ project: 'website' });
});

test('keeps Project search visible and saves an empty optional description as null', async () => {
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [] });
    if (input.endsWith('/sessions')) return Response.json([]);
    if (input === '/api/organizations/org/projects' && init.method === 'POST') return Response.json({ id: 'new', organization_id: 'org', name: 'New Project', description: '', target_repository_url: null, default_checkout_branch: null, archived: false });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager detailsTarget={detailsTarget} onOpenThread={vi.fn()} onNewThread={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Website' }));
  const search = await screen.findByRole('searchbox', { name: 'Search Website threads' });
  expect(search.closest('label')?.classList.contains('sr-only')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New Project' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save Project' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/projects', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'New Project', description: null, target_repository_url: null, default_checkout_branch: null }) })));
});

test('shows a recoverable Project lookup failure before employee-first thread creation', async () => {
  const request = vi.fn(async () => Response.json({ detail: 'Projects are temporarily unavailable.' }, { status: 503 }));
  vi.stubGlobal('fetch', request);
  const start = vi.fn();
  render(<EmployeeNewThreadChooser organization="org" agent={agents[0]} onStart={start} onCancel={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Projects are temporarily unavailable.');
  expect(start).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
});

test('keeps Project threads visible while its Workspaces view opens host-owned inspection', async () => {
  const inspection = { workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'ordinary', directory: '/workspaces/org/junior/build', repository: { state: 'absent' }, git: { kind: 'ordinary', state: 'safe', branch: null, dirty: 0, untracked: 0, ignored: 0, ahead: 0, upstream: null }, history: { state: 'verified' }, cleanup: { remove: { available: true }, discard: { available: true }, replace: { available: false, reason: 'Remove first.' } } };
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input === '/api/organizations/org/projects?include_archived=true') return Response.json([{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [{ session_id: 'build', project_id: 'website' }] });
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'build', title: 'Build landing page' }]);
    if (input.endsWith('/sessions/build/workspace') && init.method === 'GET') return Response.json(inspection);
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const detailsTarget = document.body.appendChild(document.createElement('div'));
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager={false} detailsTarget={detailsTarget} onOpenThread={vi.fn()} onNewThread={vi.fn()} />);

  fireEvent.click(await screen.findByRole('button', { name: 'Website' }));
  const details = await screen.findByRole('region', { name: 'Website project' });
  expect(within(details).getByText('Build landing page')).toBeTruthy();
  fireEvent.click(within(details).getByRole('tab', { name: 'Workspaces' }));
  expect(await within(details).findByText('/workspaces/org/junior/build')).toBeTruthy();
  expect((within(details).getByRole('button', { name: 'Remove workspace' }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(within(details).getByRole('tab', { name: 'Threads' }));
  expect(within(details).getByText('Build landing page')).toBeTruthy();
});

test('identifies the current sidebar thread and opens a separately labelled thread without confusing its agent', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.includes('/projects?')) return Response.json([]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [] });
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'current', title: 'Inspect current workspace' }, { session_id: 'next', title: 'Review changes' }]);
    throw new Error(`Unexpected ${input}`);
  }));
  const open = vi.fn();
  render(<ProjectNavigation organization="org" agents={[agents[0]]} csrf="csrf-example" manager={false} currentThread={{ agent: 'junior', session: 'current' }} onOpenThread={open} onNewThread={vi.fn()} />);
  const current = await screen.findByRole('button', { name: 'Inspect current workspace Junior developer' });
  expect(current.getAttribute('aria-current')).toBe('page');
  const next = screen.getByRole('button', { name: 'Review changes Junior developer' });
  expect(next.hasAttribute('aria-current')).toBe(false);
  fireEvent.click(next);
  expect(open).toHaveBeenCalledWith('junior', 'next');
});
