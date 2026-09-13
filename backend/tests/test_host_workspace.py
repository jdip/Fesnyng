import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.host_workspace import Fork, QuestionReply, Workspace
from fesnyng_backend.host_workspace_routes import _event_session, events, router
from fesnyng_backend.settings import ServiceSettings


class Native:
    def __init__(self):
        self.calls = []
        self.branch_permission = None
        self.native_archived = 1_789_268_000_000
        self.children: dict[str, list[dict[str, object]]] = {}
        self.questions = [{"id": "q_main", "sessionID": "ses_main", "questions": []}]
        self.moves: list[dict[str, object]] = []

    def lock(self, agent_id):
        return asyncio.Lock()

    async def workspace_path(self, organization_id, agent_id, directory, path):
        return f"{directory}/{path}"

    async def request_with_query(self, organization_id, agent_id, path, query, *, directory):
        self.calls.append((organization_id, agent_id, path, "GET", query, directory))
        return {"type": "text", "content": "artifact"}

    async def create_session(self, organization_id, agent_id, title, workspace):
        return {"id": "ses_created", "title": title, "directory": f"/workspace/{workspace}/created"}

    async def fork_workspace(self, organization_id, agent_id, source_directory):
        return f"{source_directory}-fork-isolated"

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        self.calls.append((organization_id, agent_id, path, method, body, directory))
        if path == "/session/status":
            return {"ses_main": {"type": "idle"}, "ses_branch": {"type": "idle"}}
        if path == "/session/ses_main" and method == "GET":
            return {
                "id": "ses_main",
                "directory": directory,
                "title": "Main",
                "metadata": {"fixture": "source"},
                "time": {"archived": self.native_archived},
            }
        if path == "/session/ses_main/message":
            return [
                {
                    "info": {"id": "msg_main", "sessionID": "ses_main", "role": "assistant"},
                    "parts": [],
                }
            ]
        if path == "/session/ses_branch/message":
            return [
                {
                    "info": {
                        "id": "msg_branch",
                        "sessionID": "ses_branch",
                        "role": "assistant",
                    },
                    "parts": [],
                }
            ]
        if path.startswith("/session/") and path.endswith("/children"):
            return self.children.get(path, [])
        if path == "/session/ses_main/fork":
            return {
                "id": "ses_branch",
                "parentID": "ses_main",
                "directory": directory,
                "title": "Main (fork)",
                "metadata": {"fixture": "source"},
            }
        if path == "/experimental/control-plane/move-session" and method == "POST":
            if not isinstance(body, dict):
                raise AssertionError("native move requires an object body")
            self.moves.append(body)
            return None
        if path == "/session/ses_branch" and method == "PATCH":
            self.branch_permission = (body or {}).get("permission")
            return {"id": "ses_branch", "directory": directory}
        if path == "/session/ses_branch" and method == "GET":
            return {
                "id": "ses_branch",
                "directory": directory,
                "title": "Main (fork)",
                "metadata": {"fixture": "source"},
                "permission": self.branch_permission,
            }
        if path == "/question":
            return self.questions
        if path.startswith("/question/") and path.endswith(("/reply", "/reject")):
            request_id = path.split("/")[2]
            self.questions = [item for item in self.questions if item["id"] != request_id]
            return True
        if path == "/permission":
            return []
        if path == "/session/ses_main/abort":
            return True
        if path == "/instance/dispose":
            return True
        if path == "/session/ses_main" and method == "PATCH":
            return {"id": "ses_main", "title": (body or {}).get("title", "Main")}
        raise AssertionError((path, method, body, directory))


class EventNative(Native):
    def __init__(self):
        super().__init__()
        self.events: dict[str, asyncio.Queue[str | None]] = {}

    @asynccontextmanager
    async def event_stream(self, organization_id, agent_id, directory):
        queue = self.events.setdefault(directory, asyncio.Queue())

        async def lines():
            while (line := await queue.get()) is not None:
                yield line

        yield lines()


