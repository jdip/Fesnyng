import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { AgentConversation, App } from './App';
import type { Agent } from './workspace-api';
vi.mock('./Conversation', async () => {
  const { createPortal } = await import('react-dom');
  const { useState } = await import('react');
  return { Conversation: ({ sessionId, threadListTarget, onThreadSelect, onSessionChange, readOnly, executionBlocked, executionBlockedState }: { sessionId?: string; threadListTarget?: HTMLElement; onThreadSelect?: () => void; onSessionChange?: (id: string) => void; readOnly?: boolean; executionBlocked?: boolean; executionBlockedState?: string }) => {
    const [draft, setDraft] = useState('');
    return <><p>Native conversation {sessionId}</p>{readOnly ? <p>This thread is permanently frozen and read-only.</p> : executionBlocked ? <p>{executionBlockedState === 'removed' ? 'This workspace was removed. Prepare its replacement before continuing.' : 'This workspace cannot run until its state is resolved. Inspect the workspace before continuing.'}</p> : <textarea aria-label="Composer draft" value={draft} onChange={(event) => setDraft(event.target.value)} />}{threadListTarget && createPortal(<><button onClick={onThreadSelect}>Open sidebar thread</button><button onClick={onThreadSelect}>Create sidebar thread</button><button onClick={() => onSessionChange?.('other-thread')}>Select other sidebar thread</button></>, threadListTarget)}</>;
  } };
});
vi.mock('./CodexConversation', () => ({
  CodexConversation: ({ sessionId, readOnly, executionBlocked }: { sessionId?: string; readOnly?: boolean; executionBlocked?: boolean }) => <><p>Codex conversation {sessionId}</p>{readOnly ? <p>This thread is permanently frozen and read-only.</p> : executionBlocked && <p>This workspace is unavailable. Prepare its replacement before continuing.</p>}</>,
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); window.history.replaceState(null, '', '/'); });

test('requires sign in when no authenticated browser session exists', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
  render(<App />);
  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy();
  expect(screen.queryByLabelText('Organization')).toBeNull();
});

test('keeps a rejected login visible without exposing a workspace', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({detail: 'Invalid login or password.'}), { status: 401 }))));
  render(<App />);
  fireEvent.change(await screen.findByLabelText('Login'), { target: { value: 'member' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'incorrect-password' } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Invalid login or password.');
  expect(screen.queryByLabelText('Organization')).toBeNull();
});

