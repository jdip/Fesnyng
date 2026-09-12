# Fesnyng backends

The independently runnable control-plane and agent-host APIs each own a private SQLite file, state directory and durable UUID. The control plane also owns users, memberships, desired organization policy, agent configuration and host allocations. The host's execution and OAuth integration are the next approved milestone.

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

Each service defaults to `./.fesnyng/<service>/fesnyng.sqlite3`. Set `FESNYNG_CONTROL_PLANE_STATE_DIRECTORY` or `FESNYNG_AGENT_HOST_STATE_DIRECTORY` to choose another private directory, and the corresponding `*_DATABASE_PATH` to override the database location. Use the same values for installation commands and server startup. Existing state must have private permissions; the app does not change unrelated directory permissions.

Run two independent host instances with distinct directories and ports:

```bash
FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/host-a \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app --factory --host 127.0.0.1 --port 8001
FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/host-b \
  uv run --locked --project backend uvicorn fesnyng_backend.agent_host:create_app --factory --host 127.0.0.1 --port 8002
```

`GET /health` returns the service, durable instance UUID and foundation schema version. An optional `FESNYNG_CONTROL_PLANE_INSTANCE_ID` or `FESNYNG_AGENT_HOST_INSTANCE_ID` sets its first UUID; subsequent starts reject mismatches. Control-plane tables evolve independently of the foundation identity and host state.

## Organization and agent management

Authenticated users create organizations and become their owners. Owners/admins can add a new user with a password, or add an existing login without supplying a password, through `PUT /organizations/{id}/members`. Existing users' credentials are never overwritten. Members can use authorized organization resources; owners/admins manage them. Membership revocation takes effect on subsequent requests.

Host registration is an installation operation. After creating an organization, substitute its UUID and the host's `/health` UUID into:

```bash
uv run --locked --project backend python -m fesnyng_backend.cli register-host \
  --id HOST_UUID --name 'Local host' --api-url http://127.0.0.1:8001 --organization ORGANIZATION_UUID
```

Only allocated hosts appear through organization APIs. One host can be allocated to multiple organizations. Ordinary organization management cannot alter its network address or infrastructure.

The organization API exposes `agents`, `profiles`, `hosts` and `policy` under `/organizations/{id}`. Credential profiles are metadata here; host-local login/refresh arrives with host execution. Agent creation assigns one home host. Configuration edits carry `expected_version`; stale updates return 409. When supplied in a patch, `configuration` replaces the complete configuration object. Host assignment and applied status are not client-writable. Desired state remains pending until the assigned host acknowledges it in the host integration milestone.

Agent workspaces are logical names, not arbitrary filesystem paths. Reusable skills and explicit-only commands have distinct assignments. Organization policy defaults to `allow`, with separately represented mandatory permissions and authorized thread overrides; enforcement belongs to host configuration application.
