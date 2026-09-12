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
