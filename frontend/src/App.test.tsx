import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { App } from './App';
vi.mock('./Conversation', () => ({ Conversation: ({ sessionId }: { sessionId?: string }) => <p>Native conversation {sessionId}</p> }));

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
      : input.endsWith('/members') ? [{ user_id: 'human', role: 'member' }]
      : input === '/api/organizations/one/agents' ? [{ id: 'agent-one', name: 'First researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : input === '/api/organizations/two/agents' ? [{ id: 'agent-two', name: 'Second researcher', title: 'Research', configuration: { workspace: 'default' } }]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  expect(await screen.findByRole('button', { name: /First researcher/, pressed: false })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Organization settings' })).toBeNull();
  fireEvent.change(screen.getByLabelText('Organization'), { target: { value: 'two' } });
  expect(screen.queryByRole('button', { name: /First researcher/ })).toBeNull();
  expect(await screen.findByRole('button', { name: /Second researcher/, pressed: false })).toBeTruthy();
});

test('uses the reporting chart as the default overview with compact settings navigation', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? { user: { id: 'owner', display_name: 'Owner' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? [{ id: 'one', name: 'First organization' }]
      : input.endsWith('/members') ? [{ user_id: 'owner', role: 'owner' }]
      : input.endsWith('/policy') ? { desired_version: 1, configuration: { default_permission: 'allow', mandatory_permissions: [], allow_thread_overrides: true } }
      : [];
    return new Response(JSON.stringify(body));
  }));

  render(<App />);

  expect(await screen.findByRole('heading', { name: 'Reporting chart' })).toBeTruthy();
  const settings = await screen.findByRole('button', { name: 'Organization settings' });
  expect(screen.getByText('Create your first agent to get started.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Overview' })).toBeNull();
  const chart = screen.getByRole('button', { name: 'Reporting chart' });
  expect(chart.getAttribute('title')).toBe('Reporting chart');
  expect(chart.getAttribute('aria-current')).toBe('page');
  expect(settings.getAttribute('title')).toBe('Organization settings');
  fireEvent.click(settings);
  expect(await screen.findByRole('heading', { name: 'Organization settings', level: 1 })).toBeTruthy();
  expect(settings.getAttribute('aria-current')).toBe('page');
});

test('restores a selected native thread after reloading an authorized organization', async () => {
  window.history.replaceState(null, '', '/#organization=one&agent=agent-one&thread=thread-one');
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string) => {
    const body = input === '/api/auth/session' ? {user: {id: 'human', display_name: 'Member'}, csrf_token: 'csrf-example'}
      : input === '/api/organizations' ? [{id: 'one', name: 'First organization'}]
      : input.endsWith('/members') ? [{user_id: 'human', role: 'member'}]
      : input.endsWith('/agents') ? [{id: 'agent-one', name: 'Researcher', title: 'Research', configuration: {workspace: 'default'}}]
      : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  expect(await screen.findByText('Native conversation thread-one')).toBeTruthy();
});

test('lets an existing member create another organization without losing the original membership', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string, options: RequestInit) => {
    const body = input === '/api/auth/session' ? { user: { id: 'human', display_name: 'Member' }, csrf_token: 'csrf-example' }
      : input === '/api/organizations' ? options.method === 'POST' ? {id: 'two', name: 'Second organization'} : [{id: 'one', name: 'First organization'}]
      : input.endsWith('/members') ? [{user_id: 'human', role: 'member'}] : [];
    return new Response(JSON.stringify(body));
  }));
  render(<App />);
  fireEvent.change(await screen.findByLabelText('Organization'), { target: { value: '__new__' } });
  fireEvent.change(await screen.findByLabelText('Organization name'), { target: { value: 'Second organization' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create organization' }));
  expect(await screen.findByRole('option', { name: 'First organization' })).toBeTruthy();
  expect(screen.getByRole('option', { name: 'Second organization' })).toBeTruthy();
});
