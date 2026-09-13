import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from fesnyng_backend import control_workspace_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace_routes import router as host_workspace_router
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://workspace.example"


def test_workspace_facade_scopes_native_reads_and_preserves_native_error_shape(
    organization, monkeypatch
):
    settings, _, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    calls: list[httpx.Request] = []

    def native(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/provider"):
            return httpx.Response(
                422,
                json={"name": "ModelNotFoundError", "data": {"model": "missing"}},
            )
        if request.url.path.endswith("/file/content"):
            assert dict(request.url.params) == {"sessionID": "ses_main", "path": "notes/result.txt"}
            return httpx.Response(200, content=b"artifact")
        return httpx.Response(200, json=[{"id": "ses_main", "title": "Main"}])

    monkeypatch.setattr(
        control_workspace_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(native)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            assert (
                await client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/opencode/experimental/session"
                )
            ).status_code == 401
            login = await client.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            sessions = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/experimental/session"
            )
            assert sessions.json() == [{"id": "ses_main", "title": "Main"}]
            listed = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/session"
            )
            assert listed.json() == [{"id": "ses_main", "title": "Main"}]
            error = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/provider"
            )
            assert error.status_code == 422
            assert error.json() == {"name": "ModelNotFoundError", "data": {"model": "missing"}}
            artifact = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/file/content",
                params={"sessionID": "ses_main", "path": "notes/result.txt"},
            )
            assert artifact.content == b"artifact"
            assert (
                await client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/opencode/file/content",
                    params={
                        "sessionID": "ses_main",
                        "path": "../outside",
                        "directory": "/workspace",
                    },
                )
            ).status_code == 422
            other = str(uuid4())
            assert (
                await client.get(
                    f"/organizations/{other}/agents/{agent['id']}/opencode/experimental/session"
                )
            ).status_code == 404
            assert (
                await client.get(f"/organizations/{other}/agents/{agent['id']}/opencode/event")
            ).status_code == 404
            assert (
                await client.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/opencode/session/ses_main/summarize"
                )
            ).status_code == 404

    asyncio.run(exercise())
    assert len(calls) == 4
    assert all(request.headers["authorization"].startswith("Bearer ") for request in calls)


def test_workspace_facade_forwards_attributed_prompt_with_stable_idempotency_and_sse(
    organization, monkeypatch
):
    settings, control, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    requests: list[httpx.Request] = []

    def native(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: session.updated\ndata: {"id":"ses_main"}\n\n',
            )
        return httpx.Response(202, json={"id": "delivery", "state": "queued"})

    monkeypatch.setattr(
        control_workspace_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(native)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            events_path = f"/organizations/{org.id}/agents/{agent['id']}/opencode/event"
            assert (await client.get(events_path)).status_code == 401
            login = await client.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            path = f"/organizations/{org.id}/agents/{agent['id']}/opencode/session/ses_main/prompt_async"
            denied = await client.post(path, json={"parts": [{"type": "text"}]})
            assert denied.status_code == 403
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            prompted = await client.post(
                path,
                json={"parts": [{"type": "text", "text": "Review"}]},
            )
            assert prompted.status_code == 202
            UUID(prompted.headers["Idempotency-Key"])
            question = await client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/question/question_1/reply",
                json={"answers": [["Yes"]]},
            )
            assert question.status_code == 202
            UUID(question.headers["Idempotency-Key"])
            events = await client.get(events_path)
            assert events.headers["content-type"].startswith("text/event-stream")
            assert events.content == b'event: session.updated\ndata: {"id":"ses_main"}\n\n'
            control.revoke_session(client.cookies["fesnyng_session"])
            assert (await client.get(events_path)).status_code == 401

    asyncio.run(exercise())
    assert len(requests) == 3
    prompt = requests[0]
    UUID(prompt.headers["idempotency-key"])
    assert json.loads(prompt.headers["x-fesnyng-actor"]) == {
        "kind": "human",
        "id": owner.id,
        "name": owner.display_name,
        "session_id": None,
    }
    assert "author" not in json.loads(prompt.content)
    interaction = requests[1]
    operation_id = interaction.headers["idempotency-key"]
    UUID(operation_id)
    assert json.loads(interaction.headers["x-fesnyng-actor"])["id"] == owner.id
    assert json.loads(interaction.content) == {"answers": [["Yes"]], "operation_id": operation_id}


def test_workspace_facade_reaches_the_real_host_router_with_agent_scope(
    organization, monkeypatch, tmp_path
):
    settings, _, owner, org, agents, _ = organization
    host_settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "host-state",
    )
    host_store = HostStore(host_settings)
    host_store.initialize()
    binding = secrets.token_urlsafe(32)
    host_store.bind_organization(org.id, binding)
    host_id = str(host_store.instance_id)
    agents.register_host(host_id, "Test host", "http://host.test", org.id)
    agents.set_host_credential(org.id, host_id, binding)
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    envelope = HostAgentConfiguration(
        host_id=host_store.instance_id,
        organization_id=org.id,
        agent_id=agent["id"],
        version=1,
        name="Workspace",
    )
    host_store.stage_agent(envelope)
    host_store.mark_applied(envelope)

    class Runtime:
        def lock(self, agent_id):
            return asyncio.Lock()

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            raise AssertionError((organization_id, agent_id, path, method, body, directory))

        async def create_session(self, _org, _agent, title, _workspace):
            return {"id": "ses_created", "title": title}

    host_app = FastAPI()
    host_app.state.host_store = host_store
    host_app.state.host_runtime = Runtime()
    host_app.state.dispatch_store = DispatchStore(host_store)
    host_app.state.dispatch_store.initialize()
    host_app.state.interactions = Interactions(host_store, host_app.state.host_runtime)
    host_app.state.interactions.initialize()
    host_app.include_router(host_workspace_router)
    monkeypatch.setattr(
        control_workspace_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.ASGITransport(host_app)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            login = await client.post(
                "/auth/login", json={"login": "owner", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            base = f"/organizations/{org.id}/agents/{agent['id']}/opencode"
            assert (await client.get(f"{base}/session")).json() == []
            created = await client.post(f"{base}/session", json={})
            assert created.status_code == 201
            assert created.json() == {"id": "ses_created", "title": "New thread"}

    asyncio.run(exercise())
