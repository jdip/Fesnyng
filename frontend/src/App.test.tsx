import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { App } from './App';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('shows a connection only after the control plane responds successfully', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
    status: 'ok', service: 'control-plane', instance_id: 'control-example', schema_version: 1,
  }))));
  render(<App />);
  expect(await screen.findByText('Control plane connected')).toBeTruthy();
});

test('shows an unavailable state when the server rejects the request', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Unavailable', { status: 503 })));
  render(<App />);
  expect(await screen.findByText('Control plane unavailable')).toBeTruthy();
});

test.each([
  { status: 'ok', service: 'agent-host' },
  { status: 'failed', service: 'control-plane' },
  null,
  'not a health response',
])('rejects an unexpected health response: %j', async (health) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(health))));
  render(<App />);
  expect(await screen.findByText('Control plane unavailable')).toBeTruthy();
});

test('handles a failed connection without reporting success', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
  render(<App />);
  expect(await screen.findByText('Control plane unavailable')).toBeTruthy();
});
