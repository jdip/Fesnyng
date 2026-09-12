# Fesnyng backends

This package contains independently runnable FastAPI control-plane and agent-host
services. Each service owns a separately configured SQLite file, state directory,
and durable UUID identity. The only current public endpoint is `GET /health`; product
management APIs are added by the following authorized tickets.

## Local commands

From the repository root, use the pinned `uv` declared in [`.tool-versions`](.tool-versions):

```bash
uv sync --locked --project backend
uv run --project backend ruff check backend
uv run --project backend ruff format --check backend
uv run --project backend ty check --project backend
(cd backend && uv run pytest)
```

Run each service independently. Set distinct state paths when running both locally:

```bash
FESNYNG_CONTROL_PLANE_STATE_DIRECTORY=./.fesnyng/control-plane \
  uv run --project backend uvicorn fesnyng_backend.control_plane:create_app --factory --port 8000
FESNYNG_AGENT_HOST_STATE_DIRECTORY=./.fesnyng/agent-host \
  uv run --project backend uvicorn fesnyng_backend.agent_host:create_app --factory --port 8001
```

An optional `FESNYNG_CONTROL_PLANE_INSTANCE_ID` or `FESNYNG_AGENT_HOST_INSTANCE_ID`
sets the initial UUID identity; later starts validate it against the durable record.