test('switches organization scope and removes the previous agents immediately', async () => {
  const session = { user: { id: 'human', login: 'member', display_name: 'Member' }, csrf_token: 'csrf-example' };
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? session
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }, { id: 'two', name: 'Second organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input === '/api/organizations/one/agents' ? [{ id: 'agent-one', name: 'First researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/two/agents' ? [{ id: 'agent-two', name: 'Second researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  expect(await screen.findByRole('button', { name: /First researcher/, pressed: false })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Organization settings' })).toBeNull();
  fireEvent.keyDown(screen.getByRole('button', { name: 'Switch organization: First organization' }), { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Second organization' }));
  expect(screen.queryByRole('button', { name: /First researcher/ })).toBeNull();
  expect(await screen.findByRole('button', { name: /Second researcher/, pressed: false })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Reporting chart', level: 1 }));
});

test('keeps an unsent draft when the current organization is selected again', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }, { id: 'two', name: 'Second organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input === '/api/organizations/one/agents' ? []
      : input === '/api/organizations/two/agents' ? [{ id: 'agent-two', name: 'Second researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  const firstSwitcher = await screen.findByRole('button', { name: 'Switch organization: First organization' });
  fireEvent.keyDown(firstSwitcher, { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Reporting chart for Second organization' }));
  fireEvent.click(await screen.findByRole('button', { name: /Second researcher/, pressed: false }));
  const draft = await screen.findByLabelText('Composer draft');
  fireEvent.change(draft, { target: { value: 'Keep this draft' } });
  const secondSwitcher = screen.getByRole('button', { name: 'Switch organization: Second organization' });
  fireEvent.keyDown(secondSwitcher, { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Second organization' }));
  expect(await screen.findByLabelText('Composer draft')).toHaveProperty('value', 'Keep this draft');
});

test('opens shared Resources for the selected employee without discarding its conversation draft', async () => {
  window.history.replaceState(null, '', '/#organization=one&agent=agent-one&thread=thread-one');
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input === '/api/organizations/one/agents' ? [{ id: 'agent-one', organization_id: 'one', name: 'Developer', title: 'Engineering', host_id: 'host-one', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', provider: 'openai', model: 'example', profile_id: null, workspace: 'default', instructions: '', skills: [] } }]
      : input.endsWith('/agents/agent-one/sessions/thread-one/workspace') ? { workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread-one', repository: { state: 'absent' }, git: { kind: 'ordinary', state: 'safe', dirty: 0, untracked: 0, ignored: 0, ahead: 0 }, history: { state: 'verified' }, cleanup: { remove: { available: true }, discard: { available: true }, replace: { available: false } } }
      : input.endsWith('/agents/agent-one/sessions') ? [{ session_id: 'thread-one', title: 'Implementation task' }]
      : input.includes('/hosts/host-one/docker/capability') ? { enabled: false, available: false, reason: 'Docker resources are disabled for this organization host.' }
      : input.endsWith('/hosts/host-one/docker/services') ? { services: [{ id: 'service-one', name: 'Preview', endpoint_url: 'https://preview.example.test', route: 'custom', revision: 1, target: { kind: 'employee', id: 'agent-one', status: 'available', running: true }, threads: [], project_ids: [], route_status: { status: 'configured', network_reachability: 'unverified' } }] }
      : input.endsWith('/hosts/host-one/docker') ? { capability: { enabled: false, available: false }, resources: [] }
      : input.endsWith('/projects?include_archived=true') ? []
      : input.endsWith('/thread-projects') ? { threads: [] }
      : [];
    return Response.json(body);
  }));
  render(<App />);

  const draft = await screen.findByLabelText('Composer draft');
  fireEvent.change(draft, { target: { value: 'Keep this implementation note' } });
  fireEvent.click(screen.getByRole('button', { name: 'Resources' }));
  expect(await screen.findByRole('heading', { name: 'Developer resources', level: 1 })).toBeTruthy();
  expect(await screen.findByText('Preview')).toBeTruthy();
  expect(screen.getByText('Docker capability is not required to register or view services.')).toBeTruthy();
  expect(await screen.findByText('Docker resources are disabled for this organization host.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Workspaces' }));
  fireEvent.click(screen.getByRole('button', { name: /Developer/, pressed: true }));
  expect(await screen.findByLabelText('Composer draft')).toHaveProperty('value', 'Keep this implementation note');
});

test('retries an unavailable organization role lookup when the workspace refreshes', async () => {
  let membershipRequests = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/members') && membershipRequests++ < 2) {
      return new Response(JSON.stringify({ detail: 'Temporary lookup failure.' }), { status: 500 });
    }
    const body = input === '/api/auth/session' ? { user: { id: 'owner', display_name: 'Owner' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'owner', role: 'owner' }]
      : input.endsWith('/agents') ? []
      : [];
    return new Response(JSON.stringify(body));
  }));

  render(<App />);
  const switcher = await screen.findByRole('button', { name: 'Switch organization: First organization' });
  fireEvent.keyDown(switcher, { key: 'ArrowDown' });
  expect(screen.queryByRole('menuitem', { name: 'Organization settings for First organization' })).toBeNull();
  fireEvent.keyDown(switcher, { key: 'Escape' });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh workspace' }));
  await waitFor(() => expect(membershipRequests).toBeGreaterThan(2));
  fireEvent.keyDown(switcher, { key: 'ArrowDown' });
  expect(await screen.findByRole('menuitem', { name: 'Organization settings for First organization' })).toBeTruthy();
});

test('uses the reporting chart as the default overview with compact settings navigation', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'owner', display_name: 'Owner' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'owner', role: 'owner' }]
      : input.endsWith('/policy') ? { desired_version: 1, configuration: { default_permission: 'allow', mandatory_permissions: [], allow_thread_overrides: true } }
      : [];
    return new Response(JSON.stringify(body));
  }));

  render(<App />);

  expect(await screen.findByRole('heading', { name: 'Reporting chart' })).toBeTruthy();
  expect(await screen.findByText('Create an agent or department to start your organization chart.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Overview' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Activity' })).toBeNull();
  const switcher = screen.getByRole('button', { name: 'Switch organization: First organization' });
  fireEvent.keyDown(switcher, { key: 'ArrowDown' });
  const chart = await screen.findByRole('menuitem', { name: 'Reporting chart for First organization' });
  expect(chart.getAttribute('title')).toBe('Reporting chart');
  const settings = await screen.findByRole('menuitem', { name: 'Organization settings for First organization' });
  expect(settings.getAttribute('title')).toBe('Organization settings');
  fireEvent.click(settings);
  expect(await screen.findByRole('heading', { name: 'Organization settings', level: 1 })).toBeTruthy();
});

test('restores a selected native thread after reloading an authorized organization', async () => {
  window.history.replaceState(null, '', '/#organization=one&agent=agent-one&thread=thread-one');
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? {user: {id: 'human', display_name: 'Member'}, csrf_token: 'csrf-example'}
      : input === '/api/organizations' ? [{id: 'one', name: 'First organization'}]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{user_id: 'human', role: 'member'}]
      : input.endsWith('/agents') ? [{id: 'agent-one', name: 'Researcher', title: 'Research', configuration: {workspace: 'default'}}]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  expect(await screen.findByText('Native conversation thread-one')).toBeTruthy();
  expect(document.querySelector('.thread-context')).toBeNull();
});

test('keeps a frozen OpenCode thread on its original renderer after the employee switches to Codex', async () => {
  window.history.replaceState(null, '', '/#organization=one&agent=agent-one&thread=old-opencode');
  const request = vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents/agent-one/sessions') ? [{ session_id: 'old-opencode', title: 'Original OpenCode work', runtime_type: 'opencode', frozen_at: 0 }]
      : input.endsWith('/agents') ? [{ id: 'agent-one', name: 'Researcher', title: 'Research', configuration: { workspace: 'default', runtime_type: 'codex' } }]
      : [];
    return new Response(JSON.stringify(body));
  });
  vi.stubGlobal('fetch', request);

  render(<App />);

  expect(await screen.findByText('Native conversation old-opencode')).toBeTruthy();
  expect(screen.getByText('This thread is permanently frozen and read-only.')).toBeTruthy();
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
  expect(screen.getByRole('button', { name: /Original OpenCode work/ })).toBeTruthy();
});

