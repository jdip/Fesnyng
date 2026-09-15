export type User = { id: string; login: string; display_name: string };
export type LoginSession = { user: User; csrf_token: string };
export type Organization = { id: string; name: string; icon?: { kind: 'emoji' | 'image'; value: string } | null };
export type Project = {
  id: string;
  organization_id: string;
  name: string;
  description: string | null;
  target_repository_url: string | null;
  default_checkout_branch: string | null;
  archived: boolean;
};
export type Member = { user_id: string; login: string; display_name: string; role: string };
export type Host = { id: string; name: string };
export type Profile = { id: string; name: string; provider: string };
export type Rule = { permission: string; pattern: string; action: 'allow' | 'ask' | 'deny' };
export type Skill = { name: string; content: string; explicit_only: boolean };
export type Configuration = { execution_type: 'docker'; runtime_type?: 'opencode' | 'codex'; provider: 'openai'; model: string; profile_id: string | null; workspace: string; instructions: string; skills: Skill[] };
export type Agent = { id: string; organization_id: string; name: string; title: string; host_id: string; reports_to_agent_id: string | null; department_id?: string | null; desired_version: number; applied_version: number | null; configuration_status: string; configuration: Configuration };
export type Memory = { key: string; content: string; revision: number; author: { name: string }; updated_at: number };
export type Policy = { desired_version: number; configuration: { default_permission: 'allow' | 'ask' | 'deny'; mandatory_permissions: Rule[]; allow_thread_overrides: boolean } };
export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
export async function api<T>(path: string, options: { method?: string; body?: unknown; csrf?: string; signal?: AbortSignal } = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: options.method ?? 'GET', credentials: 'same-origin', signal: options.signal,
    headers: { ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(options.csrf ? { 'X-CSRF-Token': options.csrf } : {}) },
    ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null;
    throw new ApiError(typeof detail === 'string' ? detail : `Request failed (${response.status}).`, response.status);
  }
  return response.status === 204 ? undefined as T : await response.json() as T;
}
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'The request could not be completed.'; }
export const agentPath = (organization: string, agent: string) => `/organizations/${encodeURIComponent(organization)}/agents/${encodeURIComponent(agent)}`;
