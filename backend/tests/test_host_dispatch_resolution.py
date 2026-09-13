import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
import pytest

from fesnyng_backend import control_dispatch_routes
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app as create_control_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore, Submission
from fesnyng_backend.host_dispatch_resolution import (
    DispatchResolutionService,
    HostDispatchResolution,
)
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


class Native:
    def __init__(self):
        self.busy = False
        self.running_tool = False
        self.requests: list[str] = []
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, agent_id: str) -> asyncio.Lock:
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        self.requests.append(path)
        if path == "/session/status":
            return {"ses_primary": {"type": "busy"}} if self.busy else {}
        if path == "/session/ses_primary/message":
            state = "running" if self.running_tool else "completed"
            return [{"parts": [{"type": "tool", "state": {"status": state}}]}]
        raise AssertionError(path)


def _system(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        state_directory=tmp_path / "state",
        database_path=tmp_path / "state/host.sqlite3",
    )
    host = HostStore(settings)
    host.initialize()
    organization_id, agent_id = str(uuid4()), str(uuid4())
    host.bind_organization(organization_id, "test organization binding with enough characters")
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Resolution agent",
    )
    host.stage_agent(envelope)
    host.mark_applied(envelope)
    host.save_session(organization_id, agent_id, "ses_primary", "/workspace/primary", "Primary")
    dispatches = DispatchStore(host)
    dispatches.initialize()
    native = Native()
    dispatcher = Dispatcher(dispatches, native)
    resolver = DispatchResolutionService(host, dispatches, dispatcher, native)
    author = Actor(kind="human", id=uuid4(), name="Owner")
    receipt = dispatches.enqueue(
        organization_id,
        agent_id,
        "ses_primary",
        Submission(id=uuid4(), text="Inspect the native result"),
        author,
    )
    dispatches.change(
        receipt,
        "uncertain",
        message_id="msg_unknown",
        error="Native transport interrupted",
    )
    return organization_id, agent_id, dispatches, dispatcher, native, resolver, author, receipt


def test_operator_resolution_is_durable_idempotent_and_preserves_unknown_native_outcome(tmp_path):
    org, agent, dispatches, _, native, resolver, author, receipt = _system(tmp_path)
    resolution = HostDispatchResolution(
        operation_id=uuid4(),
        outcome="failed",
        evidence="Reviewed the retained native session and no completion was recorded.",
        author=author,
    )

    result = asyncio.run(
        resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution)
    )

    assert result["state"] == "failed"
    assert result["native_message_id"] == "msg_unknown"
    assert result["receipt_validated"] is False
    assert result["outcome"]["kind"] == "operator_resolution"
    assert result["outcome"]["author"]["id"] == str(author.id)
    assert result["outcome"]["unknown_native_outcome"] == {
        "state": "uncertain",
        "native_message_id": "msg_unknown",
        "receipt_validated": False,
        "outcome": None,
        "error": "Native transport interrupted",
    }
    assert all("prompt" not in path and "command" not in path for path in native.requests)
    restored = DispatchStore(dispatches.host).get(org, agent, receipt["id"])
    assert restored == result
    assert (
        asyncio.run(resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution))
        == result
    )
    with pytest.raises(ValueError, match="identity conflict"):
        asyncio.run(
            resolver.resolve(
                org,
                agent,
                "ses_primary",
                UUID(receipt["id"]),
                resolution.model_copy(update={"evidence": "Different evidence"}),
            )
        )


def test_operator_resolution_requires_idle_no_local_task_and_no_running_tools(tmp_path):
    org, agent, _, dispatcher, native, resolver, author, receipt = _system(tmp_path)
    resolution = HostDispatchResolution(
        operation_id=uuid4(), outcome="completed", evidence="Verified externally.", author=author
    )

    native.busy = True
    with pytest.raises(ValueError, match="not idle"):
        asyncio.run(resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution))
    native.busy = False
    native.running_tool = True
    with pytest.raises(ValueError, match="running tool"):
        asyncio.run(resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution))
    native.running_tool = False

    async def check_local_task():
        waiting = asyncio.create_task(asyncio.Event().wait())
        dispatcher.tasks[receipt["id"]] = waiting
        try:
            with pytest.raises(ValueError, match="local submission task"):
                await resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(check_local_task())