test('refreshes the immutable inventory before opening a newly created thread', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 2, applied_version: 2, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'codex', provider: 'openai', model: 'gpt-6-astra', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  let reads = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/agents/agent-one/sessions')) {
      reads += 1;
      return new Response(JSON.stringify(reads === 1
        ? [{ session_id: 'existing', title: 'Existing history', runtime_type: 'opencode', frozen: true }]
        : [{ session_id: 'existing', title: 'Existing history', runtime_type: 'opencode', frozen: true }, { session_id: 'fresh', title: 'Fresh Codex thread', runtime_type: 'codex', frozen: false }]));
    }
    return new Response(JSON.stringify([]));
  }));
  const props = { organization: 'one', agent, csrfToken: 'csrf', hidden: false, refreshKey: 0, threadListTarget: null, newThreadRequest: undefined, onNewThreadStarted: vi.fn(), threadPageSize: 6, onThreadSelect: vi.fn(), onSessionChange: vi.fn(), onError: vi.fn(), onOpen: vi.fn() };
  const view = render(<AgentConversation {...props} sessionId="existing" />);
  expect(await screen.findByText('Native conversation existing')).toBeTruthy();

  view.rerender(<AgentConversation {...props} sessionId="fresh" />);
  expect(await screen.findByText('Codex conversation fresh')).toBeTruthy();
  expect(screen.queryByText('This thread is not available in the immutable agent inventory.')).toBeNull();
  expect(reads).toBeGreaterThan(1);
});

test('keeps a Project grouping warning with its created native thread', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 2, applied_version: 2, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  let grouped = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => new Response(JSON.stringify(input.endsWith('/thread-projects') ? { threads: grouped ? [{ session_id: 'created', project_id: 'website' }] : [] } : [
    { session_id: 'created', title: 'Created thread', runtime_type: 'opencode' },
    { session_id: 'other', title: 'Other thread', runtime_type: 'opencode' },
  ]))));
  const props = { organization: 'one', agent, csrfToken: 'csrf', hidden: false, refreshKey: 0, threadListTarget: null, newThreadRequest: undefined, onNewThreadStarted: vi.fn(), threadPageSize: 6, onThreadSelect: vi.fn(), onSessionChange: vi.fn(), onError: vi.fn(), onOpen: vi.fn() };
  const view = render(<AgentConversation {...props} sessionId={undefined} />);
  await screen.findByRole('textbox', { name: 'Composer draft' });

  window.dispatchEvent(new CustomEvent('fesnyng-project-grouping-warning', { detail: {
    state: 'ungrouped', requested_project_id: 'website',
    retry_path: '/organizations/one/agents/agent-one/sessions/created/project',
    detail: 'Grouping write failed.',
  } }));
  view.rerender(<AgentConversation {...props} sessionId="created" />);
  expect(await screen.findByText(/Grouping write failed/)).toBeTruthy();

  grouped = true;
  view.rerender(<AgentConversation {...props} projectRevision={1} sessionId="created" />);
  await waitFor(() => expect(screen.queryByText(/Grouping write failed/)).toBeNull());

  view.rerender(<AgentConversation {...props} sessionId="other" />);
  expect(screen.queryByText(/Grouping write failed/)).toBeNull();
});

test('holds a new target-harness thread until its configuration applies', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 3, applied_version: 2, configuration_status: 'pending', configuration: { execution_type: 'docker', runtime_type: 'codex', provider: 'openai', model: 'gpt-6-astra', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  vi.stubGlobal('fetch', vi.fn(async (input: string) => new Response(JSON.stringify(input.endsWith('/sessions') ? [{ session_id: 'old-codex', title: 'Frozen history', runtime_type: 'codex', frozen_at: 0 }] : []))));
  render(<AgentConversation organization="one" agent={agent} csrfToken="csrf" hidden={false} refreshKey={0} sessionId={undefined} threadListTarget={null} newThreadRequest={1} onNewThreadStarted={vi.fn()} threadPageSize={6} onThreadSelect={vi.fn()} onSessionChange={vi.fn()} onError={vi.fn()} onOpen={vi.fn()} />);

  expect(await screen.findByText('The selected harness is still applying. New threads will be available after configuration finishes.')).toBeTruthy();
  expect(screen.queryByText('Codex conversation')).toBeNull();
});

test('keeps the native history mounted but suspends execution after a managed workspace is removed', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'removed-thread', title: 'Retained history', runtime_type: 'opencode' }]);
    if (input.endsWith('/sessions/removed-thread/workspace')) return Response.json({ workspace_id: 'workspace-one', generation: 2, safety_digest: 'a'.repeat(64), state: 'removed', kind: 'repository', directory: '/workspaces/one/agent-one/removed-thread', repository: { state: 'available' }, git: { kind: 'repository', state: 'safe', branch: 'work/removed-thread', dirty: 0, untracked: 0, ignored: 0, ahead: 0, upstream: 'origin/test' }, history: { state: 'verified' }, cleanup: { remove: { available: false, reason: 'Already removed.' }, discard: { available: false, reason: 'Already removed.' }, replace: { available: true } } });
    return Response.json([]);
  }));
  render(<AgentConversation organization="one" agent={agent} csrfToken="csrf" hidden={false} refreshKey={0} sessionId="removed-thread" threadListTarget={null} newThreadRequest={undefined} onNewThreadStarted={vi.fn()} threadPageSize={6} onThreadSelect={vi.fn()} onSessionChange={vi.fn()} onError={vi.fn()} onOpen={vi.fn()} />);

  expect(await screen.findByText('Native conversation removed-thread')).toBeTruthy();
  expect(await screen.findByText(/^Removed · Repository worktree$/)).toBeTruthy();
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
  expect(screen.getByRole('button', { name: 'Prepare replacement' })).toBeTruthy();
});

