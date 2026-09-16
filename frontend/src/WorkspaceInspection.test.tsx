import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkspaceInspection } from './WorkspaceInspection';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const inspection = {
  workspace_id: 'workspace-one', generation: 4, safety_digest: 'a'.repeat(64), state: 'ready', kind: 'repository', directory: '/workspaces/org/employee/workspace-one',
  repository: { url: 'https://example.test/website.git', checkout_branch: 'test', starting_revision: 'abc123', working_branch: 'work/one', state: 'available' },
  git: { kind: 'repository', state: 'safe', branch: 'work/one', dirty: 0, untracked: 0, ignored: 0, ahead: 0, upstream: 'origin/test' },
  history: { state: 'verified', captured_at: 123 },
  cleanup: { remove: { available: true }, discard: { available: true }, replace: { available: false, reason: 'Remove this workspace first.' } },
};

test('shows host-owned workspace evidence and removes only with the returned safety fingerprint', async () => {
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/workspace') && (!init.method || init.method === 'GET')) return Response.json(inspection);
    if (input.endsWith('/workspace/remove') && init.method === 'POST') return Response.json({ ...inspection, state: 'removed', cleanup: { remove: { available: false, reason: 'Already removed.' }, discard: { available: false, reason: 'Already removed.' }, replace: { available: true } } });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  const changed = vi.fn();
  const operated = vi.fn();
  render(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" onChanged={changed} onOperation={operated} />);

  expect(await screen.findByText('/workspaces/org/employee/workspace-one')).toBeTruthy();
  expect(screen.getByText('work/one')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Remove workspace' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/agents/employee/sessions/thread/workspace/remove', expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }), body: JSON.stringify({ workspace_id: 'workspace-one', generation: 4, safety_digest: 'a'.repeat(64) }) })));
  expect(await screen.findByText(/^Removed · Repository worktree$/)).toBeTruthy();
  expect(changed).toHaveBeenLastCalledWith('removed');
  expect(operated).toHaveBeenLastCalledWith(expect.objectContaining({ state: 'removed' }));
});

test('requires an explicit loss acknowledgement before discarding a dirty workspace', async () => {
  const dirty = { ...inspection, git: { ...inspection.git, state: 'unsafe', dirty: 1, untracked: 2 }, cleanup: { remove: { available: false, reason: 'Uncommitted files must be retained.' }, discard: { available: true }, replace: { available: false, reason: 'Remove this workspace first.' } } };
  const request = vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/workspace') && (!init.method || init.method === 'GET')) return Response.json(dirty);
    if (input.endsWith('/workspace/discard') && init.method === 'POST') return Response.json({ ...dirty, state: 'removed' });
    throw new Error(`Unexpected ${input}`);
  });
  vi.stubGlobal('fetch', request);
  render(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Discard workspace' }));
  expect(screen.getByText('This permanently deletes the files in /workspaces/org/employee/workspace-one.')).toBeTruthy();
  expect(screen.getByText((_, element) => element?.textContent === 'Known loss: 1 modified file, 2 untracked files, 0 ignored files.')).toBeTruthy();
  const discard = screen.getByRole('button', { name: 'Discard workspace permanently' }) as HTMLButtonElement;
  expect(discard.disabled).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', { name: 'I understand this deletes the workspace files and cannot be undone.' }));
  fireEvent.click(discard);
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/agents/employee/sessions/thread/workspace/discard', expect.objectContaining({ method: 'POST', body: JSON.stringify({ workspace_id: 'workspace-one', generation: 4, safety_digest: 'a'.repeat(64) }) })));
});

test('summarizes host-reported ordinary directory entries before discard', async () => {
  const ordinary = { ...inspection, kind: 'ordinary', repository: { state: 'absent' }, git: { kind: 'ordinary', state: 'unsafe', dirty: 0, untracked: 0, ignored: 0, ahead: 0, entries: 3, branch: null, upstream: null }, cleanup: { remove: { available: false, reason: 'The directory contains retained files or entries.' }, discard: { available: true }, replace: { available: false, reason: 'Remove this workspace first.' } } };
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(ordinary)));
  render(<WorkspaceInspection organization="org" agent="employee" session="ordinary-thread" csrf="csrf-example" />);

  expect(await screen.findByText('3 directory entries')).toBeTruthy();
  fireEvent.click(await screen.findByRole('button', { name: 'Discard workspace' }));
  expect(screen.getByText((_, element) => element?.textContent === 'Known loss: 3 entries in this directory.')).toBeTruthy();
});

