import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ThreadContext } from './ThreadContext';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('shows agent authorship and navigable source thread for peer contributions', async () => {
  const open = vi.fn();
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify([{ id: 'delivery', session_id: 'receiving-thread', state: 'completed', payload: { text: 'Release evidence is ready', mode: 'queued', origin_id: 'origin' }, author: { kind: 'agent', id: 'senior', name: 'Senior engineer', session_id: 'source-thread' }, updated_at: 1 }]))));
  render(<ThreadContext organization="org" agent="junior" session="receiving-thread" csrf="csrf-example" onOpen={open} />);
  fireEvent.click(screen.getByRole('button', { name: 'Thread activity' }));
  expect(await screen.findByText('Senior engineer')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open source thread' }));
  expect(open).toHaveBeenCalledWith('senior', 'source-thread');
});