test('keeps existing sidebar thread navigation available for a removed workspace without restoring execution', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'removed-thread', title: 'Removed history', runtime_type: 'opencode' }, { session_id: 'other-thread', title: 'Other history', runtime_type: 'opencode' }]);
    if (input.endsWith('/sessions/removed-thread/workspace')) return Response.json({ workspace_id: 'workspace-one', generation: 2, safety_digest: 'a'.repeat(64), state: 'removed', kind: 'ordinary', directory: '/workspaces/one/agent-one/removed-thread', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false }, discard: { available: false }, replace: { available: true } } });
    return Response.json([]);
  }));
  const target = document.body.appendChild(document.createElement('div'));
  const selected = vi.fn();
  render(<AgentConversation organization="one" agent={agent} csrfToken="csrf" hidden={false} refreshKey={0} sessionId="removed-thread" threadListTarget={target} newThreadRequest={1} onNewThreadStarted={vi.fn()} threadPageSize={6} onThreadSelect={vi.fn()} onSessionChange={selected} onError={vi.fn()} onOpen={vi.fn()} />);

  expect(await screen.findByText('This workspace was removed. Prepare its replacement before continuing.')).toBeTruthy();
  expect(within(target).getByRole('button', { name: 'Select other sidebar thread' })).toBeTruthy();
  fireEvent.click(within(target).getByRole('button', { name: 'Select other sidebar thread' }));
  expect(selected).toHaveBeenCalledWith('other-thread');
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
  target.remove();
});

test('keeps execution blocked when a removed workspace becomes durably unavailable', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  let durableUnavailable = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'thread', title: 'History', runtime_type: 'opencode' }]);
    if (input.endsWith('/sessions/thread/workspace')) return Response.json(durableUnavailable
      ? { workspace_id: 'workspace-one', generation: 2, safety_digest: null, state: 'unavailable', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false }, discard: { available: false }, replace: { available: false } } }
      : { workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'removed', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false }, discard: { available: false }, replace: { available: true } } });
    return Response.json([]);
  }));
  const props = { organization: 'one', agent, csrfToken: 'csrf', hidden: false, refreshKey: 0, sessionId: 'thread', threadListTarget: null, newThreadRequest: undefined, onNewThreadStarted: vi.fn(), threadPageSize: 6, onThreadSelect: vi.fn(), onSessionChange: vi.fn(), onError: vi.fn(), onOpen: vi.fn() };
  const view = render(<AgentConversation {...props} workspaceInspectionRevision={0} />);
  expect(await screen.findByText('This workspace was removed. Prepare its replacement before continuing.')).toBeTruthy();
  durableUnavailable = true;
  view.rerender(<AgentConversation {...props} workspaceInspectionRevision={1} />);
  expect(await screen.findByText('This workspace cannot run until its state is resolved. Inspect the workspace before continuing.')).toBeTruthy();
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
});

test('keeps execution available when a ready workspace only lacks optional Git evidence', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'thread', title: 'History', runtime_type: 'opencode' }]);
    if (input.endsWith('/sessions/thread/workspace')) return Response.json({ workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false, reason: 'Workspace safety could not be verified.' }, discard: { available: false, reason: 'Workspace safety could not be verified.' }, replace: { available: false, reason: 'Remove first.' } } });
    return Response.json([]);
  }));
  render(<AgentConversation organization="one" agent={agent} csrfToken="csrf" hidden={false} refreshKey={0} sessionId="thread" threadListTarget={null} newThreadRequest={undefined} onNewThreadStarted={vi.fn()} threadPageSize={6} onThreadSelect={vi.fn()} onSessionChange={vi.fn()} onError={vi.fn()} onOpen={vi.fn()} />);

  expect(await screen.findByLabelText('Composer draft')).toBeTruthy();
  expect(screen.queryByText('This workspace cannot run until its state is resolved. Inspect the workspace before continuing.')).toBeNull();
});

