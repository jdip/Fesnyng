#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

for script in scripts/*.sh; do
  bash -n "$script"
  [[ -x "$script" ]] || { echo "Not executable: $script" >&2; exit 1; }
done
git diff --check
git diff --cached --check
[[ -f AGENTS.md && ! -L AGENTS.md && -L CLAUDE.md ]]
[[ $(readlink CLAUDE.md) == AGENTS.md ]]
echo 'Shell syntax, executable files, whitespace, and Claude bridge verified.'

scripts/setup.sh
uv_command=${FESNYNG_UV:-uv}
"$uv_command" run --locked --project backend ruff check backend
"$uv_command" run --locked --project backend ruff format --check backend
"$uv_command" run --locked --project backend ty check --project backend
(cd backend && "$uv_command" run --locked pytest)
npm run check --prefix frontend
npm run check --prefix agent-runtime
