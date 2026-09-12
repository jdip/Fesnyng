# Development and verification

The repository currently contains no Application Code or runnable product. Its guidance and delivery scripts are **Tooling**, using Bash 3.2+, `git`, and authenticated `gh` on macOS. Tooling is verified by successful real use; it has no coverage suite. Introducing Application Code requires the approved implementation language and stack-specific linting, type checking, validation, and meaningful high-coverage tests before delivery.

For a local change, run:

```bash
scripts/check.sh
git diff --check
```

`scripts/check.sh` checks Bash syntax, whitespace in the index and worktree, executable script modes, and the root `CLAUDE.md -> AGENTS.md` bridge. Review changed Markdown links manually. Exercise a changed delivery helper through its authorized real operation; static checks do not prove a GitHub delivery or main promotion.

## Dependency upkeep and repository verification

There are no manifests or current dependency surfaces, and this repository has no version configuration. During normal discovery, invoke `dependabot-upkeep` to read actionable alerts. Where GitHub supports and permits it, enable the dependency graph and Dependabot alerts while disabling automated Dependabot security-update and version-update pull requests. Current settings access is blocked, so those settings are not verified.

Reusable repository verification is deferred until Fesnyng has runnable product behavior. Reassess then with `repository-verification`; do not add a standalone verification framework in advance.