def test_resolution_routes_scope_target_and_derive_the_control_plane_human_actor(
    organization, monkeypatch, tmp_path
):
    settings, control, owner, org, agents, host_id = organization
    token = secrets.token_urlsafe(32)
    agents.set_host_credential(org.id, host_id, token)
    agent = agents.create_agent(org.id, owner.id, {"name": "Resolution agent", "host_id": host_id})
    host_app = create_host_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "host-state",
        )
    )
    host_store = host_app.state.host_store
    host_store.bind_organization(org.id, token)
    envelope = HostAgentConfiguration(
        host_id=host_store.instance_id,
        organization_id=org.id,
        agent_id=agent["id"],
        version=1,
        name=agent["name"],
    )
    host_store.stage_agent(envelope)
    host_store.mark_applied(envelope)
    host_store.save_session(org.id, agent["id"], "ses_primary", "/workspace/primary", "Primary")
    native = Native()
    host_app.state.dispatch_resolution = DispatchResolutionService(
        host_store, host_app.state.dispatch_store, host_app.state.dispatcher, native
    )
    author = Actor(kind="human", id=owner.id, name="Owner")
    receipt = host_app.state.dispatch_store.enqueue(
        org.id, agent["id"], "ses_primary", Submission(id=uuid4(), text="Inspect"), author
    )
    host_app.state.dispatch_store.change(receipt, "unresolved", message_id="msg_unknown")
    control.add_member(
        org.id, "member", "Member", "a separate member password", "member", actor_id=owner.id
    )
    control_app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    client = HostClient(agents, transport=httpx.ASGITransport(app=host_app))
    monkeypatch.setattr(control_dispatch_routes, "host_client", lambda _: client)

    async def check():
        transport = httpx.ASGITransport(app=control_app)
        headers = {"Origin": ORIGIN}
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers=headers) as http:
            login = await http.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            assert login.status_code == 200
            http.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            path = f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_primary/dispatches/{receipt['id']}/resolve"
            forged = await http.post(
                path,
                json={
                    "operation_id": str(uuid4()),
                    "outcome": "failed",
                    "evidence": "Reviewed.",
                    "author": {"kind": "human", "id": str(uuid4()), "name": "Forged"},
                },
            )
            assert forged.status_code == 422
            missing_csrf = await http.post(
                path,
                headers={"X-CSRF-Token": ""},
                json={"operation_id": str(uuid4()), "outcome": "failed", "evidence": "Reviewed."},
            )
            assert missing_csrf.status_code == 403
            resolved = await http.post(
                path,
                json={"operation_id": str(uuid4()), "outcome": "failed", "evidence": "Reviewed."},
            )
            assert resolved.status_code == 200
            assert resolved.json()["outcome"]["author"]["id"] == str(owner.id)
            wrong_session = await http.post(
                path.replace("ses_primary", "ses_other"),
                json={"operation_id": str(uuid4()), "outcome": "failed", "evidence": "Reviewed."},
            )
            assert wrong_session.status_code == 404

    asyncio.run(check())


def test_resolution_waits_for_a_running_native_child_even_when_parent_tool_ended(tmp_path):
    org, agent, dispatches, dispatcher, _, _, author, receipt = _system(tmp_path)

    class ChildNative(Native):
        def __init__(self):
            super().__init__()
            self.child_busy = True

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {"ses_child": {"type": "busy"}} if self.child_busy else {}
            if path == "/session/ses_primary/message":
                return [
                    {
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "task",
                                "state": {
                                    "status": "error",
                                    "metadata": {"sessionId": "ses_child"},
                                },
                            }
                        ]
                    }
                ]
            if path == "/session/ses_child":
                return {"id": "ses_child", "parentID": "ses_primary", "directory": directory}
            if path == "/session/ses_child/message":
                return [
                    {
                        "info": {
                            "id": "msg_child",
                            "role": "assistant",
                            "sessionID": "ses_child",
                            "time": {"completed": 1},
                        },
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "bash",
                                "state": {"status": "error", "metadata": {"interrupted": True}},
                            }
                        ],
                    }
                ]
            raise AssertionError(path)

    native = ChildNative()
    resolver = DispatchResolutionService(dispatches.host, dispatches, dispatcher, native)
    resolution = HostDispatchResolution(
        operation_id=uuid4(),
        outcome="failed",
        evidence="Inspected child side effects and verified the stopped operation.",
        author=author.model_copy(update={"kind": "agent"}),
    )
    with pytest.raises(ValueError, match="descendants"):
        asyncio.run(resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution))
    native.child_busy = False
    result = asyncio.run(
        resolver.resolve(org, agent, "ses_primary", UUID(receipt["id"]), resolution)
    )
    assert result["state"] == "failed"
    assert result["outcome"]["author"]["kind"] == "agent"