def test_workspace_facade_scopes_native_history_and_persists_text_prompt(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    token = secrets.token_urlsafe(32)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, token)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Agent",
        configuration=AgentConfiguration(workspace="assigned"),
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")
    store.save_session(org, agent, "ses_other", "/workspace/default/other", "Other")
    native = Native()
    app = FastAPI()
    app.state.host_store = store
    app.state.host_runtime = native
    app.state.dispatch_store = DispatchStore(store)
    app.state.dispatch_store.initialize()
    app.state.dispatcher = Dispatcher(app.state.dispatch_store, native)
    app.state.interactions = Interactions(store, native)
    app.state.interactions.initialize()
    app.include_router(router)
    author = {"kind": "human", "id": str(uuid4()), "name": "Member"}
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Fesnyng-Actor": json.dumps(author),
        "Idempotency-Key": str(uuid4()),
    }

    async def check():
        base = f"/organizations/{org}/agents/{agent}/opencode/session"
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://host", headers=headers
        ) as client:
            created = await client.post(base, json={})
            assert created.status_code == 201 and created.json()["title"] == "New thread"
            assert created.json()["directory"] == "/workspace/assigned/created"
            listed = await client.get(base)
            assert listed.status_code == 200 and listed.json()[0]["id"] == "ses_main"
            status = await client.get(f"{base}/status")
            assert status.status_code == 200 and status.json() == {"ses_main": {"type": "idle"}}
            history = await client.get(f"{base}/ses_main/message")
            assert history.status_code == 200
            assert native.calls[-1][2:] == (
                "/session/ses_main/message",
                "GET",
                None,
                "/workspace/default/main",
            )
            foreign = await client.get(f"{base}/ses_missing/message")
            assert foreign.status_code == 404
            prompt = await client.post(
                f"{base}/ses_main/prompt_async",
                json={
                    "parts": [
                        {"type": "text", "text": "First "},
                        {"type": "text", "text": "prompt"},
                    ]
                },
            )
            assert prompt.status_code == 202
            receipt = prompt.json()
            assert receipt["payload"]["text"] == "First prompt"
            assert receipt["author"] == {**author, "session_id": None}
            assert app.state.dispatcher.changed.is_set()
            aborted = await client.post(
                f"{base}/ses_main/abort", headers={"Idempotency-Key": str(uuid4())}
            )
            assert aborted.status_code == 202 and aborted.json()["payload"]["mode"] == "stop"
            unsupported = await client.post(
                f"{base}/ses_main/prompt_async",
                json={"parts": [{"type": "file", "url": "file:///secret"}]},
            )
            assert unsupported.status_code == 422
            missing_actor = await client.patch(
                f"{base}/ses_main",
                json={"title": "Renamed"},
                headers={"Authorization": f"Bearer {token}", "X-Fesnyng-Actor": ""},
            )
            assert missing_actor.status_code == 400
            artifact = await client.get(
                f"/organizations/{org}/agents/{agent}/opencode/file/content",
                params={"sessionID": "ses_main", "path": "notes.txt"},
            )
            assert artifact.status_code == 200 and artifact.json()["content"] == "artifact"
            escaped = await client.get(
                f"/organizations/{org}/agents/{agent}/opencode/file/content",
                params={"sessionID": "ses_main", "path": "../secret"},
            )
            assert escaped.status_code == 409
            for delivery in app.state.dispatch_store.for_thread(org, agent, "ses_main"):
                app.state.dispatch_store.change(delivery, "completed")
            archived = await client.patch(f"{base}/ses_main", json={"time": {"archived": 1234}})
            assert archived.status_code == 200
            assert not any(
                call[2:5] == ("/session/ses_main", "PATCH", {"time": {"archived": 1234}})
                for call in native.calls
            )
            assert [session["id"] for session in (await client.get(base)).json()] == ["ses_other"]
            archived_list = await client.get(
                f"/organizations/{org}/agents/{agent}/opencode/experimental/session",
                params={"archived": "true"},
            )
            assert archived_list.status_code == 200
            assert {session["id"] for session in archived_list.json()} == {"ses_main", "ses_other"}
            assert (
                next(session for session in archived_list.json() if session["id"] == "ses_main")[
                    "time"
                ]["archived"]
                == 1234
            )
            restored = await client.patch(f"{base}/ses_main", json={"time": {"archived": None}})
            assert restored.status_code == 200
            # The native record deliberately remains stale after null.  The
            # scoped façade's navigation archive owner must prevent it from
            # re-hiding the active mapped thread.
            restored_get = await client.get(f"{base}/ses_main")
            assert restored_get.status_code == 200
            assert "archived" not in restored_get.json()["time"]

    asyncio.run(check())


def test_host_app_wires_workspace_facade(tmp_path):
    app = create_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )

    async def check():
        async with AsyncClient(transport=ASGITransport(app), base_url="http://host") as client:
            response = await client.get(
                f"/organizations/{uuid4()}/agents/{uuid4()}/opencode/session"
            )
            assert response.status_code == 401

    asyncio.run(check())


