#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
uv_command=${FESNYNG_UV:-uv}
command -v "$uv_command" >/dev/null || { echo 'Install uv or set FESNYNG_UV to its executable path; see docs/development.md.' >&2; exit 1; }
"$uv_command" sync --locked --project backend
npm ci --prefix frontend
npm ci --prefix agent-runtime
