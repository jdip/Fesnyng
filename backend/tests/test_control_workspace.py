import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from fesnyng_backend import control_thread_pin_routes, control_workspace_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_dispatch import DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace_routes import router as host_workspace_router
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://workspace.example"


class BlockingEventResponse:
    """A host event source whose next event is released by the test."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_bytes(self):
        await self.release.wait()
        yield b'event: session.updated\ndata: {"id":"ses_main"}\n\n'

    async def aclose(self) -> None:
        self.closed = True


class BlockingHostStream:
    def __init__(self) -> None:
        self.response = BlockingEventResponse()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.response.aclose()


class EventHostClient:
    """The remote host boundary for a controlled, long-lived event response."""

    def __init__(self, agents, stream: BlockingHostStream, resource: str = "event") -> None:
        self.agents = agents
        self._stream = stream
        self.resource = resource

    async def stream(self, organization_id, host_id, path, *, headers=None, params=None):
        assert path.endswith(f"/opencode/{self.resource}")
        return self._stream


async def _open_event_route(app: FastAPI, path: str, token: str, query: bytes = b""):
    """Start the public HTTP stream without buffering its response body."""

    messages = []
    started = asyncio.Event()
    disconnected = asyncio.Event()
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            started.set()

    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": query,
                "headers": [(b"cookie", f"fesnyng_session={token}".encode())],
                "client": ("testclient", 50000),
                "server": ("workspace.example", 443),
            },
            receive,
            send,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    return task, messages


def _event_context(organization, monkeypatch, resource="event"):
    settings, _, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    store = app.state.control_store
    membership = store.add_member(
        org.id,
        "viewer",
        "Viewer",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    authenticated = store.login("viewer", "correct horse battery staple", 3600)
    assert authenticated is not None
    user, credentials = authenticated
    assert user.id == membership.user_id
    stream = BlockingHostStream()
    monkeypatch.setattr(
        control_workspace_routes, "host_client", lambda _: EventHostClient(agents, stream, resource)
    )
    return app, store, owner, org, agent, user, credentials.token, stream


def test_workspace_facade_scopes_native_reads_and_preserves_native_error_shape(
    organization, monkeypatch
):
    settings, _, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    calls: list[httpx.Request] = []

    def native(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/command"):
            return httpx.Response(200, json=[{"name": "fesnyng/review", "description": "review"}])
        if request.url.path.endswith("/provider"):
            return httpx.Response(
                422,
                json={"name": "ModelNotFoundError", "data": {"model": "missing"}},
            )
        if request.url.path.endswith("/file/content"):
            assert dict(request.url.params) == {"sessionID": "ses_main", "path": "notes/result.txt"}
            return httpx.Response(200, content=b"artifact")
        if request.url.path.endswith("/file/download"):
            assert dict(request.url.params) == {"sessionID": "ses_main", "path": "notes/result.txt"}
            return httpx.Response(
                200,
                content=b"  exact bytes\n\x00\xff\n",
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="result.txt"',
                },
            )
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
            download_path = f"/organizations/{org.id}/agents/{agent['id']}/opencode/file/download"
            file_params = {"sessionID": "ses_main", "path": "notes/result.txt"}
            assert (await client.get(download_path, params=file_params)).status_code == 401
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
            workflows = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/command"
            )
            assert workflows.status_code == 200
            assert workflows.json() == [{"name": "fesnyng/review", "description": "review"}]
            assert (
                await client.get(f"/organizations/{uuid4()}/agents/{agent['id']}/opencode/command")
            ).status_code == 404
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
            download = await client.get(download_path, params=file_params)
            assert download.content == b"  exact bytes\n\x00\xff\n"
            assert download.headers["content-type"] == "application/octet-stream"
            assert download.headers["content-disposition"] == 'attachment; filename="result.txt"'
            assert download.headers["cache-control"] == "no-store"
            assert (
                await client.get(
                    download_path, params={"sessionID": "ses_main", "path": "../outside"}
                )
            ).status_code == 422
            assert (
                await client.get(
                    f"/organizations/{uuid4()}/agents/{agent['id']}/opencode/file/download",
                    params=file_params,
                )
            ).status_code == 404
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
    assert len(calls) == 6
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


@pytest.mark.parametrize(("revocation", "expected_status"), [("membership", 404), ("session", 401)])
@pytest.mark.parametrize("resource", ["event", "file/download"])
def test_workspace_stream_drops_bytes_after_access_revocation(
    organization, monkeypatch, revocation, expected_status, resource
):
    app, store, owner, org, agent, user, token, stream = _event_context(
        organization, monkeypatch, resource
    )

    async def exercise():
        task, messages = await _open_event_route(
            app,
            f"/organizations/{org.id}/agents/{agent['id']}/opencode/{resource}",
            token,
            b"sessionID=ses_main&path=notes.txt" if resource == "file/download" else b"",
        )
        if revocation == "membership":
            store.remove_member(org.id, user.id, actor_id=owner.id)
        else:
            store.revoke_session(token)
        stream.response.release.set()
        with pytest.raises(RuntimeError) as error:
            await task
        assert isinstance(error.value.__cause__, HTTPException)
        assert error.value.__cause__.status_code == expected_status
        assert [message for message in messages if message["type"] == "http.response.body"] == []
        assert stream.closed
        assert stream.response.closed

    asyncio.run(exercise())


def test_workspace_event_route_forwards_events_while_access_remains_valid(
    organization, monkeypatch
):
    app, _, _, org, agent, _, token, stream = _event_context(organization, monkeypatch)

    async def exercise():
        task, messages = await _open_event_route(
            app,
            f"/organizations/{org.id}/agents/{agent['id']}/opencode/event",
            token,
        )
        stream.response.release.set()
        await task
        assert [
            message["body"] for message in messages if message["type"] == "http.response.body"
        ] == [
            b'event: session.updated\ndata: {"id":"ses_main"}\n\n',
            b"",
        ]
        assert stream.closed
        assert stream.response.closed

    asyncio.run(exercise())


def test_workspace_event_route_finishes_response_after_host_disconnect(organization, monkeypatch):
    app, _, _, org, agent, _, token, stream = _event_context(organization, monkeypatch)

    async def disconnected_events():
        await stream.response.release.wait()
        yield b": connected\n\n"
        raise httpx.RemoteProtocolError("Upstream connection interrupted")

    monkeypatch.setattr(stream.response, "aiter_bytes", disconnected_events)

    async def exercise():
        task, messages = await _open_event_route(
            app, f"/organizations/{org.id}/agents/{agent['id']}/opencode/event", token
        )
        stream.response.release.set()
        await task
        bodies = [message for message in messages if message["type"] == "http.response.body"]
        assert bodies[0]["body"] == b": connected\n\n"
        assert bodies[-1]["body"] == b""
        assert bodies[-1]["more_body"] is False
        assert stream.closed
        assert stream.response.closed

    asyncio.run(exercise())


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


def test_personal_thread_pins_persist_per_user_and_order_workspace_lists(organization, monkeypatch):
    settings, control, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    second = control.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    other_org = control.create_organization(owner.id, "Other organization")
    requested: list[str] = []

    def native(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/opencode/session"):
            return httpx.Response(
                200,
                json=[
                    {"id": "ses_recent", "title": "Recent"},
                    {"id": "ses_pin", "title": "Pinned"},
                    {"id": "ses_other", "title": "Other"},
                ],
            )
        if request.url.path.endswith(("/opencode/session/ses_pin", "/opencode/session/ses_other")):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        return httpx.Response(404, json={"detail": "Thread not found"})

    monkeypatch.setattr(
        control_workspace_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(native)),
    )
    monkeypatch.setattr(
        control_thread_pin_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(native)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def login(client: httpx.AsyncClient, name: str, password: str):
        response = await client.post("/auth/login", json={"login": name, "password": password})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    async def exercise():
        pins = f"/organizations/{org.id}/agents/{agent['id']}/thread-pins"
        workspace = f"/organizations/{org.id}/agents/{agent['id']}/opencode/session"
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_second_device,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as member_device,
        ):
            await login(owner_device, "owner", "correct horse battery staple")
            no_csrf = await owner_device.put(f"{pins}/ses_pin", headers={"X-CSRF-Token": "missing"})
            assert no_csrf.status_code == 403
            assert (await owner_device.get(pins)).json() == {"session_ids": []}
            pinned = await owner_device.put(f"{pins}/ses_pin")
            assert pinned.status_code == 200 and pinned.json() == {"session_ids": ["ses_pin"]}
            assert (await owner_device.put(f"{pins}/ses_pin")).json() == {
                "session_ids": ["ses_pin"]
            }
            assert (await owner_device.get(workspace)).json() == [
                {"id": "ses_pin", "title": "Pinned"},
                {"id": "ses_recent", "title": "Recent"},
                {"id": "ses_other", "title": "Other"},
            ]
            await login(owner_second_device, "owner", "correct horse battery staple")
            assert (await owner_second_device.get(pins)).json() == {"session_ids": ["ses_pin"]}
            await login(member_device, "member", "correct horse battery staple")
            assert (await member_device.get(pins)).json() == {"session_ids": []}
            assert (await member_device.put(f"{pins}/ses_other")).json() == {
                "session_ids": ["ses_other"]
            }
            assert (await owner_device.get(pins)).json() == {"session_ids": ["ses_pin"]}
            assert (await owner_device.put(f"{pins}/ses_missing")).status_code == 404
            assert (
                await owner_device.get(
                    f"/organizations/{other_org.id}/agents/{agent['id']}/thread-pins"
                )
            ).status_code == 404
            assert (
                await owner_device.get(f"/organizations/{org.id}/agents/{uuid4()}/thread-pins")
            ).status_code == 404
            unpinned = await owner_device.delete(f"{pins}/ses_pin")
            assert unpinned.status_code == 200 and unpinned.json() == {"session_ids": []}

    asyncio.run(exercise())
    assert any(path.endswith("/opencode/session/ses_pin") for path in requested)
    assert second.user_id != owner.id
