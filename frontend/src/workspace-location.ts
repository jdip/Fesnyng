const identifier = /^[A-Za-z0-9_-]{1,160}$/;
export function readWorkspaceLocation() {
  const values = new URLSearchParams(window.location.hash.slice(1));
  const value = (key: string) => { const candidate = values.get(key); return candidate && identifier.test(candidate) ? candidate : undefined; };
  return { organization: value('organization'), agent: value('agent'), thread: value('thread') };
}
export function saveWorkspaceLocation(organization: string, agent?: string, thread?: string) {
  const values = new URLSearchParams({ organization });
  if (agent) values.set('agent', agent);
  if (thread) values.set('thread', thread);
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${values}`);
}
