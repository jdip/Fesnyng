# Issue tracker: GitHub

GitHub Issues hold maps, specifications, executable work, and remediation findings. Before creating, editing, commenting on, assigning, labeling, or uploading material to an issue or pull request, apply the [public-work policy](../../SECURITY.md#public-work-policy).

Run `gh` in this clone so its remote selects `jdip/Fesnyng`. Read work with `gh issue view <number> --comments`; inspect issue state and labels with `gh issue view <number> --json state,labels,assignees`. For a focused search, use `gh issue list --state all --search '<component or symptom>' --limit 100`, vary the terms, and inspect every likely match before deciding it is distinct.

## Planning and implementation

Use native GitHub issues and sub-issues. A Wayfinder map records settled direction and links its decision children. An approved implementation specification is an open `implementation:backlog` parent whose native sub-issues are the executable work set. Create an issue with `gh issue create`; obtain its database ID with `gh api repos/jdip/Fesnyng/issues/<number> --jq .id`; then attach a child with `gh api --method POST repos/jdip/Fesnyng/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`. Read it back with `gh api repos/jdip/Fesnyng/issues/<child>/parent` and `gh api --paginate repos/jdip/Fesnyng/issues/<parent>/sub_issues`. Keep the map, specification, and their links visible in issue bodies or comments.

Before implementation, use the shared `set-map` and `set-backlog` selectors for scoped state; do not hand-edit selector state. `next-issue` verifies the covering map is resolved and the specification is approved. Recheck the child is open, unblocked, and unassigned, then claim it with `gh issue edit <number> --add-assignee @me` and leave a concise claim comment. Do not take another session's claim. Add a native blocker with `gh api --method POST repos/jdip/Fesnyng/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`; read it back with `gh api repos/jdip/Fesnyng/issues/<child> --jq .issue_dependencies_summary`. Use `Blocked by: #<number>` only when the native dependency feature itself is unavailable. An authorization failure is a delivery blocker: preserve the evidence and report it instead of substituting hand-edited state. Close a child only after it has delivered its stated outcome and its delivery evidence is recorded; close its parent only after every child is complete.

## Remediation findings

For an actionable weakness or blocker, search open and closed issues with component names, symptoms, and distinctive failures. Read likely matches and compare the affected behavior, not just titles. Reuse an open match and add a comment only when there is material new evidence. For a closed match, inspect its resolution and current behavior; create a linked recurrence only when the problem demonstrably returned.

When no match exists, create one scoped issue that states observed evidence, impact, a safe reproduction or source location, workaround, verification outcome, and whether it blocks the current work. Link it to the originating issue or pull request. Read the created issue back and report its URL. If it blocks a tracked task, add and verify the native dependency; otherwise continue the current work. If GitHub access fails, preserve sanitized evidence and proposed issue text in the task response.

Use `semver:major`, `semver:minor`, `semver:patch`, or `semver:none` on each task pull request. Pick the label from the delivered behavior; raise uncertainty for review rather than guessing.
