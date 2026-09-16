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
  expect(screen.getByLabelText('Repository and branch').textContent).toContain('sample-project · feature');
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
  await waitFor(() => expect(screen.getByLabelText('Repository and branch').textContent).toContain('Repository unavailable'));
  expect(screen.queryByText('No repository')).toBeNull();
  expect(screen.queryByText('+0 / −0')).toBeNull();
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

test('keeps workspace details collapsed while exposing a compact title, repository subtitle, and live Files action', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/context')) return Response.json(context);
    if (input.endsWith('/workspace')) return Response.json({
      workspace_id: 'workspace-one', generation: 1, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'repository', directory: '/workspaces/one',
      repository: { state: 'available', url: 'https://example.test/sample-project.git', working_branch: 'feature' },
      git: { state: 'safe', kind: 'repository', branch: 'feature', dirty: 0, untracked: 0, ignored: 0, ahead: 0 },
      history: { state: 'verified' }, cleanup: { remove: { available: false }, discard: { available: false }, replace: { available: false } },
    });
    throw new Error(`Unexpected ${input}`);
  }));
  const files = vi.fn();
  render(<ThreadInformation {...props} workspace={{ organization: 'org', agent: 'agent', csrf: 'csrf-example' }} onOpenFiles={files} />);

  expect(await screen.findByText('sample-project')).toBeTruthy();
  expect(screen.getByLabelText('Repository and branch').textContent).toContain('sample-project · feature');
  expect(screen.getByRole('button', { name: 'Files' })).toBeTruthy();
  expect(screen.getByText('Workspace details').closest('details')?.open).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Files' }));
  expect(files).toHaveBeenCalledWith(expect.any(HTMLButtonElement));
  fireEvent.click(screen.getByText('Workspace details'));
  expect(await screen.findByText('/workspaces/one')).toBeTruthy();
});

test('does not leave the compact subtitle loading after workspace inspection fails', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Host offline' }), { status: 503 })));
  render(<ThreadInformation {...props} workspace={{ organization: 'org', agent: 'agent', csrf: 'csrf-example' }} />);

  await waitFor(() => expect(screen.getByLabelText('Repository and branch').textContent).toContain('Repository unavailable · Unavailable'));
});

test('retains workspace details while omitting thread mutations for frozen or unavailable execution', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(context)));
  render(<ThreadInformation {...props} interactionDisabled onOpenFiles={vi.fn()} />);

  expect(await screen.findByText('Workspace details')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Rename thread' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Files' })).toBeNull();
});
