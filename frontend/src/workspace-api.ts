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
/** Control-plane selection fields for one native thread creation attempt. */
export type NativeThreadCreation = {
  project_id?: string;
  checkout_branch?: string;
  creation_id: string;
};
export type WorkspaceInspection = {
  workspace_id?: string;
  generation?: number;
  safety_digest?: string;
  state: 'ready' | 'removed' | 'legacy' | 'unavailable' | 'removing' | 'replacing';
  kind: 'repository' | 'ordinary' | 'fork' | 'legacy' | 'unavailable';
  directory: string | null;
  repository: { url?: string; checkout_branch?: string; starting_revision?: string; working_branch?: string; state: string };
  git: { kind?: 'repository' | 'ordinary' | null; state: 'safe' | 'unsafe' | 'unavailable'; digest?: string; branch?: string | null; dirty?: number; untracked?: number; ignored?: number; ahead?: number; entries?: number; upstream?: string | null };
  history: { state: 'verified' | 'unavailable'; captured_at?: number };
  cleanup: { remove: { available: boolean; reason?: string }; discard: { available: boolean; reason?: string }; replace: { available: boolean; reason?: string } };
};
export function workspaceInspection(value: unknown): WorkspaceInspection {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Workspace inspection is unavailable.');
  const row = value as Record<string, unknown>;
  const states = ['ready', 'removed', 'legacy', 'unavailable', 'removing', 'replacing'];
  const kinds = ['repository', 'ordinary', 'fork', 'legacy', 'unavailable'];
  const record = (item: unknown) => item && typeof item === 'object' && !Array.isArray(item) ? item as Record<string, unknown> : undefined;
  const cleanup = record(row.cleanup);
  const repository = record(row.repository);
  const git = record(row.git);
  const history = record(row.history);
  const remove = cleanup && record(cleanup.remove); const discard = cleanup && record(cleanup.discard); const replace = cleanup && record(cleanup.replace);
  const fingerprint = typeof row.workspace_id === 'string' && row.workspace_id.length > 0 && Number.isSafeInteger(row.generation) && (row.generation as number) >= 0 && typeof row.safety_digest === 'string' && /^[0-9a-f]{64}$/.test(row.safety_digest);
  const allCleanupUnavailable = remove?.available === false && discard?.available === false && replace?.available === false;
  const noMutationEvidence = allCleanupUnavailable
    && (row.workspace_id === undefined || row.workspace_id === null || (typeof row.workspace_id === 'string' && row.workspace_id.length > 0))
    && (row.generation === undefined || row.generation === null || (Number.isSafeInteger(row.generation) && (row.generation as number) >= 0))
    && (row.safety_digest === undefined || row.safety_digest === null);
  const count = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0;
  const optionalCount = (value: unknown) => value === undefined || count(value);
  const optionalText = (value: unknown) => value === undefined || typeof value === 'string' || value === null;
  const gitKnown = git && git.state !== 'unavailable';
  if ((!fingerprint && !noMutationEvidence) || !states.includes(row.state as string) || !kinds.includes(row.kind as string) || !(typeof row.directory === 'string' || row.directory === null) || !cleanup || !repository || !git || !history || typeof remove?.available !== 'boolean' || typeof discard?.available !== 'boolean' || typeof replace?.available !== 'boolean' || typeof repository.state !== 'string' || !['safe', 'unsafe', 'unavailable'].includes(git.state as string) || (gitKnown && !['repository', 'ordinary'].includes(git.kind as string)) || (!gitKnown && git.kind !== undefined && git.kind !== null && !['repository', 'ordinary'].includes(git.kind as string)) || (gitKnown && ![git.dirty, git.untracked, git.ignored].every(count)) || ![git.dirty, git.untracked, git.ignored, git.ahead, git.entries].every(optionalCount) || !optionalText(git.branch) || !optionalText(git.upstream) || !['verified', 'unavailable'].includes(history.state as string)) throw new Error('Workspace inspection is unavailable.');
  return row as WorkspaceInspection;
}
export class WorkspaceCreationUncertain extends Error {
  constructor(message: string, readonly creationId: string) { super(message); }
}
/** A host-confirmed pre-native rejection may safely start a different intent. */
export class WorkspacePreparationFailed extends Error {}
export function workspaceCreationFailure(detail: unknown, fallback: string): Error {
  if (typeof detail === 'string') return new Error(detail);
  if (!detail || typeof detail !== 'object') return new Error(fallback);
  const value = detail as Record<string, unknown>;
  if (value.code === 'workspace_creation_uncertain' && typeof value.creation_id === 'string' && typeof value.detail === 'string') return new WorkspaceCreationUncertain(value.detail, value.creation_id);
  if (value.code === 'workspace_preparation_failed' && typeof value.detail === 'string') return new WorkspacePreparationFailed(value.detail);
  return new Error(typeof value.detail === 'string' ? value.detail : fallback);
}
export type Member = { user_id: string; login: string; display_name: string; role: string };
export type Host = { id: string; name: string };
export type Profile = { id: string; name: string; provider: string };
export type Rule = { permission: string; pattern: string; action: 'allow' | 'ask' | 'deny' };
export type Skill = { name: string; content: string; explicit_only: boolean };
export type Configuration = { execution_type: 'docker'; runtime_type?: 'opencode' | 'codex'; provider: 'openai'; model: string; reasoning_effort?: string | null; profile_id: string | null; workspace: string; instructions: string; skills: Skill[] };
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
