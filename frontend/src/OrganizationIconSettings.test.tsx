import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { OrganizationIconSettings } from './OrganizationIconSettings';

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function organization(id = 'org', icon: unknown = null) {
  return { id, name: id === 'new' ? 'New organization' : 'Organization', icon };
}

function renderSettings(id = 'org', onSaved = vi.fn()) {
  return { onSaved, ...render(<OrganizationIconSettings organization={id} csrf="csrf-example" onSaved={onSaved} />) };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('loads an emoji and saves the edited organization icon through the scoped endpoint', async () => {
  const request = vi.fn(async (_url: string, options?: RequestInit) => response(options?.method === 'PUT'
    ? organization('org', { kind: 'emoji', value: '🚀' })
    : organization('org', { kind: 'emoji', value: '🌿' })));
  vi.stubGlobal('fetch', request);
  const { onSaved } = renderSettings();
  const emoji = await screen.findByLabelText('Organization emoji');
  expect(emoji).toHaveProperty('value', '🌿');
  fireEvent.change(emoji, { target: { value: '🚀' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save icon' }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(organization('org', { kind: 'emoji', value: '🚀' })));
  expect(request).toHaveBeenCalledWith('/api/organizations/org/icon', expect.objectContaining({
    method: 'PUT', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-example' }), body: JSON.stringify({ icon: { kind: 'emoji', value: '🚀' } }),
  }));
});

test('reads an accepted image into a preview and saves its data URL', async () => {
  class Reader {
    result: string | ArrayBuffer | null = null;
    onload: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    onerror: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    readAsDataURL() {
      this.result = 'data:image/png;base64,cG5n';
      this.onload?.(new ProgressEvent('load') as ProgressEvent<FileReader>);
    }
  }
  vi.stubGlobal('FileReader', Reader);
  const request = vi.fn(async (_url: string, options?: RequestInit) => response(options?.method === 'PUT'
    ? organization('org', { kind: 'image', value: 'data:image/png;base64,cG5n' })
    : organization()));
  vi.stubGlobal('fetch', request);
  renderSettings();
  const file = new File(['png'], 'icon.png', { type: 'image/png' });
  fireEvent.change(await screen.findByLabelText('Organization image'), { target: { files: [file] } });
  expect(await screen.findByRole('img', { name: 'Organization icon preview' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Save icon' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/icon', expect.objectContaining({
    body: JSON.stringify({ icon: { kind: 'image', value: 'data:image/png;base64,cG5n' } }),
  })));
});

test('rejects unsupported and oversized client uploads before saving', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(organization())));
  renderSettings();
  const input = await screen.findByLabelText('Organization image');
  fireEvent.change(input, { target: { files: [new File(['gif'], 'icon.gif', { type: 'image/gif' })] } });
  expect(screen.getByRole('alert').textContent).toContain('PNG, JPEG, or WebP');
  fireEvent.change(input, { target: { files: [new File([new Uint8Array(2 * 1024 * 1024 + 1)], 'icon.png', { type: 'image/png' })] } });
  expect(screen.getByRole('alert').textContent).toContain('no larger than 2 MiB');
});

test('resets an unsaved emoji and saves a cleared icon as null', async () => {
  const request = vi.fn(async (_url: string, options?: RequestInit) => response(options?.method === 'PUT' ? organization('org', null) : organization('org', { kind: 'emoji', value: '🌿' })));
  vi.stubGlobal('fetch', request);
  renderSettings();
  const emoji = await screen.findByLabelText('Organization emoji');
  fireEvent.change(emoji, { target: { value: '🚀' } });
  fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
  expect(emoji).toHaveProperty('value', '🌿');
  fireEvent.click(screen.getByRole('button', { name: 'Clear icon' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save icon' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/api/organizations/org/icon', expect.objectContaining({ body: JSON.stringify({ icon: null }) })));
});

test('keeps an unsaved icon visible when the server rejects the update', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options?: RequestInit) => response(options?.method === 'PUT' ? { detail: 'Icon rejected' } : organization(), options?.method === 'PUT' ? 422 : 200)));
  renderSettings();
  fireEvent.change(await screen.findByLabelText('Organization emoji'), { target: { value: '🚀' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save icon' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Icon rejected');
  expect(screen.getByLabelText('Organization emoji')).toHaveProperty('value', '🚀');
});

test('retries a failed identity load without requiring a settings-page reload', async () => {
  let attempts = 0;
  vi.stubGlobal('fetch', vi.fn(async () => {
    attempts += 1;
    return attempts === 1 ? response({ detail: 'Identity unavailable' }, 503) : response(organization('org', { kind: 'emoji', value: '🌿' }));
  }));
  renderSettings();
  expect((await screen.findByRole('alert')).textContent).toContain('Identity unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Retry identity' }));
  expect(await screen.findByDisplayValue('🌿')).toBeTruthy();
});

