# Development and verification

Fesnyng has separate Python/FastAPI control-plane and agent-host services in `backend/`, and a React/TypeScript/Vite application in `frontend/`. The Python package shares contracts while each service owns its SQLite database and schema. Product code and contracts are **Application Code**; setup and delivery scripts are **Tooling**.

The foundation exposes service health and durable instance identity. Authentication, organization management, agent execution and the assistant-ui conversation workspace are subsequent children of the [approved MVP specification](https://github.com/jdip/Fesnyng/issues/10). A running foundation is not the complete MVP.

## Install and check

Use Node 22.12+ with npm and uv with Python 3.12 or newer. Dependencies belong in `backend/pyproject.toml` and `frontend/package.json`, with their committed lockfiles. Follow `backend/README.md` for Python runtime details. If uv is outside PATH, set `FESNYNG_UV` to its absolute executable path; the setup and delivery commands preserve this override.

```bash
scripts/setup.sh
scripts/check.sh
```

Setup consumes the locked manifests. The canonical check includes setup, Python lint/format/type/behavior checks, frontend lint/behavior/type/build checks, Bash syntax, executable script modes, whitespace and the `CLAUDE.md -> AGENTS.md` bridge. Run focused component checks during implementation; use the canonical gate before delivery. Product tests exercise public behavior with temporary isolated state. Tooling is verified through successful real use, without a tooling coverage suite.

## Run locally

Run the independent backend commands documented in [backend/README.md](../backend/README.md). The control plane normally listens on `127.0.0.1:8000`; each host uses a separate port and state directory. The host is independently runnable and does not require control-plane reachability for startup. Local SQLite state, credentials and runtime files stay outside Git.

In another terminal:

```bash
npm run dev --prefix frontend
```

Vite serves the frontend and proxies `/api` to the Python control plane. Set `FESNYNG_CONTROL_PLANE_URL` when launching Vite to use a different local control-plane URL. The browser never receives a host credential from frontend configuration. Vite is a development server; retained full-system installation and runtime instructions are completed by the final MVP delivery ticket.

Inspect rendered behavior in the browser after visual changes. Verify both backend processes through their actual `/health` endpoints and confirm each service's instance identity survives restart. Use distinct host state directories to prove independent identity. Conversation UI work uses maintained assistant-ui Thread and companion components under the approved specification.

## Delivery and dependency upkeep

Use [the PR-to-test runbook](workflows/pr-to-test.md) and canonical script for review, merge and verification of the committed tree. Application checks now run as part of that script's local gate. Main promotion remains separate.

During normal discovery, use `dependabot-upkeep` to read actionable alerts. Preserve alert-only protection and the operator-confirmed rules; empty alert lists do not prove hosted settings. Dependency changes update the owning manifest and lockfile together.
