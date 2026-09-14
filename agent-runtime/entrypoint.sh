#!/bin/sh
set -eu

if [ "${FESNYNG_RUNTIME_TYPE:-opencode}" = "codex" ]; then
  umask 077
  mkdir -p /home/agent/.codex
  printf %s "$FESNYNG_RUNTIME_TOKEN" > /home/agent/.codex/app-server-token
  cat > /home/agent/.codex/config.toml <<EOF
[mcp_servers.fesnyng]
url = "${FESNYNG_MCP_URL}"
bearer_token_env_var = "FESNYNG_AGENT_TOKEN"
EOF
  exec /opt/fesnyng/node_modules/.bin/codex app-server \
    --listen ws://0.0.0.0:4096 \
    --ws-auth capability-token \
    --ws-token-file /home/agent/.codex/app-server-token
fi

exec /opt/fesnyng/node_modules/.bin/opencode serve --hostname 0.0.0.0 --port 4096
