import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { createFesnyngOpenCodeFetch } from './lib/opencode-client';

type Unavailable = { state: 'unavailable' | 'not_applicable'; reason?: string };
export const ThreadWorkspaceContext = createContext<{ baseUrl: string; csrfToken: string; refreshKey: string | number } | undefined>(undefined);
export const useThreadWorkspace = () => useContext(ThreadWorkspaceContext);
export type ThreadContext = {
  repository: { state: 'available'; name: string } | { state: 'absent' | 'unavailable' };
  branch: { state: 'available'; name: string | null } | Unavailable;
  changes: { state: 'available'; added: number; deleted: number; untracked: number; binaryFiles: number } | Unavailable;
  subagents: { state: 'available'; count: number } | Unavailable;
  backgroundProcesses: { state: 'available'; count: number } | Unavailable;
};

export function repositoryLabel(repository?: ThreadContext['repository']) {
  return repository?.state === 'available' ? repository.name : repository?.state === 'absent' ? 'No repository' : 'Repository unavailable';
}

function parseContext(value: unknown): ThreadContext {
  const record = value as Partial<ThreadContext> | null;
  if (!record || !['repository', 'branch', 'changes', 'subagents', 'backgroundProcesses'].every((key) => {
    const field = record[key as keyof ThreadContext];
    return field && typeof field === 'object' && ['available', 'absent', 'unavailable', 'not_applicable'].includes(field.state);
  })) throw new Error('Thread context unavailable');
  const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
  if (record.repository?.state === 'available' && typeof record.repository.name !== 'string') throw new Error('Thread context unavailable');
  if (record.branch?.state === 'available' && record.branch.name !== null && typeof record.branch.name !== 'string') throw new Error('Thread context unavailable');
  if (record.changes?.state === 'available' && ![record.changes.added, record.changes.deleted, record.changes.untracked, record.changes.binaryFiles].every(count)) throw new Error('Thread context unavailable');
  for (const field of [record.subagents, record.backgroundProcesses]) if (field?.state === 'available' && !count(field.count)) throw new Error('Thread context unavailable');
  return record as ThreadContext;
}

/** One scoped source for both the information block and repository subtitles. */
export function useThreadContext(baseUrl: string | undefined, csrfToken: string, session: string, refreshKey: string | number) {
  const request = useMemo(() => createFesnyngOpenCodeFetch(csrfToken), [csrfToken]);
  const endpoint = baseUrl && `${baseUrl.replace(/\/$/, '')}/session/${encodeURIComponent(session)}/context`;
  const [loaded, setLoaded] = useState<{ endpoint: string; data?: ThreadContext; failed: boolean }>({ endpoint: endpoint ?? '', failed: false });
  useEffect(() => {
    if (!endpoint) return;
    const controller = new AbortController();
    void request(endpoint, { signal: controller.signal }).then((response) => response.json()).then(parseContext)
      .then((data) => { if (!controller.signal.aborted) setLoaded({ endpoint, data, failed: false }); })
      .catch(() => { if (!controller.signal.aborted) setLoaded({ endpoint, failed: true }); });
    return () => controller.abort();
  }, [endpoint, request, refreshKey]);
  return endpoint && loaded.endpoint === endpoint ? loaded : { endpoint: endpoint ?? '', failed: false };
}
