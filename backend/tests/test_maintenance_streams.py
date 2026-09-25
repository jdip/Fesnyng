import asyncio
import secrets
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_codex_routes import events as codex_events
from fesnyng_backend.host_maintenance import MaintenanceBusy
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_workspace_routes import events as opencode_events
from fesnyng_backend.settings import ServiceSettings


@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_idle_stream_drains_only_after_verified_maintenance(tmp_path, monkeypatch, harness):
    app = create_app(
        ServiceSettings(
            service="agent-host", state_directory=tmp_path, database_path=tmp_path / "host.sqlite3"
        )
    )
    store, runtime, guard = (
        app.state.host_store,
        app.state.host_runtime,
        app.state.maintenance_guard,
    )
    org, agent, token = str(uuid4()), str(uuid4()), secrets.token_urlsafe(32)
    store.bind_organization(org, token)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Stream proof",
        configuration=AgentConfiguration(runtime_type=harness),
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_idle", "/workspace/idle", "Idle", runtime_type=harness)
    guard.interactions = None
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/event",
            "app": app,
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
    )
    route = codex_events if harness == "codex" else opencode_events

    async def check():
        started, closed, decision = asyncio.Event(), asyncio.Event(), asyncio.Event()
        allowed = False

        async def quiet(*args):
            await decision.wait()
            if not allowed:
                raise RuntimeUnavailable("Native work is busy or unknown")

        async def idle(*args):
            started.set()
            try:
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()

        @asynccontextmanager
        async def native_stream(*args):
            iterator = idle()
            try:
                yield iterator
            finally:
                await iterator.aclose()

        monkeypatch.setattr(runtime, "assert_quiet", quiet)
        monkeypatch.setattr(runtime, "event_stream", native_stream)
        monkeypatch.setattr(runtime.codex, "events", idle)
        response = await route(request, UUID(org), UUID(agent))
        iterator = response.body_iterator
        pending = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(started.wait(), 1)
        acquiring = asyncio.create_task(guard.acquire())
        await asyncio.sleep(0.3)
        assert store.maintenance_status() == {"state": "closed"}
        assert not pending.done() and not closed.is_set()
        decision.set()
        with pytest.raises(MaintenanceBusy):
            await acquiring
        assert store.maintenance_status() == {"state": "open"}
        assert not pending.done() and not closed.is_set()
        allowed = True
        assert await guard.acquire() == {"state": "closed"}
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 1)
        assert closed.is_set()
        await iterator.aclose()
        with pytest.raises(HTTPException) as blocked:
            await route(request, UUID(org), UUID(agent))
        assert blocked.value.status_code == 409
        await guard.release()
        started.clear()
        closed.clear()
        response = await route(request, UUID(org), UUID(agent))
        iterator = response.body_iterator
        pending = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(started.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await iterator.aclose()
        assert closed.is_set()

    asyncio.run(check())
