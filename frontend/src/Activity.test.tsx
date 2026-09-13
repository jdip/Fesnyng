import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Activity } from './Activity';
import type { Agent } from './workspace-api';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('counts a thread once when it has multiple pending requests', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
    const result = url.endsWith('/sessions') ? [{ session_id: 'thread', title: 'Review' }] : url.endsWith('/dispatches') ? [] : [{ id: url.endsWith('/question') ? 'question' : 'permission', sessionID: 'thread' }];
    return new Response(JSON.stringify(result));
  }));
  render(<Activity organization="org" agents={[{ id: 'agent', name: 'Reviewer' }] as Agent[]} onOpen={vi.fn()} compact />);
  expect((await screen.findByRole('status')).textContent).toContain('1 thread has a result or needs attention.');
});
test('keeps useful activity from a healthy agent when another host is unavailable', async () => {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
    if (url.includes('/unavailable/')) return new Response(JSON.stringify({ detail: 'Host unavailable' }), { status: 503 });
    const result = url.endsWith('/sessions') ? [{session_id: 'thread', title: 'Release review'}] : url.endsWith('/dispatches') ? [{id: 'delivery', state: 'completed', author: {name: 'Reviewer'}, payload: {text: 'Result ready', mode: 'queued'}, updated_at: 1}] : [];
    return new Response(JSON.stringify(result));
  }));
  const open = vi.fn();
  render(<Activity organization="org" agents={[{id: 'available', name: 'Available agent'}, {id: 'unavailable', name: 'Offline agent'}] as Agent[]} onOpen={open} />);
  expect(await screen.findByText('Result ready')).toBeTruthy();
  expect(await screen.findByText(/Offline agent: Host unavailable/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Release review' }));
  expect(open).toHaveBeenCalledWith('available', 'thread');
});