test('remembers a removed workspace after visiting another thread when inspection goes offline', async () => {
  const agent: Agent = { id: 'agent-one', organization_id: 'one', name: 'Researcher', title: 'Research', host_id: 'host', reports_to_agent_id: null, desired_version: 1, applied_version: 1, configuration_status: 'applied', configuration: { execution_type: 'docker', runtime_type: 'opencode', provider: 'openai', model: 'gpt', profile_id: null, workspace: 'default', instructions: '', skills: [] } };
  let offline = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/sessions')) return Response.json(['removed-thread', 'other-thread'].map((session_id) => ({ session_id, title: session_id, runtime_type: 'opencode' })));
    if (input.endsWith('/workspace')) {
      if (offline) return Response.json({ detail: 'Host offline' }, { status: 503 });
      const removed = input.includes('/removed-thread/');
      return Response.json({ workspace_id: removed ? 'first' : 'second', generation: 2, safety_digest: 'a'.repeat(64), state: removed ? 'removed' : 'ready', kind: 'ordinary', directory: '/managed/thread', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false }, discard: { available: false }, replace: { available: removed } } });
    }
    return Response.json([]);
  }));
  const thread = (sessionId: string) => <AgentConversation organization="one" agent={agent} csrfToken="csrf" hidden={false} refreshKey={0} sessionId={sessionId} threadListTarget={null} newThreadRequest={undefined} onNewThreadStarted={vi.fn()} threadPageSize={6} onThreadSelect={vi.fn()} onSessionChange={vi.fn()} onError={vi.fn()} onOpen={vi.fn()} />;
  const view = render(thread('removed-thread'));
  expect(await screen.findByText(/^Removed · Ordinary directory$/)).toBeTruthy();
  view.rerender(thread('other-thread'));
  expect(await screen.findByText(/^Ready · Ordinary directory$/)).toBeTruthy();
  expect(screen.getByLabelText('Composer draft')).toBeTruthy();
  offline = true;
  view.rerender(thread('removed-thread'));
  expect(await screen.findByText(/Workspace evidence is unavailable/)).toBeTruthy();
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
  expect(screen.getByText('Native conversation removed-thread')).toBeTruthy();
});

