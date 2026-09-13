import { useAui } from '@assistant-ui/react';
import { createContext, useCallback, useContext, useEffect, useEffectEvent, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { createFesnyngOpenCodeFetch } from './lib/opencode-client';
import { errorMessage } from './workspace-api';

type Pins = {
  ids: ReadonlySet<string> | undefined;
  saving: boolean;
  error: string;
  refresh: () => Promise<void>;
  setPinned: (session: string, pinned: boolean) => Promise<void>;
};
const ThreadPinsContext = createContext<Pins | undefined>(undefined);
export const useThreadPins = () => useContext(ThreadPinsContext);

function pinIds(value: unknown): Set<string> {
  if (!value || typeof value !== 'object' || !('session_ids' in value)
    || !Array.isArray(value.session_ids) || !value.session_ids.every((id) => typeof id === 'string')) {
    throw new Error('Could not read pinned threads.');
  }
  return new Set(value.session_ids as string[]);
}

/** Personal navigation metadata alongside the maintained thread inventory. */
export function ThreadPinsProvider({ baseUrl, csrfToken, refreshKey, onError, children }: PropsWithChildren<{
  baseUrl: string;
  csrfToken: string;
  refreshKey: number;
  onError?: (error: unknown) => void | Promise<void>;
}>) {
  const aui = useAui();
  const url = new URL('../thread-pins', `${baseUrl.replace(/\/$/, '')}/`).href;
  const request = useMemo(() => createFesnyngOpenCodeFetch(csrfToken), [csrfToken]);
  const [ids, setIds] = useState<Set<string>>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const revision = useRef(0);
  const pending = useRef(false);
  const reportError = useEffectEvent((cause: unknown) => onError?.(cause));
  const load = useCallback(async (signal?: AbortSignal) => {
    if (pending.current) return;
    const version = ++revision.current;
    await request(url, { signal }).then((response) => response.json()).then((value: unknown) => {
      const next = pinIds(value);
      if (version !== revision.current || signal?.aborted) return;
      setIds(next);
      setError('');
    }).catch((cause: unknown) => {
      if (version === revision.current && !signal?.aborted) setError(errorMessage(cause));
    });
  }, [request, url]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => { controller.abort(); };
  }, [load, refreshKey]);
  const refresh = useCallback(async () => {
    await Promise.all([load(), aui.threads.reload()]);
  }, [aui, load]);
  useEffect(() => {
    let refreshing = false;
    let disposed = false;
    const update = () => {
      if (refreshing || document.visibilityState === 'hidden') return;
      refreshing = true;
      void refresh().catch((cause: unknown) => {
        if (!disposed) reportError(cause);
      }).finally(() => { refreshing = false; });
    };
    const timer = window.setInterval(update, 10000);
    window.addEventListener('focus', update);
    window.addEventListener('online', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', update);
      window.removeEventListener('online', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, [refresh]);
  const setPinned = async (session: string, pinned: boolean) => {
    if (pending.current || !ids) return;
    pending.current = true;
    setSaving(true);
    const version = ++revision.current;
    try {
      const next = pinIds(await (await request(`${url}/${encodeURIComponent(session)}`, { method: pinned ? 'PUT' : 'DELETE' })).json());
      if (version !== revision.current) return;
      setIds(next);
      setError('');
      await aui.threads.reload();
    } catch (cause) {
      if (version === revision.current) setError(errorMessage(cause));
    } finally {
      pending.current = false;
      if (version === revision.current) setSaving(false);
    }
  };
  return <ThreadPinsContext.Provider value={{ ids, saving, error, refresh, setPinned }}>{children}</ThreadPinsContext.Provider>;
}
