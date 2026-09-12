# Development and verification

The repository currently contains no Application Code or runnable product. Its guidance and delivery scripts are **Tooling**, using Bash 3.2+, `git`, and authenticated `gh` on macOS. Tooling is verified by successful real use; it has no coverage suite. Introducing Application Code requires the approved implementation language and stack-specific linting, type checking, validation, and meaningful high-coverage tests before delivery.

For a local change, run:

```bash
scripts/check.sh
git diff --check
```

`scripts/check.sh` checks Bash syntax, whitespace in the index and worktree, executable script modes, and the root `CLAUDE.md -> AGENTS.md` bridge. Review changed Markdown links manually. Exercise a changed delivery helper through its authorized real operation; static checks do not prove a GitHub delivery or main promotion.

## Dependency upkeep and repository verification

There are no dependency manifests or scheduled version-update configuration. During normal discovery, invoke `dependabot-upkeep` to read actionable alerts. Enable the dependency graph and Dependabot alerts while disabling automated Dependabot security-update and version-update pull requests. Verify graph/alert coverage, update configuration, and auto-triage PR-generation rules separately; administration access is needed for some settings. Record actual hosted evidence and access limits in the owning issue rather than inferring settings from an empty alert list.

Reusable repository verification is deferred until Fesnyng has runnable product behavior. Reassess then with `repository-verification`; do not add a standalone verification framework in advance.
