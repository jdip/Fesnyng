# Run the local MVP

This guide starts one control plane, two independent agent-host APIs, and the
browser workspace on one macOS machine. Each applied agent receives one
Docker-managed OpenCode container. It is the local installation and recovery
guide for the MVP. [Issue #18](https://github.com/jdip/Fesnyng/issues/18) records
the retained full-system acceptance and delivery evidence.

The proof has four durable state owners:

| Owner | State | Keep it private |
| --- | --- | --- |
| Control plane | users, organizations, memberships, host allocations, agent configuration, and browser sessions | its SQLite state directory |
| Host A and Host B | their instance identity, organization binding digest, OAuth state, delivery receipts, and applied-agent records | one separate SQLite state directory per host |
| Docker | one labeled container and the `/home/agent` and `/workspace` volumes for every applied agent | data managed by the selected Docker engine; do not prune it during a proof |
| Browser | an authenticated session cookie and the selected organization, agent, and thread in the URL fragment | the browser profile used for the proof |

The control plane is management and browser access. A host owns its containers,
provider login and refresh credentials, and durable delivery work after it has
received configuration. A browser never receives an organization-host binding
token or OAuth refresh credential.

## Prerequisites

From the repository root, install the locked dependencies and verify Docker is
available. `scripts/setup.sh` uses `uv` from `PATH`, or the executable named by
`FESNYNG_UV`.

```bash
scripts/setup.sh
docker info
```

Use Node supported by `frontend/package.json`, npm, uv with Python 3.12 or
newer, and Docker Desktop or Colima. The runtime image requires a Docker host gateway
that containers can resolve. Docker Desktop normally supplies
`host.docker.internal`; a Colima installation commonly uses
`host.lima.internal` instead.

Choose a private state directory outside this Git checkout. Run this once in a
shell, and export the same `STATE_ROOT` in every terminal below. Generate one
binding token for each organization-host pair. Tokens are generated locally and
are never pasted into the browser or an agent container. This setup refuses to
overwrite an existing token file: retain that existing secret and use the
installation’s supported binding operations instead of inventing a rotation.

```bash
export STATE_ROOT="$HOME/.local/share/fesnyng-local-mvp"
mkdir -p "$STATE_ROOT/control-plane" "$STATE_ROOT/host-a" "$STATE_ROOT/host-b"
chmod 700 "$STATE_ROOT" "$STATE_ROOT/control-plane" "$STATE_ROOT/host-a" "$STATE_ROOT/host-b"
export HOST_A_BINDING_TOKEN_FILE="$STATE_ROOT/host-a-binding.token"
export HOST_B_BINDING_TOKEN_FILE="$STATE_ROOT/host-b-binding.token"
if [ -e "$HOST_A_BINDING_TOKEN_FILE" ] || [ -e "$HOST_B_BINDING_TOKEN_FILE" ]; then
  echo 'A binding token already exists; retain it instead of overwriting it.' >&2
  exit 1
fi
(
  umask 077
  set -C
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$HOST_A_BINDING_TOKEN_FILE" &&
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$HOST_B_BINDING_TOKEN_FILE"
)
chmod 600 "$HOST_A_BINDING_TOKEN_FILE" "$HOST_B_BINDING_TOKEN_FILE"
```

Use the Host A token only for the Host A binding and the Host B token only for
the Host B binding. Both the control-plane registration command and each host
bind command reject a token file readable by group or others.

Create the first installation owner before starting the control plane. The
command prompts twice for a password; do not put that password on a command
line. It refuses a second bootstrap against the same state.

```bash
FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" \
  uv run --locked --project backend python -m fesnyng_backend.cli bootstrap \
  --login owner --name 'Local Owner'
```

Build the pinned OpenCode runtime image once from the repository root:

```bash
docker build -t fesnyng-agent:local agent-runtime
```

## Start the services

Keep each long-running command in its own terminal. Before running an
individual command, set the same `STATE_ROOT` value in that terminal. These
commands deliberately use no daemon, PID file, or process manager: stop a
service with `Control-C` in the terminal that started it.

Start the control plane on loopback. `LOCALHOST_DEVELOPMENT=true` is required
for the local HTTP session cookie, and the allowed origin must match the Vite
origin exactly.

```bash
export STATE_ROOT="$HOME/.local/share/fesnyng-local-mvp"
FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" \
FESNYNG_CONTROL_PLANE_LOCALHOST_DEVELOPMENT=true \
FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN=http://127.0.0.1:5173 \
  uv run --locked --project backend uvicorn fesnyng_backend.control_plane:create_app \
  --factory --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
```

Start Host A and Host B in two more terminals. Bind each host API to `0.0.0.0`
so its own containers can reach the credential broker through the Docker
gateway. This proof therefore needs the host machine’s private local-network
policy to limit access to the host APIs. Set `HOST_GATEWAY` to the name that
works in the selected Docker environment.

```bash
# Terminal: Host A
export STATE_ROOT="$HOME/.local/share/fesnyng-local-mvp"
export HOST_GATEWAY=host.docker.internal
FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/host-a" \
FESNYNG_AGENT_HOST_IMAGE=fesnyng-agent:local \
FESNYNG_AGENT_HOST_CREDENTIAL_URL="http://${HOST_GATEWAY}:8001" \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app \
  --factory --host 0.0.0.0 --port 8001 --timeout-graceful-shutdown 5
```

```bash
# Terminal: Host B
export STATE_ROOT="$HOME/.local/share/fesnyng-local-mvp"
export HOST_GATEWAY=host.docker.internal
FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/host-b" \
FESNYNG_AGENT_HOST_IMAGE=fesnyng-agent:local \
FESNYNG_AGENT_HOST_CREDENTIAL_URL="http://${HOST_GATEWAY}:8002" \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app \
  --factory --host 0.0.0.0 --port 8002 --timeout-graceful-shutdown 5
```

Start the browser workspace in a fourth terminal:

```bash
FESNYNG_CONTROL_PLANE_URL=http://127.0.0.1:8000 npm run dev --prefix frontend
```

Vite serves `http://127.0.0.1:5173` and sends `/api` to the control plane. It
is a development server, not a replacement for the control plane or host APIs.

Confirm that each service is a distinct durable instance before allocating it:

```bash
curl --fail --silent http://127.0.0.1:8000/health | python3 -m json.tool
curl --fail --silent http://127.0.0.1:8001/health | python3 -m json.tool
curl --fail --silent http://127.0.0.1:8002/health | python3 -m json.tool

export HOST_A_ID="$(curl --fail --silent http://127.0.0.1:8001/health | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])')"
export HOST_B_ID="$(curl --fail --silent http://127.0.0.1:8002/health | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])')"
```

The `instance_id` is recorded in the service state. On a later restart with the
same state directory, it must remain the same. Supplying a contradictory
`FESNYNG_*_INSTANCE_ID` makes startup fail rather than silently changing
identity.

## Bootstrap the organization and bind both hosts

1. Open `http://127.0.0.1:5173` and sign in with the installation owner.
2. Create an organization. Its ID is the `organization` value in the browser
   URL fragment after selecting it. Copy that value into the terminal below.
3. Allocate both running hosts from the control-plane state, then bind the same
   organization to each autonomous host. Registration is installation work; it
   does not expose a host token to the browser.

```bash
export STATE_ROOT="$HOME/.local/share/fesnyng-local-mvp"
export HOST_A_BINDING_TOKEN_FILE="$STATE_ROOT/host-a-binding.token"
export HOST_B_BINDING_TOKEN_FILE="$STATE_ROOT/host-b-binding.token"
export ORGANIZATION_ID='replace-with-the-browser-organization-id'

FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" \
  uv run --locked --project backend python -m fesnyng_backend.cli register-host \
  --id "$HOST_A_ID" --name 'Local Host A' --api-url http://127.0.0.1:8001 \
  --organization "$ORGANIZATION_ID" --token-file "$HOST_A_BINDING_TOKEN_FILE"

FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" \
  uv run --locked --project backend python -m fesnyng_backend.cli register-host \
  --id "$HOST_B_ID" --name 'Local Host B' --api-url http://127.0.0.1:8002 \
  --organization "$ORGANIZATION_ID" --token-file "$HOST_B_BINDING_TOKEN_FILE"

FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/host-a" \
  uv run --locked --project backend python -m fesnyng_backend.host_cli bind-organization \
  --organization "$ORGANIZATION_ID" --token-file "$HOST_A_BINDING_TOKEN_FILE"

FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/host-b" \
  uv run --locked --project backend python -m fesnyng_backend.host_cli bind-organization \
  --organization "$ORGANIZATION_ID" --token-file "$HOST_B_BINDING_TOKEN_FILE"
```

Use the workspace header’s **Refresh** after registration. It also reloads the
selected conversation’s thread collection, including threads created by an
authorized peer or another client. In **Organization settings**, add members if
needed, create an OpenAI credential profile, and use
**Sign in on this host** separately for Host A and Host B. The page gives an
OpenAI verification URL and user code. Complete each device login in a browser.
The same OpenAI account may be used on both hosts, but each host keeps its own
OAuth login and refresh state; no credentials are synchronized between hosts.

Create an agent from the agent `+` control, select one registered home host and
the credential profile, then choose a logical workspace name, model,
instructions, and skills. **Save and apply** writes the desired configuration,
asks the assigned host to apply it, and sends the current collaboration roster.
An agent is ready only after its settings report `applied`; a busy host can
report a pending safe-boundary application instead. Repeat with an agent on the
other host for the two-host proof. Use **Apply collaboration configuration** in
Organization settings again after changing host placement or reporting
relationships.

Open an applied agent’s **Threads** page to start a conversation. The browser
uses an authenticated control-plane facade; it does not connect to the native
OpenCode port or agent-host credential API directly. The selected organization,
agent, and thread stay in the browser URL fragment, so a reload can reattach to
the selected authorized conversation.

## Persistence, stop, and restart

Stop the Vite server, control plane, and each host with `Control-C`. This does
not delete any SQLite state, containers, images, or agent volumes. The agent
containers have Docker’s `unless-stopped` restart policy, so stopping a Python
host is not a request to delete or reset an agent. Restart each service with the
same commands and state directories. Verify the three `/health` instance IDs
again, sign in if the browser session expired, and reopen the saved fragment.

When an agent configuration has been applied, its Docker resources are named
from the agent ID and labelled with `fesnyng.host`, `fesnyng.organization`, and
`fesnyng.agent`. Its two durable volumes end in `-home` and `-workspace` and
hold `/home/agent` and `/workspace`. Use labels to inspect only a known agent’s
resources; do not use broad `docker system prune`, delete its volumes, or
recreate a host state directory during recovery.

```bash
export AGENT_ID='replace-with-the-browser-agent-id'
docker ps -a --filter "label=fesnyng.agent=$AGENT_ID"
docker volume ls --filter "label=fesnyng.agent=$AGENT_ID"
```

If a host restarts, it reconciles delivery receipts with native history. It does
not resend a prompt, command, or interaction reply whose earlier effect is
uncertain. In the selected thread’s **Thread activity**, choose **Investigate
outcome** for an
`unresolved`, `uncertain`, `stopping`, or `submitting` delivery. Use
**Reconcile available evidence** first. Only after inspecting native history and
external effects should a human record `completed` or `failed` with evidence;
that resolution records the finding and never replays the original work.

Configured peers communicate directly while the control plane is offline.
Receiver outages retain and retry the same delivery identity. A definite first
rejection stops retries and returns a failure notice to the initiating thread;
correct the target and use a new delivery identity for changed work. If an earlier
attempt may have been accepted, the host retains an uncertain outcome instead
and notifies the source to inspect receiving history and receipts before sending
new work. The native `collaboration_status` tool exposes the retained receipt.

A host restart marks interrupted device-login or refresh work uncertain. Start a
fresh **Sign in on this host** flow for that host when needed. Do not copy a
refresh credential between Host A and Host B to repair it.

Use **Agent settings → Agent lifecycle** to start, stop, restart, or rebuild an
agent container. Stop persists until Start, including when settings are saved.
Stop/Restart require confirmation, with a typed random code when work is active.
Rebuild additionally warns that the container writable layer is replaced; retained
home/workspace volumes, identity, and host OAuth configuration are preserved.
Rebuild is the explicit recovery path for a missing container. Inspect any
**Recovery required** result through the affected thread's Investigate and
Reconcile actions; an uncertain external effect still needs a human finding.
Keep new submissions gated until that outcome is resolved.

The separate checkpoint replacement operation remains available through the
organization-bound host API. It waits for native sessions to be quiet and for
delivery effects to be reconciled, checkpoints the container image, retains the
labeled home and workspace volumes, and starts the replacement from that
checkpoint. This is an operator action, not a browser action. Substitute the
agent ID from the selected agent’s URL fragment. Select the host that owns that
agent and keep its private token file outside the command arguments. The
standard-library request below reads that token only inside the process.

```bash
export AGENT_HOST_ORIGIN=http://127.0.0.1:8001
export AGENT_BINDING_TOKEN_FILE="$HOST_A_BINDING_TOKEN_FILE"
python3 - "$AGENT_HOST_ORIGIN" "$ORGANIZATION_ID" "$AGENT_ID" "$AGENT_BINDING_TOKEN_FILE" <<'PY'
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import sys

origin, organization_id, agent_id, token_file = sys.argv[1:]
token = Path(token_file).read_text().strip()
request = Request(
    f"{origin}/organizations/{organization_id}/agents/{agent_id}/replace",
    headers={"Authorization": f"Bearer {token}"},
    method="POST",
)
try:
    with urlopen(request) as response:
        print(response.read().decode())
except HTTPError as error:
    raise SystemExit(f"replacement failed ({error.code}): {error.read().decode()}")
PY
```

If replacement returns a busy or reconciliation error, resolve the affected
delivery as described above and try only after it is settled. If a previously
applied container is missing without a recorded replacement checkpoint, the host
refuses to create a look-alike replacement. Preserve the host state, Docker
resources, and error evidence for inspection rather than deleting volumes or
replaying uncertain work.

## Messages, steering and workflows

Use the same chat composer throughout a conversation. Enter sends a message when
idle and steers the agent while it is working; Shift+Enter adds a newline. Queue
is a secondary action beside the composer, and Stop remains directly accessible
during a run. A queued message waits for current work to finish.

Type `/` to select an applied explicit workflow, then add any instructions and
submit from the composer. Configure explicit workflows under Agent settings and
apply the configuration before expecting them in the picker. A submission error
preserves the draft so it can be corrected or retried.

## Local proof boundaries

This guide establishes the local topology only. Use `scripts/check.sh` for the
repository’s complete static and behavior gate. The opt-in real-Docker test is:

```bash
FESNYNG_DOCKER_TESTS=true uv run --locked --project backend \
  pytest backend/tests/test_docker_integration.py -q
```

That test creates isolated resources and retains failed resources for diagnosis.
It does not establish an authenticated provider request. The retained MVP proof
also requires live browser, device-login, Docker, host-restart, direct-peer, and
provider evidence under [issue #18](https://github.com/jdip/Fesnyng/issues/18).
Use that record to distinguish verified behavior from remaining limitations.
