import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ThreadArtifact } from './ThreadArtifact';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test('reads an artifact only through its authorized thread scope', async () => {
  const request = vi.fn().mockResolvedValue(new Response(JSON.stringify({ type: 'text', content: 'Verified release evidence' })));
  vi.stubGlobal('fetch', request);
  render(<ThreadArtifact organization="org" agent="agent" session="thread" />);
  fireEvent.change(screen.getByLabelText('Workspace file'), { target: { value: 'release.txt' } });
  fireEvent.click(screen.getByRole('button', { name: 'Read file' }));
  expect(await screen.findByText('Verified release evidence')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/api/organizations/org/agents/agent/opencode/file/content?sessionID=thread&path=release.txt', expect.anything());
});