test.each(['available', 'failed-refresh', 'lost-response'])('keeps the conversation gated after overview removal (%s)', async (receiptMode) => {
  const session = { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' };
  let workspaceRemoved = false;
  let recoveredInspection = false;
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input === '/api/auth/session') return Response.json(session);
    if (input === '/api/organizations') return Response.json([{ id: 'one', name: 'Organization' }]);
    if (input.endsWith('/members')) return Response.json([{ user_id: 'human', role: 'member' }]);
    if (input.endsWith('/agents') && !input.endsWith('/sessions')) return Response.json([{ id: 'agent-one', name: 'Researcher', title: 'Research', configuration: { workspace: 'default' } }]);
    if (input.endsWith('/sessions')) return Response.json([{ session_id: 'thread-one', title: 'Research task', runtime_type: 'opencode' }]);
    if (input.endsWith('/thread-projects')) return Response.json({ threads: [{ session_id: 'thread-one', project_id: null }] });
    if (input.includes('/projects')) return Response.json([]);
    if (input.endsWith('/sessions/thread-one/workspace/remove')) { workspaceRemoved = true; if (receiptMode === 'lost-response') throw new Error('Lost response'); return Response.json({ workspace_id: 'workspace-one', generation: 2, safety_digest: 'b'.repeat(64), state: 'removed', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread-one', repository: { state: 'absent' }, git: { kind: 'ordinary', state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false, reason: 'Already removed.' }, discard: { available: false, reason: 'Already removed.' }, replace: { available: true } } }); }
    if (input.endsWith('/sessions/thread-one/workspace') && workspaceRemoved) {
      if (receiptMode === 'failed-refresh' || (receiptMode === 'lost-response' && recoveredInspection)) return Response.json({ detail: 'Host offline' }, { status: 503 });
      recoveredInspection = true;
    }
    if (input.endsWith('/sessions/thread-one/workspace')) return Response.json(workspaceRemoved ? { workspace_id: 'workspace-one', generation: 2, safety_digest: 'b'.repeat(64), state: 'removed', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread-one', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false, reason: 'Already removed.' }, discard: { available: false, reason: 'Already removed.' }, replace: { available: true } } } : { workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'ordinary', directory: '/workspaces/one/agent-one/thread-one', repository: { state: 'absent' }, git: { kind: 'ordinary', state: 'safe', branch: null, dirty: 0, untracked: 0, ignored: 0, ahead: 0, upstream: null }, history: { state: 'verified' }, cleanup: { remove: { available: true }, discard: { available: true }, replace: { available: false, reason: 'Remove first.' } } });
    return Response.json([]);
  }));
  window.history.replaceState(null, '', '/#organization=one&agent=agent-one&thread=thread-one');
  render(<App />);
  expect(await screen.findByText('Native conversation thread-one')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Workspaces' }));
  expect(await screen.findByRole('heading', { name: 'Workspaces', level: 2 })).toBeTruthy();
  const employeeWorkspaces = await screen.findByRole('region', { name: 'Employee Workspaces' });
  expect(within(employeeWorkspaces).getByText('/workspaces/one/agent-one/thread-one')).toBeTruthy();
  expect(screen.getByText('Native conversation thread-one')).toBeTruthy();
  fireEvent.click(within(employeeWorkspaces).getByRole('button', { name: 'Remove workspace' }));
  expect(await within(employeeWorkspaces).findByText(/^Removed · Ordinary directory$/)).toBeTruthy();
  fireEvent.click(within(employeeWorkspaces).getByRole('button', { name: /Research task/ }));
  expect(await screen.findByText('This workspace was removed. Prepare its replacement before continuing.')).toBeTruthy();
  expect(screen.queryByLabelText('Composer draft')).toBeNull();
});

test('lets an existing member create another organization without losing the original membership', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string, options: RequestInit) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? options.method === 'POST' ? {id: 'two', name: 'Second organization'} : [{id: 'one', name: 'First organization'}]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{user_id: 'human', role: 'member'}] : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  fireEvent.keyDown(await screen.findByRole('button', { name: 'Switch organization: First organization' }), { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Create organization' }));
  fireEvent.change(await screen.findByLabelText('Organization name'), { target: { value: 'Second organization' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create organization' }));
  fireEvent.keyDown(await screen.findByRole('button', { name: 'Switch organization: Second organization' }), { key: 'ArrowDown' });
  expect(await screen.findByRole('menuitemradio', { name: 'First organization' })).toBeTruthy();
  expect(screen.getByRole('menuitemradio', { name: 'Second organization' })).toBeTruthy();
});

test('shows an unread result on its agent without a standalone Activity view', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'Organization' }]
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'agent', name: 'Researcher', configuration: { workspace: 'default' } }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/sessions') ? [{ session_id: 'thread', title: 'Result' }]
      : input.endsWith('/dispatches') ? [{ id: 'delivery', session_id: 'thread', state: 'completed', updated_at: 1, author: { name: 'Member' }, payload: { mode: 'queued', text: 'Research' }, outcome: { kind: 'native_run_completed', message_id: 'result' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  const pip = await screen.findByRole('img', { name: 'Unread result' });
  expect(pip.closest('button')?.textContent).toContain('Researcher');
  expect(screen.getByRole('button', { name: /Unread result.*Researcher/ })).toBe(pip.closest('button'));
  expect(screen.queryByRole('button', { name: 'Activity' })).toBeNull();
});

test('uses accessible shell icons, orders search before agents, and restores focus after preferences', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'owner', display_name: 'Owner' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'owner', role: 'owner' }]
      : input.endsWith('/agents') ? [{ id: 'agent', name: 'Researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : input.endsWith('/policy') ? { desired_version: 1, configuration: { default_permission: 'allow', mandatory_permissions: [], allow_thread_overrides: true } }
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  await screen.findByRole('button', { name: /Researcher/, pressed: false });
  const search = screen.getByRole('searchbox', { name: 'Search organization threads' });
  const heading = screen.getByText('Agents · 1');
  expect(search.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /Researcher/, pressed: false }));
  expect(screen.queryByRole('button', { name: 'Threads' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Memory' }).getAttribute('title')).toBe('Memory');
  expect((await screen.findByRole('button', { name: 'Agent settings' })).getAttribute('title')).toBe('Agent settings');
  expect(screen.getByRole('button', { name: 'Refresh workspace' }).getAttribute('title')).toBe('Refresh workspace');
  const preferences = screen.getByRole('button', { name: 'User preferences' });
  expect(screen.getByRole('button', { name: 'Sign out' }).getAttribute('title')).toBe('Sign out');
  preferences.focus();
  fireEvent.click(preferences);
  expect(await screen.findByRole('dialog', { name: 'User preferences' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Close preferences' }));
  expect(screen.queryByRole('dialog', { name: 'User preferences' })).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(preferences));
});

test('expands only the selected agent and searches titles beyond the sidebar page', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input.endsWith('/agents/beta/sessions') ? Array.from({ length: 9 }, (_, i) => ({ session_id: `thread-${i}`, title: `Investigation ${i}` })) : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: /Alpha/, pressed: false }));
  expect(await screen.findByRole('region', { name: 'Alpha threads' })).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Beta threads' })).toBeNull();
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search organization threads' }), { target: { value: 'Investigation 8' } });
  fireEvent.click(await screen.findByRole('button', { name: /Investigation 8/ }));
  expect(await screen.findByText('Native conversation thread-8')).toBeTruthy();
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search organization threads' }), { target: { value: '' } });
  expect(await screen.findByRole('region', { name: 'Beta threads' })).toBeTruthy();
  expect(screen.queryByRole('region', { name: 'Alpha threads' })).toBeNull();
  const switcher = screen.getByRole('button', { name: 'Switch organization: Organization' });
  fireEvent.keyDown(switcher, { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Reporting chart for Organization' }));
  expect(screen.getByText('Native conversation thread-8').closest('[hidden]')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open sidebar thread' }));
  expect(screen.getByText('Native conversation thread-8').closest('[hidden]')).toBeNull();
  fireEvent.keyDown(switcher, { key: 'ArrowDown' });
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Reporting chart for Organization' }));
  fireEvent.click(screen.getByRole('button', { name: 'Create sidebar thread' }));
  expect(screen.getByText('Native conversation thread-8').closest('[hidden]')).toBeNull();
});

test('starts a new native thread from every agent card without selecting the card button first', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents/beta/opencode/session') && init.method === 'POST' ? { id: 'beta-new', title: 'New thread' }
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));

  render(<App />);
  const alpha = await screen.findByRole('button', { name: /Alpha/, pressed: false });
  const beta = screen.getByRole('button', { name: /Beta/, pressed: false });
  expect(screen.getByRole('button', { name: 'New thread for Alpha' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Beta' }));
  expect(alpha.getAttribute('aria-pressed')).toBe('false');
  await waitFor(() => expect(beta.getAttribute('aria-pressed')).toBe('true'));
  expect(await screen.findByRole('textbox', { name: 'Composer draft' })).toBeTruthy();
});

test('switches to Projects without replacing the current conversation draft and reveals its Project group', async () => {
  window.history.replaceState(null, '', '/#organization=org&agent=agent&thread=thread-a');
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'agent', name: 'Researcher', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects?include_archived=true' ? [{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : input.endsWith('/agents/agent/thread-projects') ? { threads: [{ session_id: 'thread-a', project_id: 'website' }] }
      : input.endsWith('/agents/agent/sessions') ? [{ session_id: 'thread-a', title: 'Current work' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : [];
    return Response.json(body);
  }));
  render(<App />);
  const draft = await screen.findByRole('textbox', { name: 'Composer draft' });
  fireEvent.change(draft, { target: { value: 'Keep this draft while organizing.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Projects' }));
  expect(await screen.findByRole('button', { name: 'Website', pressed: true })).toBeTruthy();
  expect(screen.getByRole('textbox', { name: 'Composer draft' })).toHaveProperty('value', 'Keep this draft while organizing.');
});

test('retries a failed repository preparation with the same creation identity', async () => {
  let nativePosts = 0;
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/opencode/session') && init.method === 'POST') {
      nativePosts += 1;
      return nativePosts === 1
        ? Response.json({ detail: { code: 'workspace_preparation_failed', detail: 'Git authentication failed.' } }, { status: 422 })
        : Response.json({ id: 'prepared-thread', title: 'Prepared thread', fesnyng_project_grouping: { state: 'ungrouped', requested_project_id: 'website', retry_path: '/organizations/org/agents/alpha/sessions/prepared-thread/project', detail: 'Project assignment needs retry.' } });
    }
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'website', organization_id: 'org', name: 'Website', description: '', target_repository_url: 'https://example.test/website.git', default_checkout_branch: 'test', archived: false }]
      : [];
    return Response.json(body);
  });
  vi.stubGlobal('fetch', request);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'website' } });
  fireEvent.change(screen.getByLabelText('Starting branch'), { target: { value: 'release' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(await screen.findByText('Git authentication failed.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Retry preparation' }));
  expect(await screen.findByText('Native conversation prepared-thread')).toBeTruthy();
  expect(await screen.findByText(/Project assignment needs retry/)).toBeTruthy();
  const creations = request.mock.calls.filter(([url, init]) => String(url).endsWith('/opencode/session') && (init as RequestInit).method === 'POST').map(([, init]) => JSON.parse((init as RequestInit).body as string));
  expect(creations).toHaveLength(2);
  expect(creations[0]).toEqual({ project_id: 'website', checkout_branch: 'release', creation_id: expect.any(String) });
  expect(creations[1]).toEqual(creations[0]);
});

