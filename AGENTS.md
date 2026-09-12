# Fesnyng

Fesnyng is a public, self-hosted control plane for multi-user organizations of persistent AI agents. Preserve its organization boundary: Fesnyng manages durable agent organizations while external systems continue to own their work tracking and execution.

- For GitHub issues, planning records, claims, and remediation follow [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
- For product planning, read [PLAN.md](PLAN.md), the initial source for the [primary map](https://github.com/jdip/Fesnyng/issues/8), and its [approved implementation specification](https://github.com/jdip/Fesnyng/issues/10).
- For product terms and architecture decisions, start with [docs/agents/domain.md](docs/agents/domain.md).
- For the current environment, code classification, and local checks, use [docs/development.md](docs/development.md).

## Visibility and publication

This is a public GitHub repository under the [MIT License](LICENSE). Before any GitHub write or public upload, follow the [public-work policy](SECURITY.md#public-work-policy). Preserve the intentional `Sterling-Automation` attribution, the `259149591+Sterling-Automation@users.noreply.github.com` commit identity, and legitimate upstream attribution. Visibility and licensing changes require explicit owner authorization.

## Languages and environments

Application Code uses a separate React/TypeScript/npm/Vite frontend and Python/FastAPI control-plane and agent-host services with uv, Ruff, ty and SQLite. Docker provides one container per agent; the initial proof runs two independent host APIs locally on macOS. OpenCode owns execution; assistant-ui owns conversation presentation. The narrow native JS/TS OpenCode auth plugin and the specification’s frontend patch/test tools are approved. Delivery and maintenance scripts are Bash Tooling. Additional languages, toolchains or execution environments require explicit user authorization.

## Checkouts and delivery

Keep the primary checkout on `test`, refreshed from `origin/test` by fast-forward only when clean. Reserve `test` for that checkout. Make changes on `codex/` feature branches in separate worktrees based on fresh `origin/test`; preserve attached worktrees and unrelated local state.

Use the global `pr-to-test` skill with [docs/workflows/pr-to-test.md](docs/workflows/pr-to-test.md) and `scripts/pr-to-test.sh` for every change through review, merge, and verified delivery to `test`. Do not commit or push directly to `test` or bypass protection.

Promotion requires a separate explicit user request. Use the global `promote-to-main` skill with [docs/workflows/promote-to-main.md](docs/workflows/promote-to-main.md) and `scripts/promote-to-main.sh` from the clean primary `test` checkout through verification and main-to-test synchronization. If a canonical script is missing, search for an existing owner, file a scoped issue, and report the delivery blocker.

## Repository Standard

- Source: https://github.com/jdip/agent-team
- Revision: f1a46a4bef77e2a00ab1d2098cbf3ecda430e3c0