test('accepts an unsafe repository receipt whose missing upstream leaves ahead unknown', async () => {
  const noUpstream = { ...inspection, git: { ...inspection.git, state: 'unsafe', ahead: undefined, upstream: null }, cleanup: { remove: { available: false, reason: 'The branch has no verified upstream.' }, discard: { available: true }, replace: { available: false, reason: 'Remove this workspace first.' } } };
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(noUpstream)));
  render(<WorkspaceInspection organization="org" agent="employee" session="unknown-ahead" csrf="csrf-example" />);

  expect(await screen.findByText('0 modified · 0 untracked · 0 ignored · Unknown ahead')).toBeTruthy();
});

test('requires a fresh discard acknowledgement after refreshed evidence changes', async () => {
  let generation = 4;
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...inspection, generation, git: { ...inspection.git, state: 'unsafe', dirty: 1 }, cleanup: { remove: { available: false, reason: 'Changes remain.' }, discard: { available: true }, replace: { available: false, reason: 'Remove first.' } } })));
  const rendered = render(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" revision={0} />);

  fireEvent.click(await screen.findByRole('button', { name: 'Discard workspace' }));
  fireEvent.click(screen.getByRole('checkbox', { name: 'I understand this deletes the workspace files and cannot be undone.' }));
  expect((screen.getByRole('button', { name: 'Discard workspace permanently' }) as HTMLButtonElement).disabled).toBe(false);
  generation = 5;
  rendered.rerender(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" revision={1} />);
  expect(await screen.findByRole('button', { name: 'Discard workspace' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Discard workspace permanently' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Discard workspace' }));
  expect((screen.getByRole('button', { name: 'Discard workspace permanently' }) as HTMLButtonElement).disabled).toBe(true);
});

test('shows legacy location and limitations without inventing cleanup evidence', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    state: 'legacy', kind: 'legacy', directory: '/legacy/container-only', repository: { state: 'unavailable' },
    git: { state: 'unavailable' },
    history: { state: 'unavailable' }, cleanup: { remove: { available: false, reason: 'Host ownership was not recorded.' }, discard: { available: false, reason: 'Host ownership was not recorded.' }, replace: { available: false, reason: 'Host ownership was not recorded.' } },
  })));
  render(<WorkspaceInspection organization="org" agent="employee" session="legacy-thread" csrf="csrf-example" />);

  expect(await screen.findByText('/legacy/container-only')).toBeTruthy();
  expect(screen.getByText('This legacy workspace keeps its established execution path. It cannot be removed or migrated automatically.')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Remove workspace' }) as HTMLButtonElement).disabled).toBe(true);
});

test('shows an owned unavailable workspace without fabricating its missing safety digest', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    workspace_id: 'workspace-one', generation: 7, safety_digest: null, state: 'unavailable', kind: 'ordinary', directory: '/workspaces/org/employee/temporary', repository: { state: 'absent' }, git: { state: 'unavailable' }, history: { state: 'verified' }, cleanup: { remove: { available: false, reason: 'Workspace safety could not be verified.' }, discard: { available: false, reason: 'Workspace safety could not be verified.' }, replace: { available: false, reason: 'Workspace safety could not be verified.' } },
  })));
  render(<WorkspaceInspection organization="org" agent="employee" session="unavailable-thread" csrf="csrf-example" />);

  expect(await screen.findByText('/workspaces/org/employee/temporary')).toBeTruthy();
  expect(screen.getByText('The host could not verify this workspace. Inspect again before relying on cleanup evidence.')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Discard workspace' }) as HTMLButtonElement).disabled).toBe(true);
});

test('drops old cleanup evidence and refreshes after a stale mutation receipt', async () => {
  let reads = 0;
  let settleRefresh: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    if (input.endsWith('/workspace') && (!init.method || init.method === 'GET')) {
      reads += 1;
      if (reads === 1) return Response.json(inspection);
      return new Promise<Response>((resolve) => { settleRefresh = resolve; });
    }
    if (input.endsWith('/workspace/remove') && init.method === 'POST') return Response.json({ detail: 'Workspace safety changed; inspect again before cleanup.' }, { status: 409 });
    throw new Error(`Unexpected ${input}`);
  }));
  render(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" />);

  fireEvent.click(await screen.findByRole('button', { name: 'Remove workspace' }));
  expect(await screen.findByText(/Workspace evidence is unavailable/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Remove workspace' })).toBeNull();
  settleRefresh?.(Response.json({ ...inspection, generation: 5 }));
  expect(await screen.findByRole('button', { name: 'Remove workspace' })).toBeTruthy();
});

test('does not report a workspace state after its inspector unmounts', async () => {
  let settle: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { settle = resolve; })));
  const changed = vi.fn();
  const rendered = render(<WorkspaceInspection organization="org" agent="employee" session="thread" csrf="csrf-example" onChanged={changed} />);

  rendered.unmount();
  settle?.(Response.json(inspection));
  await Promise.resolve();
  await Promise.resolve();
  expect(changed).not.toHaveBeenCalled();
});
