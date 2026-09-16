# Development and verification

Fesnyng has separate Python/FastAPI control-plane and agent-host services in `backend/`, and a React/TypeScript/Vite application in `frontend/`. The Python package shares contracts while each service owns its SQLite database and schema. Product code and contracts are **Application Code**; setup and delivery scripts are **Tooling**.

The current API supports service health, durable identity, human authentication, organizations, memberships, agent configuration, independent Docker hosts and host-local shared OAuth. Each employee selects OpenCode or Codex App Server; switching safely freezes prior native threads into host-owned readable history. Hosts implement durable thread delivery, native interactions, permission reconciliation, explicit memory, and direct peer discovery/collaboration with cached routing. The assistant-ui conversation workspace is described in [frontend/README.md](../frontend/README.md). Use the [local retained-MVP guide](local-mvp.md) for one control plane, two local hosts, Docker, login, persistence, and recovery. Initial full-system verification is recorded in [issue #18](https://github.com/jdip/Fesnyng/issues/18); dual-harness acceptance is recorded in [issue #89](https://github.com/jdip/Fesnyng/issues/89) under the [approved harness specification](https://github.com/jdip/Fesnyng/issues/83).

## Install and check

Use a Node version supported by `frontend/package.json` (verified with Node 22.23.1), npm, and uv with Python 3.12 or newer. Dependencies belong in `backend/pyproject.toml`, `frontend/package.json` and `agent-runtime/package.json`, with their committed lockfiles. Follow `backend/README.md` for Python runtime details and `agent-runtime/README.md` for the pinned Docker runtime and native auth plugin. If uv is outside PATH, set `FESNYNG_UV` to its absolute executable path; the setup and delivery commands preserve this override.

```bash
scripts/setup.sh
scripts/check.sh
```

Setup consumes the locked manifests. The canonical check includes setup, Python lint/format/type/behavior checks, frontend lint/behavior/type/build checks, native auth plugin lint/type/behavior checks, Bash syntax, executable script modes, whitespace and the `CLAUDE.md -> AGENTS.md` bridge. Run focused component checks during implementation; use the canonical gate before delivery. Product tests exercise public behavior with temporary isolated state. Tooling is verified through successful real use, without a tooling coverage suite.

After building the agent image, run `FESNYNG_DOCKER_TESTS=true uv run --locked --project backend pytest backend/tests/test_docker_integration.py -q -s` to verify native configuration, managed skill removal and container-replacement persistence against real Docker. The two original tests report their isolated resource identities before setup. They stop their compute on success, ordinary failure and handled interruption; successful fixtures are removed, while failed diagnostic state, volumes and required checkpoint images remain recoverable. Inspect the reported disposition and investigate any teardown failure. Hard termination or host loss may bypass teardown and needs later exact-resource inspection. Real provider login and shared-credential verification use private installation state and are separate from credential-free tests. Follow the [preview stop/restart procedure](local-mvp.md#end-a-temporary-preview) when verifying a complete local installation; durable product agents are not temporary test fixtures.

### Dual-harness verification

Build the pinned image with the instructions in [agent-runtime/README.md](../agent-runtime/README.md), then exercise the native Codex transport and both switch directions:

```bash
FESNYNG_CODEX_DOCKER_TESTS=true \
  uv run --locked --project backend pytest \
  backend/tests/test_codex_docker_integration.py \
  backend/tests/test_harness_switch_docker.py -q -s
```

These tests default to `fesnyng-agent:local`; set `FESNYNG_CODEX_TEST_IMAGE` to an already built image to isolate this proof from another installation. They use no provider credentials. The switching proof creates fresh native threads, retains a workspace file across replacements, verifies snapshot reads with the original runtime replaced, and rejects old-thread writes after switching back. Successful runs remove their own containers, volumes and state. On ordinary failure, timeout, or handled interruption, each proof waits for executor-backed Docker work to settle, stops only its reported compute, and retains diagnostic state; follow the repository cleanup rules before stopping or removing anything shared. Hard termination or host loss may bypass teardown and requires later exact-resource inspection.

Keep these evidence types separate when recording acceptance:

| Evidence | What it establishes |
| --- | --- |
| Canonical behavioral tests | Organization and account isolation, serialized refresh including rejection before expiry, scoped native approvals/questions, cancellation, peer receipts, admission races, capture rollback and restart recovery through deterministic native/provider doubles. |
| Credential-free Docker tests | Real pinned harness startup, native session/history protocols, both-direction replacement and storage preservation. They do not establish authenticated model execution or a live OAuth refresh. |
| Authenticated browser proof | Real model replies, streamed native tools/file diffs, memory access, reconnect, frozen history rendering and original URLs after switching. Use a private host profile and record only sanitized outcomes. |

The delivered proof in issue #89 distinguishes real OpenCode/Codex replies and native tool execution from simulated refresh, failure and cross-account cases. Do not describe a simulated rejection or refresh race as a live provider event. Stop verification once the relevant gates and acceptance evidence pass; later source changes require checks and review of the affected behavior.

### Projects and native thread identity

The [Projects specification](https://github.com/jdip/Fesnyng/issues/117) extends
organization navigation while retaining employee and native thread ownership.
Run the opt-in grouping proof against the pinned runtime image:

```bash
FESNYNG_PROJECT_DOCKER_TESTS=true uv run --locked --project backend pytest \
  backend/tests/test_project_docker_integration.py -q -s
```

It exercises the control-plane and host APIs with real OpenCode and Codex
containers. Project assignment, archive, restore and deletion must preserve the
native thread identity, workspace and readable history. HTTP transport between
the application services runs in process; native execution uses Docker. This is
credential-free evidence, not an authenticated model-reply test. Each case reports
its exact resources and uses the existing proof teardown owner: stop compute on
failure and retain diagnostics; remove successful isolated fixtures. Inspect the
reported disposition before claiming cleanup.

### Repository-backed workspace proof

See [thread workspace storage and Git authentication](workspaces.md) before
configuring a host. With the pinned image built, run the opt-in proof using a
temporary parent directory shared with the Docker daemon:

```bash
FESNYNG_WORKSPACE_DOCKER_TESTS=true \
FESNYNG_WORKSPACE_PROOF_ROOT=/absolute/docker-shared/proof-directory \
  uv run --locked --project backend pytest \
  backend/tests/test_repository_workspace_docker.py -q -s
```

The parent directory must already exist. Each case creates its own isolated
state beneath it and reports its container identity before launch. The proof uses
two fictional Git repositories served only on loopback inside the employee,
both native harnesses, real Git worktrees and control-plane/host API calls. It
checks selected checkouts, concurrent isolation, ordinary directories, failure
without fallback, receipt reuse, applicable native forks and container restart.
It also injects a lost response after real native creation and a temporary
discovery outage, then checks that recovery returns the same native identity
without another creation call. Codex also loses an initialization acknowledgement;
recovery must preserve the existing context item and leave the user conversation
empty. Restart checks run before any user turn or model execution.
It requires no provider credentials or external Git account. Successful fixtures
are removed; failed compute is stopped and its diagnostic state retained through
the existing Docker proof lifecycle owner.

### Workspace lifecycle proof

With the pinned image built, run the credential-free lifecycle proof for both
native harnesses:

```bash
FESNYNG_WORKSPACE_LIFECYCLE_DOCKER_TESTS=true \
FESNYNG_WORKSPACE_LIFECYCLE_PROOF_ROOT=/absolute/docker-shared/proof-directory \
  uv run --locked --project backend pytest \
  backend/tests/test_workspace_lifecycle_docker.py -q -s
```

Create the Docker-shared parent directory first. The proof exercises inspection,
stale and unsafe cleanup refusals, explicit discard, removal, retained native
history and same-thread replacement through the control-plane and host APIs.
It also checks archive, restart and permanently frozen history behavior. It uses
the existing isolated Docker proof lifecycle: stop failed compute and retain
diagnostics; remove successful fixtures. This is native protocol evidence, not
authenticated model execution.

### External Docker resource proof

See [organization Docker access](docker-resources.md) for the dedicated-engine
requirement. The opt-in `backend/tests/test_docker_resources_docker.py` proof runs
on the organization work host with its local Docker socket and a freshly built
pinned runtime image. It exercises Compose from an employee's mapped workspace,
relative bind mounts, shared thread associations and explicit resource operations
through the control-plane/host boundary. No model credentials are required.
Do not use an engine containing other organizations' data. The proof reports exact
identities, removes successful isolated fixtures, and stops failed compute while
retaining diagnostics.

The same proof registers real applications inside the employee and its Compose
sibling, verifies their HTTP responses, shared service associations, stopped and
missing target status, and metadata-only unregister. On the dedicated private
Linux proving host, `FESNYNG_SERVICES_TAILSCALE_PROOF=true` additionally exercises
an actual host Tailscale Serve route through Fesnyng's service APIs. It requires
an already authenticated HTTPS-capable host Tailscale installation, noninteractive
`sudo -n tailscale`, no existing Serve configuration, and free ports 18081/18443.
It creates one labeled loopback proxy, removes only its exact Serve route, and
uses the same proof cleanup boundary. It never enables Funnel. These HTTP checks
originate on the work host; the independent external-client restart matrix in the
[private service deployment guide](private-services.md) verifies LAN denial and
tailnet ingress separately. Neither is evidence of public-Internet testing.

## Run locally

Run the independent backend commands documented in [backend/README.md](../backend/README.md). The control plane normally listens on `127.0.0.1:8000`; each host uses a separate port and state directory. The host is independently runnable and does not require control-plane reachability for startup. Local SQLite state, credentials and runtime files stay outside Git.

In another terminal:

```bash
npm run dev --prefix frontend
```

Vite serves the frontend and proxies `/api` to the Python control plane. Set `FESNYNG_CONTROL_PLANE_URL` when launching Vite to use a different local control-plane URL. The browser never receives a host credential from frontend configuration. Vite is a development server; follow the [local retained-MVP guide](local-mvp.md) for the service topology and operational recovery sequence. See issue #18 for live acceptance and delivery evidence.

Inspect rendered behavior in the browser after visual changes. Verify both backend processes through their actual `/health` endpoints and confirm each service's instance identity survives restart. Use distinct host state directories to prove independent identity. Conversation UI work uses maintained assistant-ui Thread and companion components under the approved specification.

## Delivery and dependency upkeep

Use [the PR-to-test runbook](workflows/pr-to-test.md) and canonical script for review, merge and verification of the committed tree. Application checks now run as part of that script's local gate. Main promotion remains separate.

During normal discovery, use `dependabot-upkeep` to read actionable alerts. Preserve alert-only protection and the operator-confirmed rules; empty alert lists do not prove hosted settings. Dependency changes update the owning manifest and lockfile together.