test('keeps the conversation selected after navigating away from a pending Project thread creation', async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') return pending;
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : input.endsWith('/agents/beta/sessions') ? [{ session_id: 'thread-b', title: 'Existing B' }]
      : [];
    return Response.json(body);
  }));
  window.history.replaceState(null, '', '/#organization=org&agent=beta&thread=thread-b');
  render(<App />);
  expect(await screen.findByText('Native conversation thread-b')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(await screen.findByText('Preparing this thread workspace…')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /Beta/, pressed: false }));
  expect(await screen.findByText('Native conversation thread-b')).toBeTruthy();

  finish(Response.json({ id: 'thread-a', title: 'Created A' }));

  await waitFor(() => expect(screen.getByText('Native conversation thread-b')).toBeTruthy());
  expect(screen.queryByText('Native conversation thread-a')).toBeNull();
});

test('does not restore an unmounted organization when its creation finishes late', async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/organizations/org-a/agents/alpha/opencode/session') && init.method === 'POST') return pending;
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org-a', name: 'Organization A' }, { id: 'org-b', name: 'Organization B' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input === '/api/organizations/org-a/agents' ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org-b/agents' ? [{ id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org-a/projects' ? [{ id: 'project-a', organization_id: 'org-a', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : [];
    return Response.json(body);
  }));
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Switch organization: Organization A' }), { button: 0, ctrlKey: false });
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Organization B' }));
  expect(await screen.findByRole('heading', { name: 'Reporting chart' })).toBeTruthy();
  const locationAfterSwitch = window.location.hash;
  finish(Response.json({ id: 'late-a', title: 'Late A' }));
  await waitFor(() => expect(window.location.hash).toBe(locationAfterSwitch));
});

test('keeps an existing conversation mounted when a pending Project thread creation fails elsewhere', async () => {
  let reject!: (cause: Error) => void;
  const pending = new Promise<Response>((_resolve, rejectPromise) => { reject = rejectPromise; });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') return pending;
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : input.endsWith('/agents/beta/sessions') ? [{ session_id: 'thread-b', title: 'Existing B' }]
      : [];
    return Response.json(body);
  }));
  window.history.replaceState(null, '', '/#organization=org&agent=beta&thread=thread-b');
  render(<App />);
  expect(await screen.findByText('Native conversation thread-b')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  fireEvent.click(screen.getByRole('button', { name: /Beta/, pressed: false }));
  expect(await screen.findByText('Native conversation thread-b')).toBeTruthy();

  reject(new Error('Workspace preparation failed.'));

  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(screen.getByText('Native conversation thread-b')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /Alpha/, pressed: false }));
  expect(await screen.findByRole('alert')).toHaveProperty(
    'textContent',
    expect.stringContaining('Thread preparation may have started.'),
  );
});

