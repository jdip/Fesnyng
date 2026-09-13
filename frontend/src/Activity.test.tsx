import { afterEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Activity } from './Activity';
import type { Agent } from './workspace-api';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
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

test('preserves completed receipt baselines through a transient failed poll', async () => {
  vi.useFakeTimers();
  let sessionPolls = 0;
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
    if (url.endsWith('/sessions')) {
      sessionPolls += 1;
      if (sessionPolls === 2) return new Response(JSON.stringify({ detail: 'Host unavailable' }), { status: 503 });
      return new Response(JSON.stringify([{ session_id: 'thread', title: 'Release review' }]));
    }
    if (url.endsWith('/dispatches')) {
      const receipt = sessionPolls >= 4
        ? { id: 'delivery-two', state: 'completed', author: { name: 'Reviewer' }, payload: { text: 'New result', mode: 'queued' }, updated_at: 2 }
        : { id: 'delivery-one', state: 'completed', author: { name: 'Reviewer' }, payload: { text: 'Existing result', mode: 'queued' }, updated_at: 1 };
      return new Response(JSON.stringify([receipt]));
    }
    return new Response(JSON.stringify([]));
  }));

  render(<Activity organization="org" agents={[{ id: 'agent', name: 'Reviewer' }] as Agent[]} onOpen={vi.fn()} compact />);
  await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
  expect(sessionPolls).toBe(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.queryByRole('status')).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.queryByRole('status')).toBeNull();

  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(sessionPolls).toBe(4);
  expect(screen.getByRole('status').textContent).toContain('1 thread has a result or needs attention.');
});
