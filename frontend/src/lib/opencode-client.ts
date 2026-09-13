import { createOpencodeClient, type OpencodeClient } from '@assistant-ui/react-opencode';

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const WRITE_METHODS = new Set(['DELETE', 'PATCH', 'POST', 'PUT']);

async function nativeFailure(response: Response) {
  const body: unknown = await response.clone().json().catch(() => undefined);
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  return new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`);
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
      return response;
    });
  };
}

export function createFesnyngOpenCodeClient(baseUrl: string, csrfToken: string): OpencodeClient {
  return createOpencodeClient({
    baseUrl,
    fetch: createFesnyngOpenCodeFetch(csrfToken),
  });
}
