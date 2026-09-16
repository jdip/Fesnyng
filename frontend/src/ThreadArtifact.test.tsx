import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ThreadArtifactPanel } from './ThreadArtifact';

const baseUrl = 'http://workspace.test/api/organizations/org/agents/agent/opencode';
const session = { id: 'thread', title: 'Release readiness' };
const entry = (name: string, path: string, type: 'directory' | 'file', size = 0, modifiedAt = 1000) => ({ name, path, type, size, modifiedAt });
const listing = (path: string, entries: ReturnType<typeof entry>[]) => ({ rootSessionID: 'root', sessionID: 'thread', path, entries });
const content = (path: string, overrides: Partial<Record<string, unknown>> = {}) => ({ rootSessionID: 'root', sessionID: 'thread', path, type: 'text', encoding: 'utf-8', content: 'Verified release evidence', size: 25, truncated: false, contentType: 'text/plain; charset=utf-8', ...overrides });

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
}

function renderPanel(onClose = vi.fn(), focusRequest = 0) {
  return { onClose, ...render(<ThreadArtifactPanel baseUrl={baseUrl} csrfToken="csrf-example" session={session} focusRequest={focusRequest} onClose={onClose} />) };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('opens a linked nested file directly and reports a missing linked file', async () => {
  const request = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    const path = url.searchParams.get('path') ?? '';
    if (url.pathname.endsWith('/file/content')) return path === 'docs/missing.md'
      ? response({ detail: 'File not found.' }, 404) : response(content(path));
    return response(listing(path, [entry('report.md', 'docs/report.md', 'file')]));
  });
  vi.stubGlobal('fetch', request);
  const view = render(<ThreadArtifactPanel baseUrl={baseUrl} csrfToken="csrf-example" session={session} focusRequest={1} initialPath="docs/report.md" onClose={vi.fn()} />);
  expect(await screen.findByText('Verified release evidence')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Download' }).getAttribute('href')).toContain('path=docs%2Freport.md');
  expect(request).toHaveBeenCalledWith(`${baseUrl}/file?sessionID=thread&path=docs`, expect.anything());
  view.unmount();
  render(<ThreadArtifactPanel baseUrl={baseUrl} csrfToken="csrf-example" session={session} focusRequest={2} initialPath="docs/missing.md" onClose={vi.fn()} />);
  expect(await screen.findByText('File not found.')).toBeTruthy();
});

test('navigates folders through the authorized thread facade and previews selected text', async () => {
  const request = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    const path = url.searchParams.get('path') ?? '';
    if (url.pathname.endsWith('/file/content')) return response(content(path));
    if (path === '') return response(listing('', [entry('src', 'src', 'directory'), entry('README.md', 'README.md', 'file', 24, 2000)]));
    return response(listing('src', [entry('app.ts', 'src/app.ts', 'file', 48, 3000)]));
  });
  vi.stubGlobal('fetch', request);
  const rendered = renderPanel();
  fireEvent.click(await screen.findByRole('button', { name: 'src' }));
  expect(await screen.findByRole('button', { name: 'app.ts' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'src' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'app.ts' }));
  expect(await screen.findByText('Verified release evidence')).toBeTruthy();
  screen.getByLabelText('Filter files').focus();
  rendered.rerender(<ThreadArtifactPanel baseUrl={baseUrl} csrfToken="csrf-example" session={session} focusRequest={1} onClose={vi.fn()} />);
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Files' }));
  expect(screen.getByRole('button', { name: 'app.ts' })).toBeTruthy();
  expect(screen.getByText('Verified release evidence')).toBeTruthy();
  expect(request).toHaveBeenCalledWith(`${baseUrl}/file?sessionID=thread&path=src`, expect.anything());
  expect(request).toHaveBeenCalledWith(`${baseUrl}/file/content?sessionID=thread&path=src%2Fapp.ts`, expect.anything());
});

test('filters and orders files while retaining folders before file entries', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(listing('', [entry('zeta.txt', 'zeta.txt', 'file', 10, 1), entry('assets', 'assets', 'directory', 0, 2), entry('alpha.txt', 'alpha.txt', 'file', 100, 3)]))));
  renderPanel();
  await screen.findByRole('button', { name: 'assets' });
  expect(screen.getAllByRole('button').map((button) => button.getAttribute('aria-label')).filter((name): name is string => ['assets', 'alpha.txt', 'zeta.txt'].includes(name ?? ''))).toEqual(['assets', 'alpha.txt', 'zeta.txt']);
  fireEvent.change(screen.getByLabelText('Filter files'), { target: { value: 'alpha' } });
  expect(screen.getByRole('button', { name: 'alpha.txt' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'zeta.txt' })).toBeNull();
  fireEvent.change(screen.getByLabelText('Filter files'), { target: { value: '' } });
  fireEvent.change(screen.getByLabelText('Order'), { target: { value: 'size' } });
  expect(screen.getAllByRole('button').map((button) => button.getAttribute('aria-label')).filter((name): name is string => ['assets', 'alpha.txt', 'zeta.txt'].includes(name ?? ''))).toEqual(['assets', 'zeta.txt', 'alpha.txt']);
});

