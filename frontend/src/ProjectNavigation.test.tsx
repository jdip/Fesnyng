import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ProjectNavigation } from './ProjectNavigation';
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
  expect(within(details).getByRole('button', { name: 'Build landing pageJunior developer' })).toBeTruthy();
  expect(within(details).getByRole('button', { name: 'Review copyReviewer' })).toBeTruthy();
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
