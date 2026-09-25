import asyncio
import secrets
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
import pytest

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.agent_lifecycle import AgentLifecycle, HostLifecycleRequest
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_dispatch import DispatchStore, Submission
from fesnyng_backend.host_maintenance import MaintenanceGuard
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.settings import ServiceSettings


def test_local_maintenance_guard_requires_its_private_token_and_survives_restart(
    tmp_path: Path, monkeypatch
):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("FESNYNG_MAINTENANCE_TOKEN", token)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )

    async def check():
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8000))
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            assert (await client.get("/maintenance")).status_code == 401
            assert (
                await client.post("/maintenance/acquire", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            acquired = await client.post(
                "/maintenance/acquire", headers={"Authorization": f"Bearer {token}"}
            )
            assert acquired.status_code == 200
            assert acquired.json() == {"state": "closed"}

        restarted = create_app(settings)
        restarted_transport = httpx.ASGITransport(app=restarted, client=("127.0.0.1", 8000))
        async with httpx.AsyncClient(
            transport=restarted_transport,
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            assert (await client.get("/maintenance")).json() == {"state": "closed"}
            released = await client.post("/maintenance/release")
            assert released.status_code == 200
            assert released.json() == {"state": "open"}
            assert (await client.post("/maintenance/release")).json() == {"state": "open"}

    asyncio.run(check())


def test_acquire_closes_admission_before_waiting_for_a_runtime_lock(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Locked agent",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_maintenance", "/workspace", "Maintenance")
    dispatches = DispatchStore(store)
    dispatches.initialize()
    lock = asyncio.Lock()

    class Runtime:
        def lock(self, _agent: str):
            return lock

        async def assert_quiet(self, _org: str, _agent: str) -> None:
            return None

    guard = MaintenanceGuard(store, Runtime())

    async def check():
        await lock.acquire()
        acquisition = asyncio.create_task(guard.acquire())
        await asyncio.sleep(0)
        assert store.maintenance_status() == {"state": "closed"}
        with pytest.raises(ValueError, match="maintenance"):
            dispatches.enqueue(
                org,
                agent,
                "ses_maintenance",
                Submission(id=uuid4(), text="Must not enter after maintenance closes"),
                Actor(kind="human", id=uuid4(), name="Owner"),
            )
        lock.release()
        assert await acquisition == {"state": "closed"}

    asyncio.run(check())


def test_maintenance_acquire_defers_a_native_pending_interaction(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Interactive",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)

    class Runtime:
        def lock(self, _agent: str):
            return asyncio.Lock()

        async def assert_quiet(self, _org: str, _agent: str) -> None:
            return None

    class Interactions:
        async def maintenance_pending(self, seen_org: str, seen_agent: str) -> bool:
            assert (seen_org, seen_agent) == (org, agent)
            return True

    guard = MaintenanceGuard(store, Runtime())
    guard.interactions = Interactions()

    async def check():
        with pytest.raises(Exception, match="pending interaction"):
            await guard.acquire()
        assert store.maintenance_status() == {"state": "open"}

    asyncio.run(check())


def test_maintenance_acquire_reopens_admission_when_delivery_is_queued(tmp_path: Path, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("FESNYNG_MAINTENANCE_TOKEN", token)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    store.bind_organization(org, binding)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Queued"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_queued", "/workspace", "Queued")
    app.state.dispatch_store.enqueue(
        org,
        agent,
        "ses_queued",
        Submission(id=uuid4(), text="Already admitted before maintenance"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )

    async def check():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = await client.post("/maintenance/acquire")
            assert response.status_code == 409
            assert response.json() == {"state": "open", "reason": "pending delivery"}

    asyncio.run(check())


def test_interrupted_maintenance_acquire_keeps_admission_closed(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Interrupted",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    waiting = asyncio.Event()

    class Runtime:
        def lock(self, _agent: str):
            return asyncio.Lock()

        async def assert_quiet(self, _org: str, _agent: str) -> None:
            await waiting.wait()

    guard = MaintenanceGuard(store, Runtime())

    async def check():
        acquisition = asyncio.create_task(guard.acquire())
        await asyncio.sleep(0)
        acquisition.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        assert store.maintenance_status() == {"state": "closed"}

    asyncio.run(check())


def test_runtime_quiet_check_rejects_malformed_opencode_status_as_unknown(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Malformed"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)

    class MalformedRuntime(DockerRuntime):
        async def inspect(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
            return {"state": {"Running": True}}

        async def request(
            self,
            organization_id: str,
            agent_id: str,
            path: str,
            *,
            method: str = "GET",
            body: Any = None,
            directory: str | None = None,
        ) -> Any:
            return {"ses_malformed": {"type": []}}

    runtime = MalformedRuntime(store, "http://credentials", "test-image")

    with pytest.raises(RuntimeUnavailable, match="status response is invalid"):
        asyncio.run(runtime.assert_quiet(org, agent))


def test_lifecycle_waiting_on_runtime_inspection_cannot_transition_after_acquire(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Raced"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    inspection_started = asyncio.Event()
    release_inspection = asyncio.Event()

    class Runtime:
        def __init__(self):
            self.locks: dict[str, asyncio.Lock] = {}
            self.inspections = 0

        def lock(self, agent_id: str):
            return self.locks.setdefault(agent_id, asyncio.Lock())

        async def inspect(self, organization_id: str, agent_id: str) -> dict[str, Any] | None:
            self.inspections += 1
            if self.inspections == 1:
                inspection_started.set()
                await release_inspection.wait()

        async def assert_quiet(self, organization_id: str, agent_id: str) -> None:
            return None

        async def image_is_current(self, organization_id: str, agent_id: str) -> bool:
            raise AssertionError("lifecycle must not start")

        async def start(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

        async def stop(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

        async def restart(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

        async def rebuild(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

    class Dispatcher:
        async def agent_active(self, organization_id: str, agent_id: str) -> bool:
            return False

        async def agent_activity(self, organization_id: str, agent_id: str) -> str:
            return ""

        async def quiesce_agent(self, organization_id: str, agent_id: str, author: Actor) -> None:
            raise AssertionError("no active work")

        async def reconcile_agent_effects(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

        def agent_effects_settled(self, organization_id: str, agent_id: str) -> bool:
            return True

    class Configuration:
        async def apply_agent(self, organization_id: str, agent_id: str) -> str:
            raise AssertionError("lifecycle must not start")

        async def refresh_runtime(self, organization_id: str, agent_id: str) -> None:
            raise AssertionError("lifecycle must not start")

    runtime = Runtime()
    lifecycle = AgentLifecycle(store, runtime, Dispatcher(), Configuration())
    guard = MaintenanceGuard(store, runtime)

    async def check():
        request = HostLifecycleRequest(
            action="start", author=Actor(kind="human", id=uuid4(), name="Owner")
        )
        operation = asyncio.create_task(lifecycle.perform(org, agent, request))
        await inspection_started.wait()
        assert await guard.acquire() == {"state": "closed"}
        release_inspection.set()
        with pytest.raises(ValueError, match="maintenance"):
            await operation
        assert store.agent_status(org, agent)["lifecycle_state"] == "running"

    asyncio.run(check())


def test_maintenance_closure_blocks_harness_switch_transition(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Switch"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    assert store.close_maintenance_admission()

    with pytest.raises(ValueError, match="maintenance"):
        store.begin_harness_switch(org, agent, 1, "codex")

    assert store.agent(org, agent)["switch_state"] is None


@pytest.mark.parametrize("runtime_type", ["opencode", "codex"])
def test_maintenance_acquire_defers_busy_or_unknown_native_work_for_each_harness(
    tmp_path: Path, monkeypatch, runtime_type: Literal["opencode", "codex"]
):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("FESNYNG_MAINTENANCE_TOKEN", token)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    organization_id, agent_id = str(uuid4()), str(uuid4())
    app.state.host_store.bind_organization(organization_id, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=app.state.host_store.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Maintenance agent",
        configuration=AgentConfiguration(runtime_type=runtime_type),
    )
    app.state.host_store.stage_agent(envelope)
    app.state.host_store.mark_applied(envelope)

    class UnavailableRuntime:
        async def assert_quiet(self, org: str, agent: str) -> None:
            assert (org, agent) == (organization_id, agent_id)
            raise RuntimeUnavailable("native status is unavailable")

        def lock(self, _agent: str):
            return asyncio.Lock()

    app.state.maintenance_guard.runtime = UnavailableRuntime()

    async def check():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = await client.post("/maintenance/acquire")
            assert response.status_code == 409
            assert response.json() == {
                "state": "open",
                "reason": "native activity is busy or unavailable",
            }

    asyncio.run(check())


def test_runtime_rollout_is_private_requires_maintenance_and_keeps_failure_closed(
    tmp_path, monkeypatch
):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("FESNYNG_MAINTENANCE_TOKEN", token)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    app = create_app(settings)
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Rollout"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)

    class Runtime:
        def lock(self, agent_id):
            return asyncio.Lock()

        async def assert_quiet(self, organization_id, agent_id):
            return None

    class Lifecycle:
        fail = False
        calls = 0

        async def rollout_runtime(self, organization_id, agent_id):
            assert store.maintenance_status() == {"state": "closed"}
            self.calls += 1
            if self.fail:
                raise RuntimeUnavailable("image replacement failed")
            return True

    lifecycle = Lifecycle()
    app.state.maintenance_guard.runtime = Runtime()
    app.state.maintenance_guard.lifecycle = lifecycle

    async def check():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 8000))
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            assert (await client.post("/maintenance/rollout")).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            assert (await client.post("/maintenance/rollout")).status_code == 409
            assert lifecycle.calls == 0
            assert (await client.post("/maintenance/acquire")).status_code == 200
            result = await client.post("/maintenance/rollout")
            assert result.status_code == 200
            assert result.json() == {"state": "closed", "updated": 1}
            lifecycle.fail = True
            assert (await client.post("/maintenance/rollout")).status_code == 503
            assert (await client.get("/maintenance")).json() == {"state": "closed"}
        remote = httpx.ASGITransport(app=app, client=("192.0.2.1", 8000))
        async with httpx.AsyncClient(transport=remote, base_url="http://host") as client:
            assert (
                await client.post(
                    "/maintenance/rollout", headers={"Authorization": f"Bearer {token}"}
                )
            ).status_code == 403
        assert lifecycle.calls == 2

    asyncio.run(check())


@pytest.mark.parametrize("runtime_state", ["checkpointing", "replacing"])
def test_maintenance_defers_interrupted_checkpoint_replacement(tmp_path, runtime_state):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    store = app.state.host_store
    org, agent = str(uuid4()), str(uuid4())
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Checkpoint"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.set_runtime_state(org, agent, runtime_state)
    assert store.maintenance_pending_reason() == "configuration or lifecycle transition"