test('ignores an old organization response after the selected organization changes', async () => {
  let finishOld: (value: Response) => void = () => { throw new Error('Old request was not started'); };
  vi.stubGlobal('fetch', vi.fn((url: string) => url === '/api/organizations/old'
    ? new Promise<Response>((resolve) => { finishOld = resolve; })
    : Promise.resolve(response(organization('new', { kind: 'emoji', value: '🆕' })))));
  const onSaved = vi.fn();
  const rendered = renderSettings('old', onSaved);
  rendered.rerender(<OrganizationIconSettings organization="new" csrf="csrf-example" onSaved={onSaved} />);
  expect(await screen.findByDisplayValue('🆕')).toBeTruthy();
  finishOld(response(organization('old', { kind: 'emoji', value: '⌛' })));
  await waitFor(() => expect(screen.getByLabelText('Organization emoji')).toHaveProperty('value', '🆕'));
});

test('does not let a file read started for an old organization leak into the next one', async () => {
  let finishRead: (() => void) | undefined;
  class Reader {
    result: string | ArrayBuffer | null = null;
    onload: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    onerror: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    readAsDataURL() {
      finishRead = () => {
        this.result = 'data:image/png;base64,b2xk';
        this.onload?.(new ProgressEvent('load') as ProgressEvent<FileReader>);
      };
    }
  }
  vi.stubGlobal('FileReader', Reader);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => response(url.endsWith('/new') ? organization('new', { kind: 'emoji', value: '🆕' }) : organization('old'))));
  const rendered = renderSettings('old');
  fireEvent.change(await screen.findByLabelText('Organization image'), { target: { files: [new File(['png'], 'old.png', { type: 'image/png' })] } });
  rendered.rerender(<OrganizationIconSettings organization="new" csrf="csrf-example" onSaved={vi.fn()} />);
  expect(await screen.findByDisplayValue('🆕')).toBeTruthy();
  finishRead?.();
  await waitFor(() => expect(rendered.container.querySelector('img')).toBeNull());
});

test('keeps a newer emoji when an older file read finishes later', async () => {
  let finishRead: (() => void) | undefined;
  class Reader {
    result: string | ArrayBuffer | null = null;
    onload: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    onerror: ((event: ProgressEvent<FileReader>) => unknown) | null = null;
    readAsDataURL() {
      finishRead = () => {
        this.result = 'data:image/png;base64,b2xk';
        this.onload?.(new ProgressEvent('load') as ProgressEvent<FileReader>);
      };
    }
  }
  vi.stubGlobal('FileReader', Reader);
  vi.stubGlobal('fetch', vi.fn(async () => response(organization())));
  const rendered = renderSettings();
  fireEvent.change(await screen.findByLabelText('Organization image'), { target: { files: [new File(['png'], 'icon.png', { type: 'image/png' })] } });
  fireEvent.change(screen.getByLabelText('Organization emoji'), { target: { value: '🚀' } });
  finishRead?.();
  await waitFor(() => expect(screen.getByLabelText('Organization emoji')).toHaveProperty('value', '🚀'));
  expect(rendered.container.querySelector('img')).toBeNull();
});
