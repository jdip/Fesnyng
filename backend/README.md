# Fesnyng backends

For the complete local topology—one control plane, two local hosts, Docker
agents, browser setup, persistence, and recovery—start with the
[local retained-MVP guide](../docs/local-mvp.md). This document remains the
backend contract reference for its exact service and API behavior.

The independently runnable control-plane and agent-host APIs each own a private SQLite file, state directory and durable UUID. The control plane owns users, memberships, desired organization policy, agent configuration and host allocations. Each agent host owns Docker execution and its own credential state.

## Install and verify

Use the uv version in [`.tool-versions`](.tool-versions), installed through an [official uv installation method](https://docs.astral.sh/uv/getting-started/installation/). The repository's `scripts/setup.sh` and `scripts/check.sh` consume the committed manifests and locks. For focused Python checks from the repository root:

```bash
uv run --locked --project backend ruff check backend
uv run --locked --project backend ruff format --check backend
uv run --locked --project backend ty check --project backend
(cd backend && uv run --locked pytest)
```

## Bootstrap and run

Create the initial human login locally before serving the control plane. The command prompts without echoing the password and refuses a second bootstrap. It uses the same environment-selected SQLite state as the API:

```bash
uv run --locked --project backend python -m fesnyng_backend.cli bootstrap --login owner --name Owner
```

For local development, explicitly configure the frontend origin and localhost cookies, then start the control plane:

```bash
FESNYNG_CONTROL_PLANE_LOCALHOST_DEVELOPMENT=true \
FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN=http://127.0.0.1:5173 \
  uv run --locked --project backend uvicorn fesnyng_backend.control_plane:create_app --factory --host 127.0.0.1 --port 8000
```

Secure cookies are the default for HTTPS installations. Configure the actual frontend origin; unsafe browser requests require its exact Origin and the session's CSRF token. `POST /auth/login` takes `login` and `password`; `GET /auth/session` restores the user and CSRF token from the HttpOnly session cookie after a reload. Send `X-CSRF-Token` on mutations. Logout and password changes revoke stored sessions. The frontend organization workspace is delivered in its later approved ticket.

### Serve the compiled browser workspace

The direct `fesnyng_backend.control_plane:create_app` entrypoint remains the API-only
service. A deployed browser service instead serves the compiled Vite directory and
mounts that same API below `/api`:

```bash
FESNYNG_FRONTEND_DIST=/srv/fesnyng/releases/REVISION/frontend/dist \
FESNYNG_DEPLOYED_REVISION=FULL_LOWERCASE_GIT_SHA \
FESNYNG_CONTROL_PLANE_ALLOWED_ORIGIN=https://fesnyng.example.ts.net \
  uv run --locked --project backend uvicorn fesnyng_backend.web:create_app \
  --factory --host 127.0.0.1 --port 8000
```

`FESNYNG_FRONTEND_DIST` must be an absolute directory containing the built
`index.html`; the browser entrypoint will not fall back to a source checkout.
Client-side workspace routes receive that index, while missing `/api` routes and
missing static assets remain HTTP 404. Session cookies retain their `/` path, so
the browser sends them to `/api`; the existing secure-cookie and exact-origin CSRF
rules therefore apply unchanged. `FESNYNG_DEPLOYED_REVISION` is optional for local
runs. In a deployment it is the lowercase 40-character Git SHA that both `/health`
and `/api/health` report as `revision`, allowing an updater to verify the activated
release without reading deployment state from the source tree.

Each service defaults to `./.fesnyng/<service>/fesnyng.sqlite3`. Set `FESNYNG_CONTROL_PLANE_STATE_DIRECTORY` or `FESNYNG_AGENT_HOST_STATE_DIRECTORY` to choose another private directory, and the corresponding `*_DATABASE_PATH` to override the database location. Use the same values for installation commands and server startup. Existing state must have private permissions; the app does not change unrelated directory permissions.

For a two-host local proof, use the installation commands below with separate state directories, host API ports, and container-reachable credential URLs for each host.

`GET /health` returns the service, durable instance UUID and foundation schema version. An optional `FESNYNG_CONTROL_PLANE_INSTANCE_ID` or `FESNYNG_AGENT_HOST_INSTANCE_ID` sets its first UUID; subsequent starts reject mismatches. Control-plane tables evolve independently of the foundation identity and host state.

## Organization and agent management

Authenticated users create organizations and become their owners. Owners/admins can add a new user with a password, or add an existing login without supplying a password, through `PUT /organizations/{id}/members`. Existing users' credentials are never overwritten. Members can use authorized organization resources; owners/admins manage them. Membership revocation takes effect on subsequent requests.

Host registration is an installation operation. After creating an organization, substitute its UUID and the host's `/health` UUID into:

```bash
uv run --locked --project backend python -m fesnyng_backend.cli register-host \
  --id HOST_UUID --name 'Local host' --api-url http://127.0.0.1:8001 --organization ORGANIZATION_UUID
```

Only allocated hosts appear through organization APIs. One host can be allocated to multiple organizations. Ordinary organization management cannot alter its network address or infrastructure.

The organization API exposes `agents`, `profiles`, `hosts` and `policy` under `/organizations/{id}`. Credential profiles are metadata here; each host owns the local login and refresh state for its assigned profiles. Agent creation assigns one home host. Configuration edits carry `expected_version`; stale updates return 409. When supplied in a patch, `configuration` replaces the complete configuration object. Host assignment and applied status are not client-writable. Desired state remains pending until the assigned host acknowledges the exact configuration version.

Agent workspaces are logical names, not arbitrary filesystem paths. New agents default to `gpt-6-astra`, verified for fork continuations; an agent configuration can still select another model. Reusable skills and explicit-only commands have distinct assignments. Organization policy defaults to `allow`, with separately represented mandatory permissions and authorized thread overrides; enforcement belongs to host configuration application.

New thread directories are prepared under the host's organization/employee-scoped
workspace root. Projects can select a repository and explicit checkout branch;
browser callers cannot select an arbitrary directory or inject a repository into
the host handoff. See [thread workspaces](../docs/workspaces.md) for shared-path
storage, administrator Git authentication, legacy volumes and creation recovery.

Agent configuration also identifies the harness with `runtime_type`, defaulting to
`opencode` when omitted. Existing configurations and thread IDs remain valid.
Each host thread retains its original runtime binding; saving thread metadata must
not reassign that binding or its organization/agent ownership. New employees may
select `codex` to use the pinned Codex App Server. An ordinary configuration
update cannot change an existing employee's harness. Unsupported native
operations fail visibly. Both integrations follow the approved
[harness integration specification](https://github.com/jdip/Fesnyng/issues/83).

### Switch an existing employee's harness

An owner or admin starts the only supported change with
`POST /organizations/{organization}/agents/{agent}/harness-switch` and
`{ "expected_version": N, "target_runtime_type": "opencode" | "codex" }`.
The source configuration must be the exact applied version. The host first closes
admission and requires queued, active, uncertain, interaction, and peer delivery
effects to settle or be explicitly resolved. It then captures complete verified
history and provenance for every mapped root and descendant before one atomic,
permanent freeze. A failed capture leaves the source harness selected and
writable; a committed freeze is never undone.

Only after the host returns its frozen receipt does the control plane select the
target configuration. The response reports `switch_state: "pending_apply"`;
apply the selected configuration to create a replacement runtime. A lost response
or restart keeps the durable switch intent. Retry the switch endpoint only while
the control-plane source remains selected; after it reports `pending_apply`,
apply the selected target. The host's
`GET /organizations/{organization}/agents/{agent}` runtime status exposes
`capturing` or `frozen` state and the target harness until application succeeds.
Configuration application rebuilds the container while preserving the employee's
owned `/home/agent` and `/workspace` volumes, then clears the switch state only
after the target is actually applied.

Frozen records remain in the complete session inventory with immutable
`runtime_type` and `frozen_at`; harness façade records also project
`frozen: true`. The original `/opencode` or `/codex` read routes serve their
host-owned snapshots without contacting the old native server. All native
mutations against a frozen thread return a read-only conflict. Snapshot history
includes verified child ancestry and item/turn provenance, so a switch does not
reinterpret a thread through the replacement harness.

The authorized `opencode/session/{id}/context` read exposes display-safe repository
and branch metadata for the mapped thread workspace. Git changes compare tracked
staged and unstaged files against HEAD, with binary and untracked file counts
reported separately. Repository metadata must stay inside that workspace; external
diff, text conversion and filesystem-monitor commands are disabled. The response
distinguishes absent repositories and unavailable data, including unborn HEAD,
and never returns filesystem paths or remote URLs. Subagent counts describe
verified native descendant sessions; background processes remain unavailable
without authoritative thread attribution. The existing agent image declares Git
as the owner of these read-only repository calculations.

## Install an agent host

The host launches one container running the selected OpenCode or Codex harness per applied agent. Build the pinned runtime image from the repository root before applying an agent configuration:

```bash
docker build -t fesnyng-agent:local agent-runtime
```

Run each host with a private state directory, its own API port, the image name, and a credential URL that its containers can reach. Bind the host API to `0.0.0.0` so containers can reach it through the Docker host gateway. `FESNYNG_AGENT_HOST_CREDENTIAL_URL` must use the gateway name available inside the agent containers: Docker Desktop commonly provides `host.docker.internal`; Colima may provide `host.lima.internal`. The runtime's `host.lima.internal` default is therefore not portable—set the variable explicitly for every host.

```bash
# Set this to the Docker host gateway name resolvable inside your agent containers.
HOST_GATEWAY=host.docker.internal

# Host A
FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/host-a \
FESNYNG_AGENT_HOST_IMAGE=fesnyng-agent:local \
FESNYNG_AGENT_HOST_CREDENTIAL_URL=http://${HOST_GATEWAY}:8001 \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app \
  --factory --host 0.0.0.0 --port 8001

# Host B
FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/host-b \
FESNYNG_AGENT_HOST_IMAGE=fesnyng-agent:local \
FESNYNG_AGENT_HOST_CREDENTIAL_URL=http://${HOST_GATEWAY}:8002 \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app \
  --factory --host 0.0.0.0 --port 8002
```

Read each host's `GET /health` response and use its `instance_id` when registering that host. Its durable instance identity, SQLite state, agent containers, and credential profiles are independent.

### Bind an organization to its host

An organization-host binding uses one opaque token shared by the control plane and the selected host. Keep the token in a file outside the repository with permissions no broader than `0600`; both installation commands reject a public token file. Substitute the same private file, organization UUID, and host UUID in these commands:

```bash
uv run --locked --project backend python -m fesnyng_backend.cli register-host \
  --id HOST_UUID --name 'Local host' --api-url http://127.0.0.1:8001 \
  --organization ORGANIZATION_UUID --token-file /private/path/host-binding-token

FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/host-a \
  uv run --locked --project backend python -m fesnyng_backend.host_cli bind-organization \
  --organization ORGANIZATION_UUID --token-file /private/path/host-binding-token
```

`register-host` makes the host available for organization placement and stores the control-plane side of the binding. `bind-organization` stores only the binding digest on the host. The control plane uses this token for host API calls; browsers and agent containers do not receive it.

Owners and admins create profile metadata and agent configuration in the control plane. Applying an agent sends its versioned configuration to its assigned host. The host accepts only its assigned organization binding and acknowledges the exact host, organization, agent, policy, and configuration version. A failed or mismatched acknowledgement leaves the control-plane agent pending.

## Host-local OAuth profiles

Credential profiles are organization-scoped metadata in the control plane and durable OAuth state on each assigned host. An owner or admin starts device authorization with `POST /organizations/{organization_id}/hosts/{host_id}/profiles/{profile_id}/login` through the authenticated control-plane API. The response contains the official device verification URL and user code. Complete that step in a browser; access and refresh tokens remain in the host's private SQLite state and are never returned by profile status or control-plane APIs.

The host is the only login and refresh owner. It derives the assigned profile from each container's host-issued agent key, serializes refreshes per profile, and fails closed on an uncertain rotation or account mismatch. Several agents on one host can share a profile. A second host can authenticate the same account through a separate login, but it keeps independent refresh credentials and receives no cross-host token synchronization.

Codex uses App Server's experimental externally managed ChatGPT token mode. The
host supplies access credentials and answers native refresh requests, including
rejection before expiry. A rejected credential generation is refreshed once for
all agents sharing that host profile; stale rejection requests use the newer
credential. Refresh tokens remain with the host, and uncertain refresh outcomes
require reconciliation instead of falling back to a rejected access token.

The broker verifies the assigned profile and account scope for every credential
response. A container receives only a short-lived access token and account
metadata; it cannot perform device login, refresh, or move a profile to another
host. A profile can be shared by agents on its host, but a different host requires
its own login and refresh state.

## Container state and replacement

Applied agents use labeled Docker volumes for `/home/agent` and `/workspace`; native server ports are published only on loopback. The host writes the agent's configuration, skill files, and broker configuration with a private umask. Codex uses an authenticated App Server connection for native requests and events. Docker receives no socket mount from this runtime.

Organization owners and admins manage **Start**, **Stop**, **Restart**, and
**Rebuild** from Agent settings. The control-plane
`POST /organizations/{organization}/agents/{agent}/lifecycle` endpoint accepts
`action`, `confirmed`, and an optional `code`; it supplies authenticated actor
provenance to the owning host. Stop, Restart, and Rebuild require confirmation.
Active work additionally requires a short, expiring, one-use random code, and
the host rechecks activity before acting. Interruption uses native abort and
delivery reconciliation. A code never authorizes discarding uncertain effects.

Stop is durable: reconciliation and saving configuration do not silently start
the container. Start applies pending configuration; Restart restarts the existing
container. Runtime status distinguishes desired state from actual container
availability and exposes transition and recovery states.

Rebuild creates a replacement from the configured runtime image and keeps the
agent identity, ownership-validated home/workspace volumes, and host-owned OAuth
binding. It cannot recover a missing container's writable layer. New submissions
remain gated while retained native history and delivery effects are reconciled.
If the result remains uncertain, investigate the affected thread and reconcile
available evidence before recording an attributed human outcome. Rebuild never
replays an uncertain delivery or copies OAuth credentials between hosts.

The organization-bound host API provides `POST /organizations/{organization_id}/agents/{agent_id}/replace`. It waits for native sessions to be idle, stops the container, commits a checkpoint image, removes the container, and starts the replacement from that checkpoint while retaining the labeled home and workspace volumes. If an already-applied container is missing without a recorded checkpoint, the host retains state for inspection and refuses replacement.

Replacement also requires durable delivery effects to be reconciled. An idle native session alone does not authorize discarding uncertain work.

## Thread delivery, policy and memory

The control-plane and organization-bound host APIs expose thread delivery under `/organizations/{organization}/agents/{agent}/sessions/{session}/dispatches`. Submit a stable UUID `id`, `text` (or a native `command` and its text arguments), and `mode`: `queued` preserves thread order; `steering` joins an established native run. Repeating the same ID and content returns the existing receipt; a conflicting retry returns 409. Different threads can run concurrently. The host persists delivery state and attribution before contacting OpenCode, and continues processing without the browser or control plane.

An explicit `stop` has its own receipt; `cancel_queued` additionally cancels work that has not been submitted. Inspect the receipt's state and outcome instead of treating HTTP acceptance as execution completion. Steering may contribute to a shared native response rather than produce a separate answer. Native message IDs preserve that relationship.

Read `questions` and `permissions` beneath a thread, and reply to a native request ID with a stable `operation_id`. Question replies contain nested `answers`; permission replies accept `once` or `reject`. The control plane derives the human author from the authenticated session. Replies and their receipts remain scoped to the exact thread and native request.

Thread `policy` reads expose desired and applied revisions. Writes include `expected_revision` and complete override `rules`. Organization policy controls whether overrides are allowed; mandatory organization rules remain the final authority. Changed permissions interrupt native work when necessary and clear remembered grants before verification. Pinned OpenCode does not consistently inherit restrictions in new and reused native child sessions. Restricted threads therefore block native task delegation, subtask commands, and executable command-template substitutions; broad-default threads retain native delegation. These guards cover the pinned runtime’s [child-policy inheritance](https://github.com/anomalyco/opencode/blob/3104c1428ec91f809e5ab86631300de41eb6952e/packages/opencode/src/agent/subagent-permissions.ts) and [command expansion](https://github.com/anomalyco/opencode/blob/3104c1428ec91f809e5ab86631300de41eb6952e/packages/opencode/src/session/prompt.ts), which run outside the complete parent-session policy. Other agent configuration waits for native work and uncertain effects to settle. Pending changes are retried by the autonomous host.

Codex applies only the policy App Server can prove: `default_permission: "allow"`
with no mandatory permissions and no thread overrides. A Codex target with an
unrepresentable policy is rejected before any source thread freezes. Codex uses
native `turn/start`, `turn/steer`, and `turn/interrupt` receipts. Fesnyng records
the durable delivery UUID as the native client message correlation value, but an
ambiguous native submission is still unresolved and is never automatically
replayed.

Explicit agent `memory` is stored separately from native conversation history. List entries or read a key beneath `/organizations/{organization}/agents/{agent}/memory`; PUT an entry with `key`, `content`, and `expected_revision` (zero creates). Stale edits return 409. Entries retain revision, author, and update time. Agent containers use the maintained MCP SDK's authenticated memory tools; the host derives their agent identity from the host-issued credential.

After a restart, the host reconciles native receipts without resending uncertain prompts, commands, or replies. An unresolved delivery can be explicitly resolved through its `/resolve` endpoint with an `operation_id`, `outcome` (`completed` or `failed`), and inspected `evidence`. This records an attributed resolution, preserves the native receipt, and never replays the payload. Resolution requires native work to be quiet and its tool states settled.

Each host database has one live API-process owner. Startup marks abandoned login and refresh operations uncertain while preserving account identity and token generation; a fresh host-local login can recover them. It never repeats a possibly completed refresh rotation. Run one API worker per host database, and use separate databases and ports for independent hosts.

## Assistant workspace façade

The browser-facing control plane proxies finite harness façades to the assigned
host at `/organizations/{organization}/agents/{agent}/opencode` and `/codex`.
The host, rather than the browser, owns the allowlist, mapped-session scope, and
native runtime credentials. Control-plane reads require organization membership;
mutations also require the browser CSRF token and forward a binding-authenticated
`X-Fesnyng-Actor` provenance header plus an `Idempotency-Key` where a durable
prompt, abort, or interaction reply needs one. Browsers never receive host
bindings or native credentials.

The OpenCode façade exposes mapped session list/create/read/update/delete, native
message history, durable `prompt_async` and abort receipts, bounded question and
permission replies, provider/config display, and an authorized event stream.
The Codex façade preserves its native thread, turn, and item representation for
mapped session reads, history, prompts, aborts, pending replies, configured
explicit skills, and events; unavailable native parity is reported rather than
synthesized. OpenCode continues to own native conversation history and
execution. Fesnyng's `HostStore` owns archive state only for navigation among
its mapped threads: the pinned OpenCode version accepts a null archive timestamp
without clearing its native archive record, so scoped list, session, and event
responses project the host's archive metadata. Archive state has no execution or
history effect.

`GET /file` and `GET /file/content` require a mapped `sessionID` and a relative path. The host resolves that path inside the mapped workspace and rejects absolute, traversal, symlink, and cross-workspace escapes. A fork makes a controlled workspace copy, asks native OpenCode to fork the source history, then uses pinned OpenCode's [`move-session`](https://github.com/anomalyco/opencode/blob/3104c1428ec91f809e5ab86631300de41eb6952e/packages/opencode/src/server/routes/instance/httpapi/groups/control-plane.ts) endpoint to move only that fresh child into the copy with `moveChanges: false`. The host verifies the child's destination, metadata, and history before mapping it; it never rewrites native history. Forked workspaces are distinct thread directories sharing the agent container’s filesystem permissions. Host-managed instructions direct continuations to use the current native working directory rather than historical absolute paths; the Docker isolation boundary is the agent. Save and apply an agent configuration after a host upgrade to refresh its managed continuation guidance. Copies with a `.git` file or any symlink are rejected because they could alias another checkout. If copy, fork, or move cannot be verified, both the copied workspace and native child are retained for inspection instead of being replayed. The façade does not provide a general filesystem or native HTTP proxy.

`GET /event` relays only events whose session is a mapped root or a verified native descendant. It attaches newly mapped thread directories while open and removes unsupported native permission `always` choices from live events. If a native event relay ends after disposal or restart, the façade ends the client stream so the client reconnects to a fresh authorized stream.

## Direct agent collaboration

An organization owner or admin applies the current roster and peer routes with authenticated, CSRF-protected `POST /organizations/{organization_id}/peers/apply` on the control plane. `GET /organizations/{organization_id}/peers` reports desired and confirmed host versions, including partial application. Apply again after changing agent placement, reporting relationships, or registered host origins. Peer credentials are directional and separate from control-plane management bindings. Their values stay in private backend state; status responses omit them.

Hosts cache the applied roster, reporting relationships and authenticated direct connections. The native MCP connection exposes `discover_threads`, `read_thread`, `contribute_to_thread`, `delegate`, and `collaboration_status`. Discovery supports agent, workspace, topic and activity filters, ranks colleagues by reporting-chart distance, and reports unavailable peers alongside healthy results. Membership in the same organization governs access; reporting distance only guides selection.

Contribution and delegation require a stable delivery UUID and an owned `source_session`. OpenCode's remote MCP requests do not carry native thread identity, so this field is validated agent-supplied attribution. Agents can discover their own thread IDs. Contributions address an existing receiving thread; delegation reserves a new native thread. Identical retries retain the original receipt and receiving thread; conflicting content is rejected. Native session metadata correlates interrupted creation, and ambiguous creation remains unresolved instead of creating another thread.

Peer acceptance and native completion are distinct. Hosts retain queued work during receiver outages and retry through direct connections. Completed or failed work returns a linked result to the initiating thread; result messages do not trigger recursive result notifications. Configured peer traffic and recovery continue while the control plane or browser is unavailable. Inspect uncertain receipts and native effects before repeating work. An exact retry of a previously accepted inbound peer delivery can return its durable receipt after the receiver thread freezes; it does not create or replay native work. New or nonterminal peer recovery remains blocked by the switch gate.

## Native Docker verification

Build the pinned image, then opt into the real OpenCode boundary check from the
repository root:

```bash
docker build -t fesnyng-agent:local agent-runtime
FESNYNG_DOCKER_TESTS=true uv run --locked --project backend \
  pytest backend/tests/test_docker_integration.py -q -s
```

The two original tests create an isolated temporary host, container, labeled
volumes and state, and print their identities before setup. They stop temporary
compute on success, ordinary failure and handled interruption. Successful checks
remove their fixtures; failures retain stopped containers and required diagnostic
state, volumes and checkpoint images. Inspect the reported disposition before
resuming a failed proof, and preserve the original failure when investigating a
teardown error. Hard termination or host loss can bypass handlers and requires
later ownership-based cleanup. Use the [preview stop/restart procedure](../docs/local-mvp.md#end-a-temporary-preview)
for a full installation; its durable agents have a separate lifecycle.
The credential-free Codex dispatch/reconnect and both-direction harness-switch
checks use the pinned image explicitly:

```bash
FESNYNG_CODEX_DOCKER_TESTS=true FESNYNG_CODEX_TEST_IMAGE=fesnyng-agent:local \
  uv run --locked --project backend pytest \
  backend/tests/test_codex_docker_integration.py \
  backend/tests/test_harness_switch_docker.py -q -s
```

These checks prove native protocol and retained-volume behavior without an
authenticated provider response. A real ChatGPT account request still requires a
host-local profile login and its separate operator evidence.
