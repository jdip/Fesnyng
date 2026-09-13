import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AgentMemory } from './AgentMemory';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('edits explicit memory with its revision and preserves a conflicting draft', async () => {
  const request = vi.fn().mockImplementation(async (_url: string, options: RequestInit) => options.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'Memory revision conflict' }), { status: 409 })
    : new Response(JSON.stringify([{ key: 'preferences', content: 'Use short replies', revision: 3, author: { name: 'Researcher' }, updated_at: 1 }])));
  vi.stubGlobal('fetch', request);
  render(<AgentMemory organization="org" agent="agent" csrf="csrf-example" />);
  fireEvent.click(await screen.findByRole('button', { name: 'preferences' }));
  fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'Use detailed replies' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save memory' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Memory revision conflict');
  expect(screen.getByLabelText('Memory content')).toHaveProperty('value', 'Use detailed replies');
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/agents/agent/memory', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ key: 'preferences', content: 'Use detailed replies', expected_revision: 3 }) })));
});
