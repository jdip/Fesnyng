import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App } from './App';
vi.mock('./Conversation', async () => {
  const { createPortal } = await import('react-dom');
  const { useState } = await import('react');
  return { Conversation: ({ sessionId, threadListTarget, onThreadSelect }: { sessionId?: string; threadListTarget?: HTMLElement; onThreadSelect?: () => void }) => {
    const [draft, setDraft] = useState('');
    return <><p>Native conversation {sessionId}</p><textarea aria-label="Composer draft" value={draft} onChange={(event) => setDraft(event.target.value)} />{threadListTarget && createPortal(<><button onClick={onThreadSelect}>Open sidebar thread</button><button onClick={onThreadSelect}>Create sidebar thread</button></>, threadListTarget)}</>;
  } };
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

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
  expect(pip.parentElement?.parentElement?.className).toContain('agent-name-row');
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
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'org', name: 'Organization' }]
      : input.endsWith('/workspace-preferences') ? { thread_list_page_size: 6 }
      : input.endsWith('/thread-acknowledgements') ? { acknowledgements: [] }
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
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
  expect(beta.getAttribute('aria-pressed')).toBe('true');
  expect(await screen.findByRole('textbox', { name: 'Composer draft' })).toBeTruthy();
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
