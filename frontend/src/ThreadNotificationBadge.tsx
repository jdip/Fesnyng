import { useThreadNotifications } from './ThreadNotifications';

export function ThreadNotificationBadge({ agent, session }: { agent?: string; session?: string }) {
  const notifications = useThreadNotifications();
  const target = agent ?? notifications?.selectedAgent;
  if (!notifications || !target) return null;
  const status = notifications.notificationFor(target, session);
  return <span className="thread-notifications">
    {status.unread && <span className="unread-pip" role="img" aria-label="Unread result" title="Unread completed result" />}
    {status.attention && <span className="attention-pip" role="img" aria-label="Needs attention" title="Unresolved question, approval, or failure">!</span>}
  </span>;
}
