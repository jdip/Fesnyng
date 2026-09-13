import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronRightIcon, DownloadIcon, FileIcon, FolderIcon, RefreshCwIcon, XIcon } from 'lucide-react';
import { createFesnyngOpenCodeFetch } from './lib/opencode-client';
import './ThreadArtifact.css';

type ArtifactEntry = { name: string; path: string; type: 'directory' | 'file'; size: number; modifiedAt: number };
type ArtifactList = { rootSessionID: string; sessionID: string; path: string; entries: ArtifactEntry[] };
type ArtifactContent = { rootSessionID: string; sessionID: string; path: string; type: 'text' | 'binary'; content: string; encoding: 'utf-8' | 'base64'; size: number; truncated: boolean; contentType: string };
type LoadState<T> = { scope: string; loading: boolean; value?: T; error: string };
const PREVIEW_BYTES = 131_072;
const MIN_WIDTH = 280;
const MAX_WIDTH = 720;

function endpoint(baseUrl: string, resource: 'file' | 'file/content' | 'file/download', session: string, path: string) {
  const url = new URL(`${baseUrl.replace(/\/$/, '')}/${resource}`);
  url.search = new URLSearchParams({ sessionID: session, path }).toString();
  return url.toString();
}

function isArtifactEntry(value: unknown): value is ArtifactEntry {
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  return typeof record.name === 'string' && typeof record.path === 'string'
    && (record.type === 'directory' || record.type === 'file')
    && typeof record.size === 'number' && typeof record.modifiedAt === 'number';
}

function artifactList(value: unknown): ArtifactList {
  if (!value || typeof value !== 'object') throw new Error('Could not read workspace files.');
  const record = value as Record<string, unknown>;
  if (typeof record.rootSessionID !== 'string' || typeof record.sessionID !== 'string' || typeof record.path !== 'string' || !Array.isArray(record.entries)) throw new Error('Could not read workspace files.');
  if (!record.entries.every(isArtifactEntry)) throw new Error('Could not read workspace files.');
  return { rootSessionID: record.rootSessionID, sessionID: record.sessionID, path: record.path, entries: record.entries };
}

function artifactContent(value: unknown): ArtifactContent {
  if (!value || typeof value !== 'object') throw new Error('Could not read this file.');
  const record = value as Record<string, unknown>;
  if (typeof record.rootSessionID !== 'string' || typeof record.sessionID !== 'string' || typeof record.path !== 'string'
    || (record.type !== 'text' && record.type !== 'binary') || typeof record.content !== 'string'
    || (record.encoding !== 'utf-8' && record.encoding !== 'base64') || typeof record.size !== 'number' || typeof record.truncated !== 'boolean' || typeof record.contentType !== 'string') throw new Error('Could not read this file.');
  return record as ArtifactContent;
}

function displaySize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

function imageType(contentType: string) {
  return ['image/png', 'image/jpeg', 'image/webp'].includes(contentType.toLowerCase()) ? contentType.toLowerCase() : undefined;
}

function crumbPaths(path: string) {
  const pieces = path ? path.split('/') : [];
  return pieces.map((piece, index) => ({ name: piece, path: pieces.slice(0, index + 1).join('/') }));
}

