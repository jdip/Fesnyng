import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LifecycleControls } from './LifecycleControls';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const path = '/api/organizations/org/agents/agent/runtime';
const lifecyclePath = '/api/organizations/org/agents/agent/lifecycle';

function runtime(lifecycle_state: string, desired_state = lifecycle_state) {
  return { lifecycle_state, desired_state, container: { id: 'container' } };
}

test('starts a stopped agent through its organization-scoped lifecycle endpoint', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    void options;
    if (url === path) return new Response(JSON.stringify(runtime('stopped')));
    if (url === lifecyclePath) return new Response(JSON.stringify(runtime('running')));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  expect(await screen.findByText('Stopped')).toBeTruthy();
  const rebuild = screen.getByRole('button', { name: 'Rebuild' });
  rebuild.focus();
  fireEvent.click(rebuild);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  const start = screen.getByRole('button', { name: 'Start' });
  start.focus();
  fireEvent.click(start);

  await waitFor(() => expect(request.mock.calls.some(([url]) => url === lifecyclePath)).toBe(true));
  const [, options] = request.mock.calls.find(([url]) => url === lifecyclePath)!;
  expect(JSON.parse(options!.body as string)).toEqual({ action: 'start', confirmed: true });
  expect(await screen.findByText('Running')).toBeTruthy();
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Refresh' })));
});

test('does not send an ordinary lifecycle action when its confirmation is cancelled', async () => {
  const request = vi.fn(async (url: string) => {
    void url;
    return new Response(JSON.stringify(runtime('running')));
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  await screen.findByText('Running');
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
  expect(screen.getByRole('dialog')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

  expect(request.mock.calls.some(([url]) => url === lifecyclePath)).toBe(false);
});

test('keeps keyboard focus on Start when the immediate request fails', async () => {
  const request = vi.fn(async (url: string) => {
    if (url === path) return new Response(JSON.stringify(runtime('stopped')));
    return new Response(JSON.stringify({ detail: 'Host is unavailable.' }), { status: 503 });
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  await screen.findByText('Stopped');
  const start = screen.getByRole('button', { name: 'Start' });
  start.focus();
  fireEvent.click(start);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Host is unavailable.');
  await waitFor(() => expect(document.activeElement).toBe(start));
});

test('returns keyboard focus to the opener on close and Refresh after a successful stop removes it', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    void options;
    if (url === path) return new Response(JSON.stringify(runtime('running')));
    if (url === lifecyclePath) return new Response(JSON.stringify(runtime('stopped')));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  await screen.findByText('Running');
  const stop = screen.getByRole('button', { name: 'Stop' });
  stop.focus();
  fireEvent.click(stop);
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  await waitFor(() => expect(document.activeElement).toBe(stop));

  fireEvent.click(stop);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(document.activeElement).toBe(stop));

  fireEvent.click(stop);
  fireEvent.click(screen.getByRole('button', { name: 'Confirm stop' }));
  await screen.findByText('Stopped');
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Refresh' })));
});

test('offers only the host-validated recovery retry and keeps its action explicit', async () => {
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    void options;
    if (url === path) return new Response(JSON.stringify({ ...runtime('recovery_required', 'running'), retry_action: 'start', error: 'Reconcile retained delivery effects before restarting.' }));
    if (url === lifecyclePath) return new Response(JSON.stringify(runtime('running')));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  expect(await screen.findByText('Recovery required')).toBeTruthy();
  expect(screen.getByText('Reconcile retained delivery effects before restarting.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Retry start' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Rebuild' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Retry start' }));
  await waitFor(() => expect(request.mock.calls.some(([url]) => url === lifecyclePath)).toBe(true));
  const [, options] = request.mock.calls.find(([url]) => url === lifecyclePath)!;
  expect(JSON.parse(options!.body as string)).toEqual({ action: 'start', confirmed: true });
});

test('requires the host challenge code and keeps a failed confirmation available to retry', async () => {
  let attempts = 0;
  const request = vi.fn(async (url: string, options: RequestInit = {}) => {
    void options;
    if (url === path) return new Response(JSON.stringify(runtime('running')));
    if (url !== lifecyclePath) throw new Error(`Unexpected request: ${url}`);
    attempts += 1;
    if (attempts === 1) return new Response(JSON.stringify({ ...runtime('running'), confirmation_required: true, confirmation_code: 'owl-17', message: 'Active work must be confirmed.' }));
    if (attempts === 2) return new Response(JSON.stringify({ detail: 'The agent changed state; confirm again.' }), { status: 409 });
    return new Response(JSON.stringify(runtime('stopped')));
  });
  vi.stubGlobal('fetch', request);
  render(<LifecycleControls organization="org" agent="agent" agentName="Researcher" csrf="csrf" />);

  await screen.findByText('Running');
  fireEvent.click(screen.getByRole('button', { name: 'Restart' }));
  expect(screen.getByRole('heading', { name: 'Restart Researcher' })).toBeTruthy();
  expect(screen.queryByLabelText('Confirmation code')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restart' }));
  const code = await screen.findByLabelText('Confirmation code');
  expect(screen.getByText('owl-17')).toBeTruthy();
  fireEvent.change(code, { target: { value: 'owl-17' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm restart' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'The agent changed state; confirm again.');

  fireEvent.click(screen.getByRole('button', { name: 'Confirm restart' }));
  expect(await screen.findByText('Stopped')).toBeTruthy();
  const lifecycleBodies = request.mock.calls.filter(([url]) => url === lifecyclePath).map(([, options]) => JSON.parse(options!.body as string));
  expect(lifecycleBodies).toEqual([
    { action: 'restart', confirmed: true },
    { action: 'restart', confirmed: true, code: 'owl-17' },
    { action: 'restart', confirmed: true, code: 'owl-17' },
  ]);
});
