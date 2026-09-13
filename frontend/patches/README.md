# `@assistant-ui/react-opencode` pending-interaction patch

`@assistant-ui+react-opencode+0.2.23.patch` is a narrow local patch against
`@assistant-ui/react-opencode` 0.2.23. It updates the distributed
`OpenCodeThreadController` to reconcile the authoritative native OpenCode
permission and question snapshots after initial thread loading and reconnect.
It also makes the adapter's required title-generation stream a no-op: OpenCode
already generates titles, while its `session.summarize` endpoint compacts
history and can race a new session's first prompt.

The upstream controller source inspected for this patch is
[`packages/react-opencode/src/OpenCodeThreadController.ts` at assistant-ui commit `bd7e8fa9f79ffea10fab0741026d53cdba4cfa70`](https://github.com/assistant-ui/assistant-ui/blob/bd7e8fa9f79ffea10fab0741026d53cdba4cfa70/packages/react-opencode/src/OpenCodeThreadController.ts).
The companion UI source under `src/components/assistant-ui` is copied from the
[official assistant-ui registry](https://r.assistant-ui.com/) and retains its
MIT license in `src/components/assistant-ui/LICENSE`.

Remove this patch when an upgraded maintained adapter reconciles initial and
reconnect permission/question snapshots authoritatively: removals made by a
second client must disappear, each kind must remain isolated, only the active
session may update, a failed endpoint must retain the last good snapshot, and a
delayed snapshot must merge newer per-request ask/reply/reject events without
dropping unrelated pending requests. Remove the title portion only when the
maintained adapter no longer calls `session.summarize` from `generateTitle`;
then retain the no-compaction first-message regression in
`src/Conversation.runtime.test.tsx`.
After upgrading, delete this patch and this note, then verify the behavior with
`src/lib/opencode-pending-sync.test.ts`.
