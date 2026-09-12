# Promote test to main

Run only after a separate explicit operator promotion request, using the global `promote-to-main` skill. Start in the clean primary `test` checkout and read [root guidance](../../AGENTS.md), [development checks](../development.md), and the [public-work policy](../../SECURITY.md#public-work-policy). Review the complete outgoing range, associated task PRs, labels, source/history, metadata and evidence before publication. Preserve visibility, licensing, and the approved delivery identity.

## Scope and versions

Promotion delivers the reviewed `test` revision to `main` by merge commit, verifies it, and synchronizes `main` back to `test` by another reviewed merge-commit PR. The current repository has no application, release artifact, semantic version tag, or deployment. Its documentation/tooling adoption uses `semver:none`; all-none promotion creates no version tag. Main and synchronization PRs themselves use `semver:none`.

When product releases exist, count each applicable merged task PR once in deterministic first-parent merge order since the previous release. Use existing tags and Git/GitHub evidence, not a ledger. Apply each task label in order with normal resets: `1.4.2 + patch + minor + patch = 1.5.1`; synchronization contributes none. Resolve routine label gaps from actual changes; report uncertainty without guessing a version or moving tags. Establish an initial application version and any real artifact pipeline prerequisites with that application's approved development scope. This documentation-only tooling does not manufacture a release version or tag. Real application/deployment gates must be added when that behavior exists; version bookkeeping cannot waive them.

## Promote and synchronize

Review the immutable `origin/main...origin/test` diff through `code-review`, including the task PR evidence. Run `scripts/check.sh`; inspect Markdown links and public metadata. For Tooling, retain the actual use evidence from task delivery and exercise changed operations within the promotion request's authority.

Write a reviewed promotion description outside the checkout, identifying the selected commits, task PRs, version disposition, review, and validation. Then:

```bash
scripts/promote-to-main.sh submit 'Promotion title' /path/to/reviewed-body.md
scripts/promote-to-main.sh merge PROMOTION_PR_NUMBER
```

`submit` refreshes the clean primary `test` checkout by fast-forward only, runs local checks, and creates or reuses a `test`-to-`main` PR. If `test` changed since review, inspect and review the changed range before merge. `merge` requires the PR head to equal the current local and remote `test` commit, waits for hosted checks when present, requests a merge commit without bypass, and verifies the exact committed main result in an isolated detached checkout. It checks the approved author account/no-reply email and GitHub committer metadata, then creates or reuses a `main`-to-`test` synchronization PR.

Inspect the synchronization PR against its immutable head and current `test`. Confirm it carries the intended promotion and any separately reviewed main changes, then:

```bash
scripts/promote-to-main.sh sync SYNC_PR_NUMBER REVIEWED_MAIN_SHA
scripts/promote-to-main.sh verify PROMOTION_PR_NUMBER SYNC_PR_NUMBER
```

`sync` requires the full main commit SHA from the completed synchronization review and rejects remote or PR head drift. It verifies that revision, performs the merge-commit synchronization, verifies the resulting commit, and refreshes/checks the clean primary `test` checkout. Final `verify` establishes that both PRs are merged to the intended branches and the synchronization includes the selected promotion. A clean verified primary checkout, both merge SHAs, and the actual version/deployment disposition are the completion evidence.

## Recovery and cleanup

Stop at the earliest failure and inspect actual effects. A queued merge or a dirty/diverged primary checkout is incomplete; preserve it. Read merged PR state before any retry, so a completed merge is not submitted again. If main is already promoted but synchronization is missing, create/reuse the reviewed `main`-to-`test` PR through `gh pr create --base test --head main --label semver:none --body-file /path/to/reviewed-body.md`, then continue with the canonical `sync` and `verify` commands. This is recovery of the same separately authorized promotion, not a direct push or bypass.

Record the PRs, exact revisions, review, checks, version result, hosted-setting activation if relevant, and any runtime/deployment evidence in the task. Failed detached verification worktrees are retained for investigation. Remove only clean, task-owned resources proven safe and no longer attached; preserve uncertain state. The initial adoption checks syntax/help only for this promotion path and does not claim a real main promotion.
