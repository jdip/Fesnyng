import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ThreadInformation } from './ThreadInformation';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const context = {
  repository: { state: 'available', name: 'sample-project' },
  branch: { state: 'available', name: 'feature' },
  changes: { state: 'available', added: 7, deleted: 3, untracked: 2, binaryFiles: 1 },
  subagents: { state: 'available', count: 2 },
  backgroundProcesses: { state: 'unavailable' },
};
const props = { baseUrl: 'http://localhost/api/organizations/org/agents/agent/opencode', csrfToken: 'csrf-example', session: { id: 'one', title: 'Investigate layout' }, refreshKey: 0, onRename: vi.fn() };

test('shows authoritative thread context and preserves the title until rename succeeds', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(context)));
  const rename = vi.fn(async () => {});
  render(<ThreadInformation {...props} onRename={rename} />);
  expect(await screen.findByText('sample-project')).toBeTruthy();
  expect(screen.getByText('feature')).toBeTruthy();
  expect(screen.getByText('+7 / −3')).toBeTruthy();
  expect(screen.getByText('2 untracked · 1 binary')).toBeTruthy();
  expect(within(screen.getByLabelText('Background processes')).getByText('Unavailable')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Rename thread' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Thread title' }), { target: { value: 'New title' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }));
  await waitFor(() => expect(rename).toHaveBeenCalledWith('New title'));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Thread title' })).toBeNull());
});

test('distinguishes absent repository from a failed refresh, without displaying fabricated zero counts', async () => {
  const request = vi.fn().mockResolvedValueOnce(Response.json({ ...context, repository: { state: 'absent' }, branch: { state: 'not_applicable' }, changes: { state: 'not_applicable' }, subagents: { state: 'unavailable' } })).mockRejectedValue(new Error('Offline'));
  vi.stubGlobal('fetch', request);
  const view = render(<ThreadInformation {...props} />);
  expect(await screen.findByText('No repository')).toBeTruthy();
  expect(within(screen.getByLabelText('Git changes')).getByText('Not applicable')).toBeTruthy();
  view.rerender(<ThreadInformation {...props} refreshKey={1} />);
  expect(await screen.findByText('Repository unavailable')).toBeTruthy();
  expect(screen.queryByText('No repository')).toBeNull();
  expect(within(screen.getByLabelText('Subagents')).getByText('Unavailable')).toBeTruthy();
});

test('late context from the prior session cannot replace the selected workspace', async () => {
  let resolveOld: (response: Response) => void = () => {};
  vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveOld = resolve; })).mockResolvedValue(Response.json({ ...context, repository: { state: 'available', name: 'other-project' } })));
  const view = render(<ThreadInformation {...props} />);
  view.rerender(<ThreadInformation {...props} session={{ id: 'two', title: 'Other task' }} />);
  expect(await screen.findByText('other-project')).toBeTruthy();
  resolveOld(Response.json(context));
  await waitFor(() => expect(screen.queryByText('sample-project')).toBeNull());
  expect(screen.getByText('other-project')).toBeTruthy();
});

test('failed title save keeps the editor and error available for retry', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(context)));
  render(<ThreadInformation {...props} onRename={async () => { throw new Error('Offline'); }} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rename thread' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Thread title' }), { target: { value: 'Retry title' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }));
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect((screen.getByRole('textbox', { name: 'Thread title' }) as HTMLInputElement).value).toBe('Retry title');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Rename thread' }));
});