test('keeps an existing thread available while another Project thread prepares for the same employee', async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') return pending;
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : input.endsWith('/agents/alpha/sessions') ? [{ session_id: 'existing-a', title: 'Existing A' }]
      : [];
    return Response.json(body);
  }));
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search organization threads' }), { target: { value: 'Existing A' } });
  fireEvent.click(await screen.findByRole('button', { name: 'Existing A' }));

  expect(await screen.findByText('Native conversation existing-a')).toBeTruthy();
  expect(screen.getByText('Preparing this thread workspace…')).toBeTruthy();
  finish(Response.json({ id: 'new-a', title: 'Created A' }));
  await waitFor(() => expect(screen.queryByText('Preparing this thread workspace…')).toBeNull());
});

test('keeps an uncertain creation recoverable while a distinct thread succeeds', async () => {
  let alphaPosts = 0;
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') {
      alphaPosts += 1;
      return alphaPosts === 1
        ? Response.json({ detail: 'The receipt was lost.' }, { status: 503 })
        : Response.json({ id: 'recovered-a', title: 'Recovered A' });
    }
    if (input.endsWith('/agents/beta/opencode/session') && init.method === 'POST') return Response.json({ id: 'new-b', title: 'Created B' });
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : [];
    return Response.json(body);
  });
  vi.stubGlobal('fetch', request);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Thread preparation may have started.'));
  const firstAlphaRequest = request.mock.calls.find(([url, init]) => String(url).endsWith('/agents/alpha/opencode/session') && (init as RequestInit).method === 'POST');
  const firstCreation = JSON.parse((firstAlphaRequest![1] as RequestInit).body as string).creation_id;
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Beta' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Start thread' }));
  expect(await screen.findByText('Native conversation new-b')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Check preparation again' }));
  expect(await screen.findByText('Native conversation recovered-a')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Check preparation again' })).toBeNull();
  const alphaBodies = request.mock.calls.filter(([url, init]) => String(url).endsWith('/agents/alpha/opencode/session') && (init as RequestInit).method === 'POST').map(([, init]) => JSON.parse((init as RequestInit).body as string));
  expect(alphaBodies).toHaveLength(2);
  expect(alphaBodies[1].creation_id).toBe(firstCreation);
});

test('lets a confirmed Project workspace failure be left before a distinct new thread is started', async () => {
  let posts = 0;
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') {
      posts += 1;
      return posts === 1
        ? Response.json({ detail: { code: 'workspace_preparation_failed', detail: 'The configured branch does not exist.' } }, { status: 422 })
        : Response.json({ id: 'replacement-thread', title: 'Replacement thread' });
    }
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : [];
    return Response.json(body);
  });
  vi.stubGlobal('fetch', request);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(await screen.findByText('The configured branch does not exist.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Start a different thread' }));
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  expect(await screen.findByText('Native conversation replacement-thread')).toBeTruthy();
  const bodies = request.mock.calls.filter(([url, init]) => String(url).endsWith('/opencode/session') && (init as RequestInit).method === 'POST').map(([, init]) => JSON.parse((init as RequestInit).body as string));
  expect(bodies).toHaveLength(2);
  expect(bodies[1].creation_id).not.toBe(bodies[0].creation_id);
});

test('offers a visible return to the employee with a pending Project preparation', async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/agents/alpha/opencode/session') && init.method === 'POST') return pending;
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', configuration: { workspace: 'default' } }, { id: 'beta', name: 'Beta', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/org/projects' ? [{ id: 'project-a', organization_id: 'org', name: 'Project A', description: '', target_repository_url: null, default_checkout_branch: null, archived: false }]
      : [];
    return Response.json(body);
  });
  vi.stubGlobal('fetch', request);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'New thread for Alpha' }));
  fireEvent.change(await screen.findByRole('combobox', { name: 'Project' }), { target: { value: 'project-a' } });
  fireEvent.click(screen.getByRole('button', { name: 'Start thread' }));
  fireEvent.click(screen.getByRole('button', { name: /Beta/, pressed: false }));
  fireEvent.click(screen.getByRole('button', { name: 'New thread for Beta' }));
  expect(await screen.findByRole('button', { name: 'Return to Alpha preparation' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Return to Alpha preparation' }));
  expect(await screen.findByText('Preparing this thread workspace…')).toBeTruthy();
  expect(request.mock.calls.filter(([url, init]) => String(url).endsWith('/agents/beta/opencode/session') && (init as RequestInit).method === 'POST')).toHaveLength(0);
  finish(Response.json({ id: 'new-a', title: 'Created A' }));
  expect(await screen.findByText('Native conversation new-a')).toBeTruthy();
});

test('keeps the role selectable inside the compact agent card while threads remain outside it', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input.endsWith('/agents') ? [{ id: 'alpha', name: 'Alpha', title: 'Research', configuration: { workspace: 'default' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));

  render(<App />);
  const alpha = await screen.findByRole('button', { name: /Alpha/, pressed: false });
  const role = within(alpha.closest('.app-sidebar')!).getByText('Research');
  const choice = role.closest('button');
  const card = role.closest('.agent-card');
  expect(choice?.className).toContain('agent-choice');
  expect(card?.contains(screen.getByRole('button', { name: 'New thread for Alpha' }))).toBe(true);
  fireEvent.click(role);
  expect(choice?.getAttribute('aria-pressed')).toBe('true');
  expect(await screen.findByRole('region', { name: 'Alpha threads' }).then((threads) => card?.contains(threads))).toBe(false);
});