/** Read-only, session-scoped project file navigation. It never changes the active conversation runtime. */
export function ThreadArtifactPanel({ baseUrl, csrfToken, session, focusRequest, onClose }: {
  baseUrl: string;
  csrfToken: string;
  session: { id: string; title: string };
  focusRequest: number;
  onClose: () => void;
}) {
  const request = useMemo(() => createFesnyngOpenCodeFetch(csrfToken), [csrfToken]);
  const [path, setPath] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState<'name' | 'modified' | 'size'>('name');
  const [selected, setSelected] = useState<ArtifactEntry>();
  const [width, setWidth] = useState(400);
  const [maximumWidth, setMaximumWidth] = useState(MAX_WIDTH);
  const [imageFailureScope, setImageFailureScope] = useState('');
  const listScope = `${baseUrl}:${session.id}:${path}:${refresh}`;
  const [listing, setListing] = useState<LoadState<ArtifactList>>({ scope: listScope, loading: true, error: '' });
  const contentScope = selected ? `${baseUrl}:${session.id}:${selected.path}:${refresh}` : '';
  const [content, setContent] = useState<LoadState<ArtifactContent>>({ scope: '', loading: false, error: '' });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const resizeObserver = useRef<ResizeObserver | undefined>(undefined);
  const resizeRef = useRef<{ pointer: number; width: number } | undefined>(undefined);
  const activeList = listing.scope === listScope ? listing : { scope: listScope, loading: true, error: '' };
  const activeContent = content.scope === contentScope ? content : { scope: contentScope, loading: Boolean(selected), error: '' };
  const selectedPath = selected?.path;

  const attachDrawer = useCallback((element: HTMLDivElement | null) => {
    resizeObserver.current?.disconnect();
    if (!element) return;
    const setAvailableWidth = () => {
      const containerWidth = element.parentElement?.getBoundingClientRect().width || window.innerWidth;
      const next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.floor(containerWidth - 320)));
      setMaximumWidth((previous) => previous === next ? previous : next);
    };
    setAvailableWidth();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(setAvailableWidth);
    observer.observe(element.parentElement ?? element);
    resizeObserver.current = observer;
  }, []);

  useEffect(() => { headingRef.current?.focus(); }, [focusRequest, session.id]);
  useEffect(() => () => resizeObserver.current?.disconnect(), []);
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    void request(endpoint(baseUrl, 'file', session.id, path), { signal: controller.signal }).then((response) => response.json()).then(artifactList).then((value) => {
      if (current && !controller.signal.aborted) {
        setListing({ scope: listScope, loading: false, value, error: '' });
        setSelected((previous) => previous ? value.entries.find((entry) => entry.path === previous.path && entry.type === 'file') : undefined);
      }
    }).catch((cause: unknown) => {
      if (current && !controller.signal.aborted) setListing({ scope: listScope, loading: false, error: cause instanceof Error ? cause.message : 'Could not read workspace files.' });
    });
    return () => { current = false; controller.abort(); };
  }, [baseUrl, listScope, path, request, session.id]);
  useEffect(() => {
    if (!selectedPath) return;
    const controller = new AbortController();
    let current = true;
    void request(endpoint(baseUrl, 'file/content', session.id, selectedPath), { signal: controller.signal }).then((response) => response.json()).then(artifactContent).then((value) => {
      if (current && !controller.signal.aborted) setContent({ scope: contentScope, loading: false, value, error: '' });
    }).catch((cause: unknown) => {
      if (current && !controller.signal.aborted) setContent({ scope: contentScope, loading: false, error: cause instanceof Error ? cause.message : 'Could not read this file.' });
    });
    return () => { current = false; controller.abort(); };
  }, [baseUrl, contentScope, request, selectedPath, session.id]);

  const entries = [...(activeList.value?.entries ?? [])].filter((entry) => entry.name.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase())).sort((left, right) => {
    if (left.type !== right.type) return left.type === 'directory' ? -1 : 1;
    return sort === 'name' ? left.name.localeCompare(right.name, undefined, { sensitivity: 'base' })
      : sort === 'size' ? left.size - right.size || left.name.localeCompare(right.name)
        : right.modifiedAt - left.modifiedAt || left.name.localeCompare(right.name);
  });
  const preview = activeContent.value;
  const downloadUrl = selected ? endpoint(baseUrl, 'file/download', session.id, selected.path) : undefined;
  const previewImage = preview && preview.type === 'binary' && preview.encoding === 'base64' && !preview.truncated && preview.size <= PREVIEW_BYTES ? imageType(preview.contentType) : undefined;
  const imageFailed = imageFailureScope === contentScope;
  const panelWidth = Math.min(width, maximumWidth);

  const openDirectory = (entry: ArtifactEntry) => {
    setPath(entry.path);
    setSelected(undefined);
    setFilter('');
  };
  const selectFile = (entry: ArtifactEntry) => {
    setImageFailureScope('');
    setSelected(entry);
  };
  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    resizeRef.current = { pointer: event.clientX, width: panelWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const resize = (event: React.PointerEvent<HTMLDivElement>) => {
    const start = resizeRef.current;
    if (start) setWidth(Math.min(maximumWidth, Math.max(MIN_WIDTH, start.width - (event.clientX - start.pointer))));
  };
  const finishResize = () => { resizeRef.current = undefined; };
  const resizeWithKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const next = event.key === 'ArrowLeft' ? panelWidth + 24 : event.key === 'ArrowRight' ? panelWidth - 24 : event.key === 'Home' ? MIN_WIDTH : event.key === 'End' ? maximumWidth : undefined;
    if (next === undefined) return;
    event.preventDefault();
    setWidth(Math.min(maximumWidth, Math.max(MIN_WIDTH, next)));
  };

  const handleEscape = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    onClose();
  };

  return <div ref={attachDrawer} className="thread-artifact-drawer" style={{ width: panelWidth }} onKeyDown={handleEscape}>
    <div className="thread-artifact-resize" role="separator" tabIndex={0} aria-label="Resize files panel" aria-orientation="vertical" aria-valuemin={MIN_WIDTH} aria-valuemax={maximumWidth} aria-valuenow={Math.round(panelWidth)} onPointerDown={startResize} onPointerMove={resize} onPointerUp={finishResize} onPointerCancel={finishResize} onKeyDown={resizeWithKey} />
    <section className="thread-artifact-panel" aria-label="Thread files">
      <header><div><h2 ref={headingRef} tabIndex={-1}>Files</h2><p title={session.title}>{session.title}</p></div><button type="button" className="thread-artifact-icon-button" aria-label="Close files" onClick={onClose}><XIcon aria-hidden /></button></header>
      <div className="thread-artifact-controls"><label>Filter files<input type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter files" /></label><label>Order<select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)}><option value="name">Name</option><option value="modified">Modified</option><option value="size">Size</option></select></label><button type="button" className="thread-artifact-icon-button" aria-label="Refresh files" onClick={() => { setListing((previous) => previous.scope === listScope ? { ...previous, loading: true, error: '' } : previous); setRefresh((value) => value + 1); }}><RefreshCwIcon aria-hidden /></button></div>
      <nav className="thread-artifact-breadcrumbs" aria-label="File path"><button type="button" onClick={() => { setPath(''); setSelected(undefined); }}>Workspace</button>{crumbPaths(path).map((crumb) => <span key={crumb.path}><ChevronRightIcon aria-hidden /><button type="button" onClick={() => { setPath(crumb.path); setSelected(undefined); }}>{crumb.name}</button></span>)}</nav>
      {activeList.loading && <p role="status" className="thread-artifact-status">Loading files…</p>}
      {activeList.error && <p role="alert" className="thread-artifact-error">{activeList.error}</p>}
      {!activeList.loading && !activeList.error && <ul className="thread-artifact-list">{entries.map((entry) => <li key={entry.path}><button type="button" aria-label={entry.name} aria-pressed={selected?.path === entry.path} onClick={() => entry.type === 'directory' ? openDirectory(entry) : selectFile(entry)}>{entry.type === 'directory' ? <FolderIcon aria-hidden /> : <FileIcon aria-hidden />}<span>{entry.name}<small>{entry.type === 'directory' ? 'Folder' : `${displaySize(entry.size)} · ${new Date(entry.modifiedAt).toLocaleString()}`}</small></span></button></li>)}{entries.length === 0 && <li className="thread-artifact-empty">No matching files.</li>}</ul>}
      <section className="thread-artifact-preview" aria-label="File preview">{selected && <><header><strong>{selected.path}</strong>{downloadUrl && <a className="thread-artifact-download" href={downloadUrl} download={selected.name}><DownloadIcon aria-hidden />Download</a>}</header><p className="thread-artifact-meta">{displaySize(selected.size)} · {new Date(selected.modifiedAt).toLocaleString()}</p>{activeContent.loading && <p role="status">Loading preview…</p>}{activeContent.error && <p role="alert" className="thread-artifact-error">{activeContent.error}</p>}{preview && preview.truncated && <p>This file is larger than the preview limit. Download it to inspect its complete contents.</p>}{preview && !preview.truncated && previewImage && !imageFailed && <img className="thread-artifact-image" src={`data:${previewImage};base64,${preview.content}`} alt={`Preview of ${selected.name}`} onError={() => setImageFailureScope(contentScope)} />}{preview && !preview.truncated && previewImage && imageFailed && <p>Image preview is unavailable. Download it to inspect the exact file.</p>}{preview && !preview.truncated && preview.type === 'text' && preview.encoding === 'utf-8' && <pre>{preview.content}</pre>}{preview && !preview.truncated && !previewImage && (preview.type !== 'text' || preview.encoding !== 'utf-8') && <p>This binary file does not have a safe preview. Download it to inspect its exact contents.</p>}</>}</section>
    </section>
  </div>;
}
