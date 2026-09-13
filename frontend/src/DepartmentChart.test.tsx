import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DepartmentChart } from './DepartmentChart';
import type { Agent } from './workspace-api';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('creates a nested department and retains the chart after a rejected edit', async () => {
  const departments = [{ id: 'engineering', name: 'Engineering', parent_id: null, head_agent_id: null }];
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return new Response(JSON.stringify({ detail: 'Department name already exists' }), { status: 409 });
    return new Response(JSON.stringify(departments));
  });
  vi.stubGlobal('fetch', fetch);
  render(<DepartmentChart organization="org" agents={[]} manager csrf="csrf" onSelect={() => {}} />);
  expect(await screen.findByText('Engineering')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Add department' }));
  fireEvent.change(screen.getByLabelText('Department name'), { target: { value: 'Platform' } });
  fireEvent.change(screen.getByLabelText('Parent department'), { target: { value: 'engineering' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create department' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Department name already exists');
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/organizations/org/departments', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'Platform', parent_id: 'engineering', head_agent_id: null }) })));
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Add department' })));
});
test('shows nested departments, cross-department reporting and unassigned agents without member edit controls', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([
    { id: 'engineering', name: 'Engineering', parent_id: null, head_agent_id: 'lead' },
    { id: 'platform', name: 'Platform', parent_id: 'engineering', head_agent_id: null },
  ]))));
  const base = { organization_id: 'org', host_id: 'host', desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'example', profile_id: null, workspace: 'default', instructions: '', skills: [] } } as const;
  const agents: Agent[] = [
    { ...base, configuration: { ...base.configuration, skills: [] }, id: 'lead', name: 'Team lead', title: 'Director', department_id: 'engineering', reports_to_agent_id: null },
    { ...base, configuration: { ...base.configuration, skills: [] }, id: 'engineer', name: 'Platform engineer', title: 'Engineer', department_id: 'platform', reports_to_agent_id: 'lead' },
    { ...base, configuration: { ...base.configuration, skills: [] }, id: 'reviewer', name: 'Independent reviewer', title: 'Reviewer', department_id: null, reports_to_agent_id: null },
  ];
  const select = vi.fn();
  render(<DepartmentChart organization="org" agents={agents} manager={false} csrf="csrf" onSelect={select} />);
  const engineering = await screen.findByRole('region', { name: 'Engineering department' });
  expect(engineering.contains(screen.getByRole('region', { name: 'Platform department' }))).toBe(true);
  expect(screen.getByText('Head of Engineering')).toBeTruthy();
  expect(screen.getByRole('region', { name: 'Agents without a department' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /Platform engineer Engineer Reports to Team lead/ }));
  expect(select).toHaveBeenCalledWith(agents[1]);
  expect(screen.queryByRole('button', { name: 'Add department' })).toBeNull();
  expect(screen.queryByRole('button', { name: /Edit .* department/ })).toBeNull();
});
test.each(['save', 'delete'] as const)('restores chart focus after a successful department %s', async (operation) => {
  const department = { id: 'empty', organization_id: 'org', name: 'Research', parent_id: null, head_agent_id: null };
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'DELETE'
    ? new Response(null, { status: 204 })
    : new Response(JSON.stringify(options?.method === 'PATCH' ? department : [department]))));
  render(<DepartmentChart organization="org" agents={[]} manager csrf="csrf" onSelect={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Research department' }));
  if (operation === 'save') fireEvent.click(screen.getByRole('button', { name: 'Save department' }));
  else {
    fireEvent.click(screen.getByRole('button', { name: 'Delete department' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm deletion' }));
  }
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: operation === 'save' ? 'Edit Research department' : 'Add department' })));
});
