export type ProjectGroupingWarning = { state: 'ungrouped'; requested_project_id: string; retry_path: string; detail: string };

export function publishProjectGroupingWarning(value: unknown) {
  if (!value || typeof value !== 'object') return;
  const warning = (value as Record<string, unknown>).fesnyng_project_grouping;
  if (!warning || typeof warning !== 'object') return;
  const record = warning as Record<string, unknown>;
  if (record.state !== 'ungrouped' || typeof record.requested_project_id !== 'string' || typeof record.retry_path !== 'string' || typeof record.detail !== 'string') return;
  window.dispatchEvent(new CustomEvent<ProjectGroupingWarning>('fesnyng-project-grouping-warning', { detail: record as ProjectGroupingWarning }));
}
