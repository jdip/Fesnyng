#!/usr/bin/env bash
set -Eeuo pipefail
usage() { echo "Usage: $0 --origin https://tailnet.example.ts.net --agent-host-credential-url http://DOCKER_GATEWAY:8001 [--deploy-base PATH] [--state-root PATH]" >&2; }
origin=
credential_url=
deploy_base=${XDG_DATA_HOME:-"$HOME/.local/share"}/fesnyng-test/deployment
state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}/fesnyng-test
while [[ $# -gt 0 ]]; do
  case $1 in
    --origin) origin=${2:-}; shift 2 ;;
    --agent-host-credential-url) credential_url=${2:-}; shift 2 ;;
    --deploy-base) deploy_base=${2:-}; shift 2 ;;
    --state-root) state_root=${2:-}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ $origin =~ ^https://[^/]+$ && $credential_url =~ ^http://[^/]+$ && $deploy_base = /* && $state_root = /* ]] || { usage; exit 2; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
node_version=$(awk '$1 == "nodejs" {print $2}' "$root/frontend/.tool-versions")
uv_version=$(awk '$1 == "uv" {print $2}' "$root/backend/.tool-versions")
[[ $node_version && $uv_version ]] || { echo 'Missing canonical Node or uv declaration.' >&2; exit 1; }
case $(uname -m) in x86_64) node_arch=x64;; aarch64) node_arch=arm64;; *) echo 'Unsupported Node architecture.' >&2; exit 1;; esac
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/fesnyng-test
[[ ! -e $config_dir/deployment.env && ! -e $deploy_base/current && ! -e $state_root/control-plane && ! -e $state_root/agent-host ]] || { echo 'Existing deployment resource retained.' >&2; exit 1; }
for unit in control host update; do [[ ! -e $HOME/.config/systemd/user/fesnyng-test-$unit.service ]] || { echo 'Existing deployment unit retained.' >&2; exit 1; }; done
[[ ! -e $HOME/.config/systemd/user/fesnyng-test-update.timer ]] || { echo 'Existing deployment timer retained.' >&2; exit 1; }
for command in git curl tar sha256sum docker systemctl loginctl python3 ss flock sudo; do command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }; done
docker info >/dev/null
systemctl --user show-environment >/dev/null
[[ $(loginctl show-user "$(id -u)" -p Linger --value) == yes ]] || { echo 'Enable user lingering before installation.' >&2; exit 1; }
for port in 8000 8001; do
  if ss -ltn "sport = :$port" | grep -q LISTEN; then
    echo "Port $port is already in use." >&2
    exit 1
  fi
done
serve_status=$(sudo -n tailscale serve status --json)
python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(0 if not value.get("Web") and not value.get("TCP") else 1)' <<<"$serve_status" || { echo 'Existing Tailscale Serve configuration is retained.' >&2; exit 1; }
mkdir -p "$deploy_base/tooling"
node_root=$deploy_base/tooling/node-v$node_version
if [[ ! -x $node_root/bin/node ]]; then
  archive=node-v$node_version-linux-$node_arch.tar.xz; tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
  curl -fsSL "https://nodejs.org/dist/v$node_version/$archive" -o "$tmp/$archive"
  checksum=$(curl -fsSL "https://nodejs.org/dist/v$node_version/SHASUMS256.txt" | awk -v file="$archive" '$2 == file {print $1}')
  [[ $checksum ]] || { echo 'Node checksum is unavailable.' >&2; exit 1; }
  printf '%s  %s\n' "$checksum" "$tmp/$archive" | sha256sum --check --status
  tar -C "$deploy_base/tooling" -xf "$tmp/$archive"; mv "$deploy_base/tooling/node-v$node_version-linux-$node_arch" "$node_root"
