import { createContext, useContext, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { useAuiState } from '@assistant-ui/react';
import type { OpenThreadFiles } from './thread-files';

type FileContext = { baseUrl: string; session?: { id: string; title: string }; disabled: boolean; openFiles: OpenThreadFiles };
const WorkspaceFilesContext = createContext<FileContext | undefined>(undefined);

/** Bind markdown file links to the native thread currently rendered by assistant-ui. */
export function WorkspaceFileLinks({ baseUrl, disabled, openFiles, children }: {
  baseUrl: string; disabled: boolean; openFiles: OpenThreadFiles; children: ReactNode;
}) {
  const item = useAuiState((state) => state.threadListItem);
  const id = item.externalId ?? item.remoteId;
  return <WorkspaceFilesContext.Provider value={{ baseUrl, disabled, openFiles, session: id ? { id, title: item.title ?? 'New thread' } : undefined }}>{children}</WorkspaceFilesContext.Provider>;
}

/** Undefined denotes a normal URL; null denotes an unsafe workspace path. */
export function workspaceFilePath(href: string): string | null | undefined {
  if (!href || href.startsWith('#') || href.startsWith('?') || /^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith('//')) return undefined;
  let path: string;
  try { path = decodeURIComponent(href.split(/[?#]/, 1)[0]!); }
  catch { return null; }
  if ((path.includes('\\') || [...path].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) || path.startsWith('//') || path.split('/').includes('..')) return null;
  path = path.replace(/^\//, '').split('/').filter((piece) => piece && piece !== '.').join('/');
  return path && path.length <= 240 ? path : null;
}

export function WorkspaceFileLink({ href, onClick, ...props }: ComponentPropsWithoutRef<'a'>) {
  const context = useContext(WorkspaceFilesContext);
  const path = href === undefined ? undefined : workspaceFilePath(href);
  if (!context || path === undefined) return <a {...props} href={href} onClick={onClick} />;
  if (path === null || context.disabled || !context.session) return <a {...props} aria-disabled="true" title={path === null ? 'This path is outside the workspace.' : 'Workspace files are unavailable.'} />;
  const session = context.session;
  const download = `${context.baseUrl.replace(/\/$/, '')}/file/download?${new URLSearchParams({ sessionID: session.id, path })}`;
  return <a {...props} href={download} onClick={(event) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    context.openFiles(session, event.currentTarget, path);
  }} />;
}