def test_workspace_fork_verifies_native_ancestry_and_reapplies_thread_policy(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    binding = secrets.token_urlsafe(32)
    store.bind_organization(org, binding)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")

    class EmptyStatusNative(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {}
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = EmptyStatusNative()
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, native)
    interactions.initialize()
    workspace = Workspace(store, native, dispatches, interactions)
    author = Actor(kind="human", id=uuid4(), name="Member")

    forked = asyncio.run(
        workspace.fork(
            org, agent, "ses_main", Fork.model_validate({"messageID": "msg_main"}), author
        )
    )

    assert forked["id"] == "ses_branch"
    assert forked["directory"] == "/workspace/default/main-fork-isolated"
    assert (
        store.session(org, agent, "ses_branch")["directory"]
        == "/workspace/default/main-fork-isolated"
    )
    assert native.moves == [
        {
            "sessionID": "ses_branch",
            "destination": {"directory": "/workspace/default/main-fork-isolated"},
            "moveChanges": False,
        }
    ]
    assert (
        org,
        agent,
        "/session/ses_main/fork",
        "POST",
        {"messageID": "msg_main"},
        "/workspace/default/main",
    ) in native.calls
    assert [
        call[5]
        for call in native.calls
        if call[2:5] == ("/session/ses_branch/message", "GET", None)
    ] == ["/workspace/default/main", "/workspace/default/main-fork-isolated"]
    assert native.branch_permission is not None


def test_workspace_fork_refuses_a_child_that_did_not_move_to_the_copy(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    source_directory = "/workspace/default/threads/source"
    store.save_session(org, agent, "ses_main", source_directory, "Main")

    class MisboundForkNative(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/ses_branch" and method == "GET":
                return {
                    "id": "ses_branch",
                    "directory": source_directory,
                    "title": "Main (fork)",
                }
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = MisboundForkNative()
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, native)
    interactions.initialize()
    workspace = Workspace(store, native, dispatches, interactions)
    author = Actor(kind="human", id=uuid4(), name="Member")

    with pytest.raises(RuntimeUnavailable, match="outcome is uncertain"):
        asyncio.run(workspace.fork(org, agent, "ses_main", Fork(), author))
    with pytest.raises(LookupError):
        store.session(org, agent, "ses_branch")


def test_workspace_fork_retains_unmapped_native_child_when_move_is_uncertain(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")

    class MoveFailsNative(Native):
        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/experimental/control-plane/move-session":
                raise RuntimeUnavailable("Native runtime request failed (HTTP 400)")
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = MoveFailsNative()
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, native)
    interactions.initialize()
    workspace = Workspace(store, native, dispatches, interactions)
    author = Actor(kind="human", id=uuid4(), name="Member")

    with pytest.raises(RuntimeUnavailable, match="outcome is uncertain"):
        asyncio.run(workspace.fork(org, agent, "ses_main", Fork(), author))
    with pytest.raises(LookupError):
        store.session(org, agent, "ses_branch")


def test_workspace_fork_refuses_an_active_source_before_copying(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")

    class ActiveSourceNative(Native):
        def __init__(self):
            super().__init__()
            self.copy_attempts = 0

        async def fork_workspace(self, organization_id, agent_id, source_directory):
            self.copy_attempts += 1
            return await super().fork_workspace(organization_id, agent_id, source_directory)

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            if path == "/session/status":
                return {"ses_main": {"type": "busy"}}
            return await super().request(
                organization_id, agent_id, path, method=method, body=body, directory=directory
            )

    native = ActiveSourceNative()
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, native)
    interactions.initialize()
    workspace = Workspace(store, native, dispatches, interactions)
    author = Actor(kind="human", id=uuid4(), name="Member")

    with pytest.raises(RuntimeUnavailable, match="source session is active"):
        asyncio.run(workspace.fork(org, agent, "ses_main", Fork(), author))
    assert native.copy_attempts == 0


def test_workspace_accepts_native_message_id_field_names():
    assert Fork.model_validate({"messageID": "msg_parent"}).message_id == "msg_parent"
    from fesnyng_backend.host_workspace import Revert

    assert Revert.model_validate({"messageID": "msg_parent"}).message_id == "msg_parent"


def test_workspace_child_pending_and_reply_use_verified_child_directory_but_root_receipt(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")
    native = Native()
    native.children["/session/ses_main/children"] = [
        {"id": "ses_child", "parentID": "ses_main", "directory": "/workspace/default/child"}
    ]
    native.questions = [{"id": "q_child", "sessionID": "ses_child", "questions": []}]
    dispatches = DispatchStore(store)
    dispatches.initialize()
    interactions = Interactions(store, native)
    interactions.initialize()
    interactions.get_policy(org, agent, "ses_main")
    interactions._mark_policy_applied(org, agent, "ses_main", 0, envelope.policy_version)
    workspace = Workspace(store, native, dispatches, interactions)
    author = Actor(kind="human", id=uuid4(), name="Member")

    async def check():
        pending = await workspace.pending_all(org, agent, "question")
        assert pending == native.questions
        return await workspace.reply_question(
            org,
            agent,
            "q_child",
            QuestionReply(operation_id=uuid4(), answers=[["Yes"]]),
            author,
        )

    receipt = asyncio.run(check())
    assert receipt["state"] == "completed"
    assert receipt["session_id"] == "ses_main"
    assert native.calls[-1][2:] == (
        "/question/q_child/reply",
        "POST",
        {"answers": [["Yes"]]},
        "/workspace/default/child",
    )


def test_workspace_does_not_treat_an_uncertain_interaction_receipt_as_answered(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, secrets.token_urlsafe(32))
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")
    native = Native()
    interactions = Interactions(store, native)
    interactions.initialize()
    author = Actor(kind="human", id=uuid4(), name="Member")
    operation = uuid4()
    interactions._start_reply(
        org, agent, "ses_main", operation, "q_main", "question", [["Yes"]], author
    )
    interactions.recover_interrupted()
    workspace = Workspace(store, native, DispatchStore(store), interactions)

    with pytest.raises(RuntimeUnavailable, match="outcome is uncertain"):
        asyncio.run(
            workspace.reply_question(
                org,
                agent,
                "q_main",
                QuestionReply(operation_id=operation, answers=[["Yes"]]),
                author,
            )
        )


def test_workspace_events_use_native_identity_shapes_sse_framing_and_new_directories(tmp_path):
    org, agent = str(uuid4()), str(uuid4())
    token = secrets.token_urlsafe(32)
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
    )
    store = HostStore(settings)
    store.initialize()
    store.bind_organization(org, token)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id, organization_id=org, agent_id=agent, version=1, name="Agent"
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(org, agent, "ses_main", "/workspace/default/main", "Main")
    app = FastAPI()
    app.state.host_store = store
    app.state.host_runtime = EventNative()
    app.state.dispatch_store = DispatchStore(store)
    app.state.dispatch_store.initialize()
    app.state.interactions = Interactions(store, app.state.host_runtime)
    app.state.interactions.initialize()
    app.state.dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def check():
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/event",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "app": app,
                "query_string": b"",
            }
        )
        response = await events(request, UUID(org), UUID(agent))
        iterator = response.body_iterator
        first = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0.3)
        await app.state.host_runtime.events["/workspace/default/main"].put(
            'data: {"type":"message.updated","properties":{"info":{"sessionID":"ses_main","time":{"archived":1789268000000}}}}'
        )
        assert (
            await first
            == 'event: message\ndata: {"type":"message.updated","properties":{"info":{"sessionID":"ses_main","time":{}}}}\n\n'
        )
        permission = asyncio.create_task(anext(iterator))
        await app.state.host_runtime.events["/workspace/default/main"].put(
            'data: {"type":"permission.asked","properties":{"sessionID":"ses_main","always":["*"],"permission":"bash"}}'
        )
        assert (
            await permission
            == 'event: message\ndata: {"type":"permission.asked","properties":{"sessionID":"ses_main","always":[],"permission":"bash"}}\n\n'
        )
        store.save_session(org, agent, "ses_later", "/workspace/default/later", "Later")
        second = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0.3)
        await app.state.host_runtime.events["/workspace/default/later"].put(
            'data: {"type":"part.updated","properties":{"part":{"sessionID":"ses_later"}}}'
        )
        assert (
            await second
            == 'event: message\ndata: {"type":"part.updated","properties":{"part":{"sessionID":"ses_later"}}}\n\n'
        )
        await app.state.host_runtime.events["/workspace/default/main"].put(None)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await iterator.aclose()

    asyncio.run(check())
    assert _event_session({"properties": {"info": {"id": "ses_main"}}}) == "ses_main"