fi
curl -fsSL "https://astral.sh/uv/$uv_version/install.sh" | env UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$deploy_base/tooling/uv-bin" sh
uv_bin=$deploy_base/tooling/uv-bin/uv
[[ $($node_root/bin/node --version) == v$node_version && $($uv_bin --version | awk '{print $2}') == "$uv_version" ]] || { echo 'Declared tool installation did not verify.' >&2; exit 1; }
mkdir -p -m 700 "$deploy_base/bin" "$deploy_base/releases" "$state_root/control-plane" "$state_root/agent-host" "$config_dir" "$HOME/.config/systemd/user"
umask 077; token=$state_root/maintenance.token; [[ -e $token ]] || head -c 32 /dev/urandom | base64 > "$token"
if [[ -d $deploy_base/source.git ]]; then
  [[ $(git -C "$deploy_base/source.git" rev-parse --is-bare-repository) == true && $(git -C "$deploy_base/source.git" remote get-url origin) == https://github.com/jdip/Fesnyng.git ]] || { echo 'Existing source mirror does not match this installation.' >&2; exit 1; }
else
  git clone --mirror https://github.com/jdip/Fesnyng.git "$deploy_base/source.git"
fi
# Install only the stable selector; each selected release supplies its deployer.
install -m 700 "$root/scripts/update-test-deployment.sh" "$deploy_base/bin/update"
cat > "$deploy_base/bin/run-control" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail; source "${XDG_CONFIG_HOME:-$HOME/.config}/fesnyng-test/deployment.env"; release=$(readlink -f "$DEPLOY_BASE/current")
export FESNYNG_FRONTEND_DIST="$release/frontend/dist" FESNYNG_DEPLOYED_REVISION="$(<"$release/.fesnyng-revision")" FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN="$DEPLOY_ORIGIN"
exec "$UV_BIN" run --locked --project "$release/backend" uvicorn fesnyng_backend.web:create_app --factory --host 127.0.0.1 --port "$CONTROL_PORT" --no-proxy-headers
EOF
cat > "$deploy_base/bin/run-host" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail; source "${XDG_CONFIG_HOME:-$HOME/.config}/fesnyng-test/deployment.env"; release=$(readlink -f "$DEPLOY_BASE/current")
export FESNYNG_DEPLOYED_REVISION="$(<"$release/.fesnyng-revision")"
export FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/agent-host" FESNYNG_AGENT_HOST_IMAGE="fesnyng-agent:prepared-$FESNYNG_DEPLOYED_REVISION" FESNYNG_AGENT_HOST_CREDENTIAL_URL="$AGENT_HOST_CREDENTIAL_URL" FESNYNG_MAINTENANCE_TOKEN_FILE="$MAINTENANCE_TOKEN_FILE"
exec "$UV_BIN" run --locked --project "$release/backend" uvicorn fesnyng_backend.agent_host:create_app --factory --host 0.0.0.0 --port "$HOST_PORT" --no-proxy-headers
EOF
chmod 700 "$deploy_base/bin/run-control" "$deploy_base/bin/run-host"
for unit in control host update; do sed "s|__DEPLOY_BASE__|$deploy_base|g" "$root/deployment/systemd/fesnyng-test-$unit.service" > "$HOME/.config/systemd/user/fesnyng-test-$unit.service"; done
cp "$root/deployment/systemd/fesnyng-test-update.timer" "$HOME/.config/systemd/user/"
cat > "$config_dir/deployment.env.next" <<EOF
DEPLOY_BASE=$(printf '%q' "$deploy_base")
SOURCE_MIRROR=$(printf '%q' "$deploy_base/source.git")
UV_BIN=$(printf '%q' "$uv_bin")
STATE_ROOT=$(printf '%q' "$state_root")
MAINTENANCE_TOKEN_FILE=$(printf '%q' "$token")
MAINTENANCE_CURL_CONFIG=$(printf '%q' "$state_root/maintenance.curl")
CONTROL_PORT=8000
HOST_PORT=8001
export UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=10
DEPLOY_ORIGIN=$(printf '%q' "$origin")
AGENT_HOST_CREDENTIAL_URL=$(printf '%q' "$credential_url")
export PATH=$(printf '%q' "$node_root/bin"):\$PATH
EOF
mv "$config_dir/deployment.env.next" "$config_dir/deployment.env"
systemctl --user daemon-reload; systemctl --user enable fesnyng-test-control.service fesnyng-test-host.service fesnyng-test-update.timer
"$deploy_base/bin/update"
sudo -n tailscale serve --bg --https=443 "http://127.0.0.1:8000"
systemctl --user start fesnyng-test-update.timer
echo 'Installed. Complete the documented interactive owner bootstrap and organization/host binding before provider login.'
