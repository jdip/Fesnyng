import { createOpencodeClient, type OpencodeClient } from '@assistant-ui/react-opencode';
import { projectGroupingWarning, publishProjectGroupingWarning } from '../project-grouping-warning';
import { WorkspaceCreationUncertain, WorkspacePreparationFailed, workspaceCreationFailure, type NativeThreadCreation } from '../workspace-api';

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const WRITE_METHODS = new Set(['DELETE', 'PATCH', 'POST', 'PUT']);

const isOpenCodeThreadList = (input: RequestInfo | URL, method: string) => {
  if (method !== 'GET') return false;
  const url = input instanceof Request ? input.url : String(input);
  try { return new URL(url, window.location.origin).pathname.endsWith('/experimental/session'); }
  catch { return false; }
};

const writableOpenCodeThreads = (value: unknown) => Array.isArray(value) ? value.filter((entry) => {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return true;
  const thread = entry as Record<string, unknown>;
  return thread.runtime_type !== 'codex' && thread.frozen !== true && typeof thread.frozen_at !== 'number';
}) : value;

async function activeOpenCodeThreadList(response: Response) {
  const value = await response.clone().json().catch(() => undefined);
  if (!Array.isArray(value)) return response;
  const headers = new Headers(response.headers);
  headers.delete('content-length');
  return new Response(JSON.stringify(writableOpenCodeThreads(value)), { status: response.status, statusText: response.statusText, headers });
}

async function nativeFailure(response: Response) {
  const body: unknown = await response.clone().json().catch(() => undefined);
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  return workspaceCreationFailure(detail, `Request failed (${response.status}).`);
}

/**
 * Browser transport for the Fesnyng-owned OpenCode facade.
 *
 * The facade accepts the browser session cookie and rejects mutations without
 * a control-plane CSRF token. A separate idempotency key for each SDK write
 * lets the host-owned dispatch service deduplicate retrying browser requests.
 */
export function createFesnyngOpenCodeFetch(csrfToken: string, fetchImpl: FetchLike = fetch): FetchLike {
  return (input, init = {}) => {
    const method = (init.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));

    if (WRITE_METHODS.has(method)) {
      headers.set('X-CSRF-Token', csrfToken);
      if (!headers.has('Idempotency-Key')) headers.set('Idempotency-Key', crypto.randomUUID());
    }

    return fetchImpl(input, { ...init, credentials: 'include', headers }).then(async (response) => {
      if (!response.ok) throw await nativeFailure(response);
      if (method === 'POST' && new URL(input instanceof Request ? input.url : String(input), window.location.origin).pathname.endsWith('/session')) {
        void response.clone().json().then(publishProjectGroupingWarning).catch(() => {});
      }
      return isOpenCodeThreadList(input, method) ? activeOpenCodeThreadList(response) : response;
    });
  };
}

export function createFesnyngOpenCodeClient(baseUrl: string, csrfToken: string, creation?: NativeThreadCreation): OpencodeClient {
  const transport = createFesnyngOpenCodeFetch(csrfToken);
  return createOpencodeClient({
    baseUrl,
    fetch: (input, init = {}) => {
      const method = (init.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
      const url = input instanceof Request ? input.url : String(input);
      const isNewThread = creation && method === 'POST' && new URL(url, window.location.origin).pathname.endsWith('/session');
      if (!isNewThread) return transport(input, init);
      const current = typeof init.body === 'string' && init.body ? JSON.parse(init.body) as Record<string, unknown> : {};
      return transport(input, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers }, body: JSON.stringify({ ...current, ...creation }) });
    },
  });
}

/** Prepare one OpenCode thread before the conversation runtime opens it. */
export async function createFesnyngOpenCodeThread(baseUrl: string, csrfToken: string, creation: NativeThreadCreation) {
  try {
    const response = await createFesnyngOpenCodeFetch(csrfToken)(`${baseUrl.replace(/\/$/, '')}/session`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(creation),
    });
    const receipt: unknown = await response.json();
    if (!receipt || typeof receipt !== 'object') throw new Error('OpenCode thread receipt is invalid.');
    const id = (receipt as Record<string, unknown>).id ?? (receipt as Record<string, unknown>).session_id;
    if (typeof id !== 'string') throw new Error('OpenCode thread receipt is invalid.');
    return { id, groupingWarning: projectGroupingWarning(receipt) };
  } catch (cause) {
    if (cause instanceof WorkspaceCreationUncertain || cause instanceof WorkspacePreparationFailed) throw cause;
    throw new WorkspaceCreationUncertain('Thread preparation may have started. Check this preparation again to recover its result.', creation.creation_id);
  }
}
