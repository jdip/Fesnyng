# Fesnyng

Fesnyng is a public, self-hosted control plane for multi-user organizations of persistent AI agents. Preserve its organization boundary: Fesnyng manages durable agent organizations while external systems continue to own their work tracking and execution.

- For GitHub issues, planning records, claims, and remediation follow [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
- For product terms and architecture decisions, start with [docs/agents/domain.md](docs/agents/domain.md).
- For the current environment, code classification, and local checks, use [docs/development.md](docs/development.md).

## Visibility and publication

This is a public GitHub repository under the [MIT License](LICENSE). Before any GitHub write or public upload, follow the [public-work policy](SECURITY.md#public-work-policy). Preserve the intentional `Sterling-Automation` attribution, the `259149591+Sterling-Automation@users.noreply.github.com` commit identity, and legitimate upstream attribution. Visibility and licensing changes require explicit owner authorization.

## Languages and environments

There is no Application Code, product runtime, package manager, or deployment environment yet. Current delivery and maintenance scripts are Tooling, approved to use Bash 3.2+ with the existing macOS `git` and authenticated `gh` CLIs. Adding any application language, runtime, build/package toolchain, execution environment, or additional helper environment requires explicit user authorization.

## Checkouts and delivery

Keep the primary checkout on `test`, refreshed from `origin/test` by fast-forward only when clean. Reserve `test` for that checkout. Make changes on `codex/` feature branches in separate worktrees based on fresh `origin/test`; preserve attached worktrees and unrelated local state.

Use the global `pr-to-test` skill with [docs/workflows/pr-to-test.md](docs/workflows/pr-to-test.md) and `scripts/pr-to-test.sh` for every change through review, merge, and verified delivery to `test`. Do not commit or push directly to `test` or bypass protection.

Promotion requires a separate explicit user request. Use the global `promote-to-main` skill with [docs/workflows/promote-to-main.md](docs/workflows/promote-to-main.md) and `scripts/promote-to-main.sh` from the clean primary `test` checkout through verification and main-to-test synchronization. If a canonical script is missing, search for an existing owner, file a scoped issue, and report the delivery blocker.

## Repository Standard

- Source: https://github.com/jdip/agent-team
- Revision: f1a46a4bef77e2a00ab1d2098cbf3ecda430e3c0
