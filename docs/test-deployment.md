# Persistent test installation

Install one fresh, user-owned test instance from a clean candidate checkout:

```bash
scripts/install-test-deployment.sh \
  --origin https://YOUR-TAILNET-NAME.ts.net \
  --agent-host-credential-url http://YOUR-DOCKER-GATEWAY:8001
```

The installer reads the exact Node version from
[`frontend/.tool-versions`](../frontend/.tool-versions) and uv from
[`backend/.tool-versions`](../backend/.tool-versions), verifies the Node archive
checksum, creates a private XDG deployment/state split, and installs user
services plus a five-minute `OnCalendar=*:0/5` timer. It prepares locked backend,
frontend, and agent-runtime dependencies before atomically activating a release.
The stable updater lives outside `current`, so an installation started from a
candidate can first activate the existing `test` revision and later update to the
merged candidate.

The browser/control service listens on loopback and is served by one private
Tailscale HTTPS route. The host listens on port 8001 for Docker bridge clients;
the firewall profile must allow only the reviewed Docker bridge and loopback path
to that port and deny tailnet access. The installer refuses an unavailable
Tailscale command; inspect existing Serve state and configure the exact private
route before use. It never enables Funnel, resets Tailscale, changes UFW, or
changes Docker grants.

Verify the firewall before running the installer. Tailscale's `ts-input` accept
rule can run before UFW, so an earlier UFW deny is insufficient. The installed
host uses a dedicated nftables `inet fesnyng_host_api` input chain at priority
`-10` with `iifname "tailscale0" tcp dport 8001 counter drop`. Its root-owned
`fesnyng-host-api-firewall.service` loads only that table from
`/etc/fesnyng/host-api-firewall.nft`; it never flushes the shared ruleset.
Its unit uses `DefaultDependencies=no`, `Before=network-pre.target tailscaled.service docker.service shutdown.target`, `After=local-fs.target`, and `Conflicts=shutdown.target` so the narrow deny is installed before network-facing owners and removed only at shutdown.
Keep the existing Docker guard and the narrow UFW Docker bridge callback allow.
Verify loopback and Docker bridge access, and tailnet IPv4/IPv6 and LAN denial,
with actual requests. Inspect the dedicated drop counter to prove which owner
enforces the boundary. Do not enable a stock nftables service that flushes rules.
The user service manager must have lingering enabled for unattended startup.

If a first install stops before `deployment.env` exists, no application has been
started. Verify that the directories and units belong to this attempt. Remove its
empty `control-plane` and `agent-host` directories with `rmdir`, and remove only
its generated `fesnyng-test-{control,host,update}.service` and
`fesnyng-test-update.timer` files from `~/.config/systemd/user`, if present.
Retain downloaded tooling and the source mirror, then rerun the installer with
the same arguments. If either state directory is nonempty, stop and inspect it;
do not delete it to make the preflight pass.

The installer publishes `deployment.env` atomically after the updater, wrappers,
source mirror and unit files exist. If that configuration exists, retain it and
finish installation with the following commands. If `current` already exists
and the updater reports recovery is required, use the health/admission recovery
below first. Recheck that the Serve configuration is empty before creating the
route; preserve an existing route and verify it separately.

```bash
source "$HOME/.config/fesnyng-test/deployment.env"
systemctl --user daemon-reload
systemctl --user enable fesnyng-test-control fesnyng-test-host fesnyng-test-update.timer
"$DEPLOY_BASE/bin/update"
sudo tailscale serve status
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
systemctl --user start fesnyng-test-update.timer
```

`systemctl --user status fesnyng-test-control fesnyng-test-host
fesnyng-test-update.timer` and `journalctl --user -u fesnyng-test-update` show
service and update state. The updater serializes scheduled and manual runs with
`flock`, logs unchanged revisions as no-ops, builds a non-live release, then uses
the host-local maintenance token file to acquire admission immediately before the
symlink swap. It restarts both services, requires their health revisions to match,
and only then releases admission. Busy or unavailable maintenance defers without
changing `current`. A failure after acquire deliberately leaves admission closed:
inspect the journal and both health endpoints, repair the retained release, then
POST `/maintenance/release` on loopback with the private curl config. Do not
delete state, interrupt agents, or use rollback automation.

For manual recovery, first inspect the retained state and exact current revision:

```bash
source "$HOME/.config/fesnyng-test/deployment.env"
curl --config "$MAINTENANCE_CURL_CONFIG" http://127.0.0.1:"$HOST_PORT"/maintenance
curl --fail http://127.0.0.1:"$CONTROL_PORT"/health
curl --fail http://127.0.0.1:"$HOST_PORT"/health
journalctl --user -u fesnyng-test-update -u fesnyng-test-control -u fesnyng-test-host
systemctl --user restart fesnyng-test-control fesnyng-test-host
```

Only after both health responses report the intended revision, reopen admission:

```bash
curl --config "$MAINTENANCE_CURL_CONFIG" -X POST http://127.0.0.1:"$HOST_PORT"/maintenance/release
```

After initial service health succeeds, create the first owner interactively:

```bash
source "$HOME/.config/fesnyng-test/deployment.env"
FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" \
  "$UV_BIN" run --locked --project "$DEPLOY_BASE/current/backend" \
  python -m fesnyng_backend.cli bootstrap --login owner --name Owner
```

Create the organization, register and bind the host with a mode-600 binding-token
file, then complete provider device login in a browser. These human credential
steps are intentionally separate from installation and update automation.

With the operator-provided organization UUID and a private binding token file:

```bash
source "$HOME/.config/fesnyng-test/deployment.env"
HOST_ID=$(curl --fail --silent http://127.0.0.1:"$HOST_PORT"/health | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])')
FESNYNG_CONTROL_PLANE_STATE_DIRECTORY="$STATE_ROOT/control-plane" "$UV_BIN" run --locked --project "$DEPLOY_BASE/current/backend" python -m fesnyng_backend.cli register-host --id "$HOST_ID" --name 'Test host' --api-url http://127.0.0.1:"$HOST_PORT" --organization "$ORGANIZATION_UUID" --token-file "$BINDING_TOKEN_FILE"
FESNYNG_AGENT_HOST_STATE_DIRECTORY="$STATE_ROOT/agent-host" "$UV_BIN" run --locked --project "$DEPLOY_BASE/current/backend" python -m fesnyng_backend.host_cli bind-organization --organization "$ORGANIZATION_UUID" --token-file "$BINDING_TOKEN_FILE"
```
