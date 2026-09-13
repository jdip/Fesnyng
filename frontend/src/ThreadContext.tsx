export function ThreadContext({ organization, agent, session, csrf }: { organization: string; agent: string; session: string; csrf: string }) {
  void organization;
  void agent;
  void session;
  void csrf;
  return <div className="thread-context" />;
}
