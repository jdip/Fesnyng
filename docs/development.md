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

After building the agent image, run `FESNYNG_DOCKER_TESTS=true uv run --locked --project backend pytest backend/tests/test_docker_integration.py -q` to verify native configuration, managed skill removal and container-replacement persistence against real Docker. This opt-in check creates isolated containers and volumes, removes its own successfully verified resources, and retains failed resources for diagnosis. Real provider login and shared-credential verification use private installation state and are separate from credential-free tests.

### Dual-harness verification

Build the pinned image with the instructions in [agent-runtime/README.md](../agent-runtime/README.md), then exercise the native Codex transport and both switch directions:

```bash
FESNYNG_CODEX_DOCKER_TESTS=true \
  uv run --locked --project backend pytest \
  backend/tests/test_codex_docker_integration.py \
  backend/tests/test_harness_switch_docker.py -q -s
```

These tests default to `fesnyng-agent:local`; set `FESNYNG_CODEX_TEST_IMAGE` to an already built image to isolate this proof from another installation. They use no provider credentials. The switching proof creates fresh native threads, retains a workspace file across replacements, verifies snapshot reads with the original runtime replaced, and rejects old-thread writes after switching back. Successful runs remove their own containers, volumes and state. On failure, inspect the reported exact resources and retain diagnostic state; follow the repository cleanup rules before stopping or removing anything shared.

Keep these evidence types separate when recording acceptance:

| Evidence | What it establishes |
| --- | --- |
| Canonical behavioral tests | Organization and account isolation, serialized refresh including rejection before expiry, scoped native approvals/questions, cancellation, peer receipts, admission races, capture rollback and restart recovery through deterministic native/provider doubles. |
| Credential-free Docker tests | Real pinned harness startup, native session/history protocols, both-direction replacement and storage preservation. They do not establish authenticated model execution or a live OAuth refresh. |
| Authenticated browser proof | Real model replies, streamed native tools/file diffs, memory access, reconnect, frozen history rendering and original URLs after switching. Use a private host profile and record only sanitized outcomes. |

The delivered proof in issue #89 distinguishes real OpenCode/Codex replies and native tool execution from simulated refresh, failure and cross-account cases. Do not describe a simulated rejection or refresh race as a live provider event. Stop verification once the relevant gates and acceptance evidence pass; later source changes require checks and review of the affected behavior.

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
