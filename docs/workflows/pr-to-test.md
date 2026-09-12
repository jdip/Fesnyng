# Deliver a task to test

Use the global `pr-to-test` skill with this runbook. The repository contains Python and frontend Application Code plus Bash Tooling. Completion is a reviewed merge-commit PR, applicable application checks and actual runtime/browser evidence, verification of the committed tree, and a clean primary `test` checkout at the fetched `origin/test` revision. The foundation requires both independently running service health endpoints and the frontend connection page; later product tickets add their own approved runtime acceptance. Hosted checks, when configured, also apply.

## Prepare and review

Read [root guidance](../../AGENTS.md), [development checks](../development.md), the approved issue/specification and resolved map, and the [public-work policy](../../SECURITY.md#public-work-policy). Recheck the task's selection and claim before writes. Review all outgoing content, relevant history, filenames, branch names, messages, authorship, and evidence before the first push or PR. The policy applies to tracker writes too.

Keep the primary checkout on `test`; refresh it only when clean with `git fetch origin` and `git merge --ff-only origin/test`. Create a separate worktree on a `codex/` feature branch from fresh `origin/test`. Make and commit only the intended changes there using the repository-local approved identity from `SECURITY.md`.

Run `scripts/check.sh`. Review changed Markdown links manually and inspect the index with `git diff --cached`; confirm the bridge with `readlink CLAUDE.md` and `git ls-files --stage CLAUDE.md` (mode `120000`, sibling target `AGENTS.md`). Delivery scripts must have executable Git mode `100755`. Review all changes through `code-review` against immutable base/head SHAs. Exercise changed Tooling through its real authorized use; no Tooling coverage suite is required. Every later source change needs review of the affected diff before merging.

Choose one task label: `semver:none` for documentation and development tooling without a product release effect; otherwise `semver:patch`, `semver:minor`, or `semver:major` from actual behavior. Resolve conflicting/missing labels from the change, or report ambiguity. Labels are advisory bookkeeping, not substitutes for checks. Create missing labels through ordinary `gh label create` operations after public metadata review.

## Submit and merge

Write the reviewed PR description to a file outside the checkout. Include the concrete change, its issue/specification links, checks, review outcome, and unresolved limitations. Avoid closing-keyword references when the issue has unmet requirements.

From the clean feature worktree:

```bash
scripts/pr-to-test.sh submit 'PR title' /path/to/reviewed-body.md semver:none
scripts/pr-to-test.sh merge PR_NUMBER
```

The submit command checks the repository, feature worktree, approved account and local identity, local gates, and fresh `test` ancestry before pushing and creating or reusing a PR. Inspect a reused PR's description and labels before merging. The merge command checks that the PR is open, targets `test`, and has exactly the current reviewed feature commit, waits for any hosted checks, and requests a merge commit without an administrative bypass.

When there are no hosted checks, the script reports that fact; the local checks and completed review remain mandatory. A queued or pending merge is incomplete. Observe its actual state rather than repeatedly requesting a merge.

After merge, the script fetches and verifies the merge's ancestry, two parents, approved author account/no-reply email, and GitHub committer metadata. It checks the exact committed revision in a temporary detached worktree, removes that worktree only on successful verification, and fast-forwards the clean primary `test` checkout, running its checks as well. GitHub may render the approved account's public display name instead of its login.

## Recovery and evidence

On failure, inspect the earliest error, PR state, branch revisions, and any retained worktree. Preserve partial effects and unrelated work. A source correction returns through the reviewed feature PR workflow. A transient post-merge verification or primary-refresh failure can continue without a second merge:

```bash
scripts/pr-to-test.sh verify PR_NUMBER
```

This command verifies the existing merge and refreshes the primary checkout; it does not create a new PR. An identity mismatch is a public metadata incident to investigate, not permission to rewrite history. A failed script reports incomplete delivery and may leave a detached verification checkout for diagnosis.

Record the PR URL, merge SHA, review and checks, verified primary revision, and limitations in the originating issue. Hosted dependency settings are verified separately through `dependabot-upkeep`; inaccessible settings remain explicitly incomplete and tracked. Close the issue and parent only when their actual acceptance outcomes are met. Promotion to `main` is not part of this workflow.

Remove only task-owned, merged, clean resources proven safe and no longer attached to a live task. Retain shared or uncertain worktrees and report them. No blanket pruning, forced checkout updates, or direct feature pushes to `test`.