test('uses only verified raster content types for image preview and leaves a streaming download link', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    return url.pathname.endsWith('/file/content')
      ? response(content('image.png', { type: 'binary', encoding: 'base64', content: 'cG5n', size: 3, contentType: 'image/png' }))
      : response(listing('', [entry('image.png', 'image.png', 'file', 3)]));
  }));
  renderPanel();
  fireEvent.click(await screen.findByRole('button', { name: /image.png/ }));
  expect(await screen.findByRole('img', { name: 'Preview of image.png' })).toBeTruthy();
  const download = screen.getByRole('link', { name: 'Download' });
  expect(download).toHaveProperty('href', `${baseUrl}/file/download?sessionID=thread&path=image.png`);
  expect(download.getAttribute('download')).toBe('image.png');
  fireEvent.error(screen.getByRole('img', { name: 'Preview of image.png' }));
  expect(await screen.findByText(/Image preview is unavailable/)).toBeTruthy();
});

test('does not execute unsupported image-like content and explains truncated previews', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    return url.pathname.endsWith('/file/content')
      ? response(content('unsafe.svg', { type: 'binary', encoding: 'base64', content: 'PHN2Zy8+', size: 8, truncated: true, contentType: 'image/svg+xml' }))
      : response(listing('', [entry('unsafe.svg', 'unsafe.svg', 'file', 300_000)]));
  }));
  renderPanel();
  fireEvent.click(await screen.findByRole('button', { name: /unsafe.svg/ }));
  expect(await screen.findByText(/larger than the preview limit/)).toBeTruthy();
  expect(screen.queryByRole('img')).toBeNull();
  expect(screen.getByRole('link', { name: 'Download' })).toBeTruthy();
});

test('supports keyboard resize, heading focus, and close', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(listing('', []))));
  const { onClose } = renderPanel();
  const heading = await screen.findByRole('heading', { name: 'Files' });
  expect(document.activeElement).toBe(heading);
  const separator = screen.getByRole('separator', { name: 'Resize files panel' });
  expect(separator.getAttribute('aria-valuenow')).toBe('400');
  fireEvent.keyDown(separator, { key: 'ArrowLeft' });
  expect(separator.getAttribute('aria-valuenow')).toBe('424');
  fireEvent.keyDown(separator, { key: 'End' });
  expect(separator.getAttribute('aria-valuenow')).toBe(separator.getAttribute('aria-valuemax'));
  fireEvent.keyDown(separator, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledOnce();
});

test('refreshes both selected metadata and its capped preview', async () => {
  let listCalls = 0;
  let contentCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith('/file/content')) {
      contentCalls += 1;
      return response(content('report.txt', { content: contentCalls === 1 ? 'First preview' : 'Refreshed preview' }));
    }
    listCalls += 1;
    return response(listing('', [entry('report.txt', 'report.txt', 'file', listCalls === 1 ? 10 : 20)]));
  }));
  renderPanel();
  fireEvent.click(await screen.findByRole('button', { name: /report.txt/ }));
  expect(await screen.findByText('First preview')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh files' }));
  expect(await screen.findByText('Refreshed preview')).toBeTruthy();
  expect(screen.getByLabelText('File preview').querySelector('.thread-artifact-meta')?.textContent).toContain('20 B');
  expect(contentCalls).toBe(2);
});

test('ignores an older session listing after the panel session changes', async () => {
  let finishOld: (value: Response) => void = () => { throw new Error('Old request was not started'); };
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => new URL(String(input)).searchParams.get('sessionID') === 'thread'
    ? new Promise<Response>((resolve) => { finishOld = resolve; })
    : Promise.resolve(response({ rootSessionID: 'root', sessionID: 'new-thread', path: '', entries: [entry('new.txt', 'new.txt', 'file', 1)] }))));
  const rendered = renderPanel();
  rendered.rerender(<ThreadArtifactPanel baseUrl={baseUrl} csrfToken="csrf-example" session={{ id: 'new-thread', title: 'New thread' }} focusRequest={0} onClose={vi.fn()} />);
  expect(await screen.findByRole('button', { name: /new.txt/ })).toBeTruthy();
  finishOld(response(listing('', [entry('old.txt', 'old.txt', 'file', 1)])));
  await waitFor(() => expect(screen.queryByRole('button', { name: /old.txt/ })).toBeNull());
});
