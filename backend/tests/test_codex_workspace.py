import asyncio
import json
import secrets
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fesnyng_backend.agent_models import AgentConfiguration, NativeSkill
from fesnyng_backend.codex_history import is_unmaterialized
from fesnyng_backend.host_codex_routes import router
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore, Submission
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings


class Codex:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.unmaterialized = False
        self.paginated = False
        self.active_turn = False
        self.policy_calls = []

    async def call(self, org, agent, method, params):
        self.calls.append((org, agent, method, params))
        if method == "skills/list":
            return {
                "data": [
                    {
                        "cwd": params.get("cwds", ["/workspace/default/codex"])[0],
                        "errors": [],
                        "skills": [
                            {
                                "name": "review",
                                "path": "/home/agent/.codex/skills/fesnyng/review/SKILL.md",
                                "description": "review",
                            }
                        ],
                    }
                ]
            }
        thread_id = params["threadId"]
        if method == "thread/turns/list" and self.unmaterialized:
            raise RuntimeUnavailable("list_turns is not supported yet")
        if method == "thread/turns/list" and self.paginated:
            if params.get("cursor") is None:
                return {
                    "data": [{"id": "turn_first", "status": "completed", "items": []}],
                    "nextCursor": "next-page",
                }
            assert params["cursor"] == "next-page"
            return {
                "data": [{"id": "turn_second", "status": "completed", "items": []}],
                "nextCursor": None,
            }
        if method in {"thread/read", "thread/turns/list"}:
            if method == "thread/turns/list" and self.active_turn:
                return {
                    "data": [{"id": "turn_known", "status": "inProgress", "items": []}],
                    "nextCursor": None,
                }
            return {
                "thread": {"id": thread_id, "title": "Native Codex"},
                "turns" if method == "thread/read" else "data": [
                    {
                        "id": "turn_done",
                        "status": "completed",
                        "items": [
                            {"id": "item_user", "type": "userMessage", "text": "hello"},
                            {
                                "id": "item_command",
                                "type": "commandExecution",
                                "command": "git status",
                            },
                        ],
                    }
                ],
            }
        if method == "thread/name/set":
            return {"thread": {"id": thread_id, "name": params["name"]}}
        if method == "thread/resume":
            return {"thread": {"id": thread_id}}
        if method in {"thread/archive", "thread/unarchive"}:
            return {}
        if method == "turn/start":
            return {"turn": {"id": "turn_started", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": "turn_steered"}
        if method == "turn/interrupt":
            return {}
        raise AssertionError((method, params))

    async def apply_policy(self, org, agent, thread_id, envelope, overrides):
        self.policy_calls.append((org, agent, thread_id, envelope, overrides))

    async def pending(self, org, agent, thread_id):
        return [
            {
                "id": "req_approval",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": thread_id},
            }
        ]

    async def respond(self, org, agent, thread_id, request_id, response):
        self.replies.append((org, agent, thread_id, request_id, response))
        return {"ok": True}

    async def events(self, org, agent):
        if False:
            yield {}


class RuntimeRouter:
    def __init__(self, codex):
        self.codex = codex

    def for_session(self, session):
        assert session["runtime_type"] == "codex"
        return self.codex


class Runtime:
    def __init__(self, store, codex):
        self.store, self.codex = store, codex
        self.runtime_router = RuntimeRouter(codex)
        self.locks = {}
        self.create_calls = 0

    def lock(self, agent_id):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        raise AssertionError("OpenCode request on Codex runtime")

    async def create_session(self, org, agent, title, workspace):
        self.create_calls += 1
        self.store.save_session(
            org,
            agent,
            "thr_created",
            f"/workspace/{workspace}/created",
            title,
            runtime_type="codex",
        )
        return {"id": "thr_created", "title": title}


class Waker:
    def wake(self):
        return None


def _app(tmp_path, *, policy_applied=True):
    org, agent = str(uuid4()), str(uuid4())
    store = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "state",
        )
    )
    store.initialize()
    token = secrets.token_urlsafe(32)
    store.bind_organization(org, token)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=org,
        agent_id=agent,
        version=1,
        name="Codex",
        configuration=AgentConfiguration(runtime_type="codex"),
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(
        org, agent, "thr_codex", "/workspace/default/codex", "Codex", runtime_type="codex"
    )
    dispatches = DispatchStore(store)
    dispatches.initialize()
    codex = Codex()
    app = FastAPI()
    app.state.host_store = store
    app.state.host_runtime = Runtime(store, codex)
    app.state.dispatch_store = dispatches
    app.state.interactions = Interactions(store, app.state.host_runtime)
    app.state.interactions.initialize()
    if policy_applied:
        asyncio.run(app.state.interactions.apply_policy(org, agent, "thr_codex", envelope))
    app.state.dispatcher = Waker()
    app.include_router(router)
    return app, org, agent, token, codex


def test_codex_workspace_preserves_native_turn_items_and_scopes_pending_replies(tmp_path):
    app, org, agent, token, codex = _app(tmp_path)
    actor = {"kind": "human", "id": str(uuid4()), "name": "Owner"}

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            history = await client.get(
                f"/organizations/{org}/agents/{agent}/codex/session/thr_codex/history"
            )
            pending = await client.get(
                f"/organizations/{org}/agents/{agent}/codex/pending",
                params={"sessionID": "thr_codex"},
            )
            replied = await client.post(
                f"/organizations/{org}/agents/{agent}/codex/pending/req_approval/reply",
                params={"sessionID": "thr_codex"},
                headers={"X-Fesnyng-Actor": json.dumps(actor), "Idempotency-Key": str(uuid4())},
                json={"response": {"decision": "accept"}},
            )
            prompt = await client.post(
                f"/organizations/{org}/agents/{agent}/codex/session/thr_codex/prompt",
                headers={"X-Fesnyng-Actor": json.dumps(actor), "Idempotency-Key": str(uuid4())},
                json={"text": "continue"},
            )
            return history, pending, replied, prompt

    history, pending, replied, prompt = asyncio.run(exercise())
    assert history.status_code == pending.status_code == replied.status_code == 200
    assert history.json()["turns"][0]["items"][1] == {
        "id": "item_command",
        "type": "commandExecution",
        "command": "git status",
    }
    assert codex.calls[0][3] == {"threadId": "thr_codex", "includeTurns": True}
    assert history.json()["historyState"] == "complete"
    assert pending.json()[0]["id"] == "req_approval"
    assert replied.json()["answer"] == {"decision": "accept"}
    assert codex.replies[-1][-1] == {"decision": "accept"}
    assert prompt.status_code == 202
    assert prompt.json()["state"] == "queued"


def test_codex_create_rejects_an_opencode_employee_before_native_creation(tmp_path):
    app, org, agent, token, _codex = _app(tmp_path)
    current = app.state.host_store.agent(org, agent)
    envelope = HostAgentConfiguration.model_validate_json(current["applied_envelope"]).model_copy(
        update={"version": 2, "configuration": AgentConfiguration(runtime_type="opencode")}
    )
    app.state.host_store.stage_agent(envelope)
    app.state.host_store.mark_applied(envelope)
    actor = {"kind": "human", "id": str(uuid4()), "name": "Owner"}

    async def create():
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.post(
                f"/organizations/{org}/agents/{agent}/codex/session",
                headers={"X-Fesnyng-Actor": json.dumps(actor)},
                json={},
            )

    response = asyncio.run(create())
    assert response.status_code == 409
    assert app.state.host_runtime.create_calls == 0


def test_dispatcher_starts_a_codex_turn_and_records_its_native_turn_receipt(tmp_path):
    app, org, agent, _token, codex = _app(tmp_path)
    receipt = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), text="continue"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await dispatcher._thread((org, agent, "thr_codex"), [receipt])
        await asyncio.gather(*dispatcher.tasks.values())

    asyncio.run(submit())
    stored = app.state.dispatch_store.get(org, agent, receipt["id"])
    assert (
        org,
        agent,
        "turn/start",
        {
            "threadId": "thr_codex",
            "input": [{"type": "text", "text": "continue"}],
            "clientUserMessageId": receipt["id"],
            "model": "gpt-6-astra",
        },
    ) in codex.calls
    assert stored["state"] == "active"
    assert stored["native_message_id"] == "turn_started"


def test_dispatcher_applies_a_new_codex_thread_policy_without_reentering_agent_lock(tmp_path):
    app, org, agent, _token, _codex = _app(tmp_path, policy_applied=False)
    receipt = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), text="continue"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    app.state.interactions.put_policy(
        org,
        agent,
        "thr_codex",
        0,
        [],
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await asyncio.wait_for(dispatcher._thread((org, agent, "thr_codex"), [receipt]), timeout=1)
        await asyncio.wait_for(asyncio.gather(*dispatcher.tasks.values()), timeout=1)

    asyncio.run(submit())
    assert app.state.dispatch_store.get(org, agent, receipt["id"])["state"] == "active"


def test_stop_interrupts_known_active_turn_without_settling_an_unresolved_lost_steer(tmp_path):
    app, org, agent, _token, codex = _app(tmp_path)
    codex.active_turn = True
    unknown = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), text="unknown"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    app.state.dispatch_store.change(unknown, "unresolved", error="lost steer response")
    stop = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), mode="stop"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await dispatcher._thread((org, agent, "thr_codex"), [unknown, stop])
        await asyncio.gather(*dispatcher.tasks.values())

    asyncio.run(submit())
    assert app.state.dispatch_store.get(org, agent, unknown["id"])["state"] == "unresolved"
    assert app.state.dispatch_store.get(org, agent, stop["id"])["state"] == "stopping"
    assert any(
        call[2] == "turn/interrupt" and call[3]["turnId"] == "turn_known" for call in codex.calls
    )


def test_stop_bypasses_a_staged_model_change_but_still_interrupts_known_work(tmp_path):
    app, org, agent, _token, codex = _app(tmp_path)
    codex.active_turn = True
    applied = HostAgentConfiguration.model_validate_json(
        app.state.host_store.agent(org, agent)["applied_envelope"]
    )
    app.state.host_store.stage_agent(applied.model_copy(update={"version": 2}))
    stop = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), mode="stop"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await dispatcher._thread((org, agent, "thr_codex"), [stop])
        await asyncio.gather(*dispatcher.tasks.values())

    asyncio.run(submit())
    assert app.state.dispatch_store.get(org, agent, stop["id"])["state"] == "stopping"


def test_stale_codex_submission_does_not_resurrect_a_cancelled_receipt(tmp_path):
    app, org, agent, _token, _codex = _app(tmp_path)
    receipt = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), text="stale"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    app.state.dispatch_store.change(
        receipt, "cancelled", outcome={"kind": "cancelled_before_submission"}
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)
    asyncio.run(
        dispatcher._submit_codex(
            receipt, app.state.host_store.session(org, agent, "thr_codex"), None
        )
    )
    assert app.state.dispatch_store.get(org, agent, receipt["id"])["state"] == "cancelled"


def test_dispatcher_can_start_the_first_codex_turn_before_history_is_materialized(tmp_path):
    app, org, agent, _token, codex = _app(tmp_path)
    codex.unmaterialized = True
    receipt = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), text="first turn"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await dispatcher._thread((org, agent, "thr_codex"), [receipt])
        await asyncio.gather(*dispatcher.tasks.values())

    asyncio.run(submit())
    assert app.state.dispatch_store.get(org, agent, receipt["id"])["state"] == "active"


def test_only_the_bound_thread_materialization_error_permits_its_first_turn():
    error = RuntimeUnavailable(
        "thread thr_codex is not materialized yet; "
        "thread/turns/list is unavailable before first user message"
    )
    assert is_unmaterialized(error, "thr_codex")
    assert not is_unmaterialized(error, "other_thread")


def test_dispatcher_resolves_a_configured_native_skill_before_starting_a_codex_turn(tmp_path):
    app, org, agent, _token, codex = _app(tmp_path)
    current = app.state.host_store.agent(org, agent)
    envelope = HostAgentConfiguration.model_validate_json(current["applied_envelope"]).model_copy(
        update={
            "version": 2,
            "configuration": AgentConfiguration(
                runtime_type="codex",
                skills=[NativeSkill(name="review", content="Review.", explicit_only=True)],
            ),
        }
    )
    app.state.host_store.stage_agent(envelope)
    app.state.host_store.mark_applied(envelope)
    receipt = app.state.dispatch_store.enqueue(
        org,
        agent,
        "thr_codex",
        Submission(id=uuid4(), command="fesnyng/review", text="inspect this"),
        Actor(kind="human", id=uuid4(), name="Owner"),
    )
    dispatcher = Dispatcher(app.state.dispatch_store, app.state.host_runtime)

    async def submit():
        await dispatcher._thread((org, agent, "thr_codex"), [receipt])
        await asyncio.gather(*dispatcher.tasks.values())

    asyncio.run(submit())
    start = next(call for call in codex.calls if call[2] == "turn/start")
    assert start[3]["input"] == [
        {
            "type": "skill",
            "name": "review",
            "path": "/home/agent/.codex/skills/fesnyng/review/SKILL.md",
        },
        {"type": "text", "text": "inspect this"},
    ]


def test_codex_history_reads_each_full_cursor_page_without_duplicates(tmp_path):
    app, org, agent, token, codex = _app(tmp_path)
    codex.paginated = True

    async def read():
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.get(
                f"/organizations/{org}/agents/{agent}/codex/session/thr_codex/history"
            )

    response = asyncio.run(read())
    assert response.status_code == 200
    assert [turn["id"] for turn in response.json()["turns"]] == ["turn_first", "turn_second"]
    assert any(call[3].get("cursor") == "next-page" for call in codex.calls)


def test_codex_archive_and_unarchive_keep_the_mapped_thread_visible(tmp_path):
    app, org, agent, token, codex = _app(tmp_path)
    actor = {"kind": "human", "id": str(uuid4()), "name": "Owner"}

    async def update(value):
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.patch(
                f"/organizations/{org}/agents/{agent}/codex/session/thr_codex",
                headers={"X-Fesnyng-Actor": json.dumps(actor)},
                json={"time": {"archived": value}},
            )

    archived = asyncio.run(update(123))
    unarchived = asyncio.run(update(None))
    assert archived.status_code == unarchived.status_code == 200
    assert [
        call[2] for call in codex.calls if call[2] in {"thread/archive", "thread/unarchive"}
    ] == [
        "thread/archive",
        "thread/unarchive",
    ]
    assert app.state.host_store.session(org, agent, "thr_codex")["archived_at"] is None


def test_frozen_codex_history_and_native_write_rejection_survive_host_reload(tmp_path):
    app, org, agent, token, native = _app(tmp_path)
    from fesnyng_backend.host_routes import router as host_router

    app.include_router(host_router)
    store = app.state.host_store
    store.begin_harness_switch(org, agent, 1, "opencode")
    retained = {
        "thread": {"id": "thr_codex"},
        "turns": [
            {
                "id": "turn_retained",
                "status": "completed",
                "items": [
                    {
                        "id": "patch",
                        "type": "fileChange",
                        "changes": [
                            {"path": "proof.txt", "kind": {"type": "add"}, "diff": "+retained"}
                        ],
                    }
                ],
            }
        ],
        "historyState": "complete",
    }
    store.commit_freeze(
        org,
        agent,
        {
            "thr_codex": {
                "runtime_type": "codex",
                "session": {"id": "thr_codex", "title": "Retained"},
                "history": retained,
                "children": {
                    "thr_child": {
                        "session": {
                            "id": "thr_child",
                            "cwd": "/workspace/proof/child",
                            "title": "Child",
                        },
                        "history": {
                            "thread": {"id": "thr_child"},
                            "turns": [],
                            "historyState": "complete",
                        },
                    }
                },
            }
        },
    )
    # A freshly opened host store must own reads even if no native connection exists.
    app.state.host_store = HostStore(store.settings)
    before = len(native.calls)
    actor = {"kind": "human", "id": str(uuid4()), "name": "Owner"}

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app),
            base_url="http://host",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Fesnyng-Actor": json.dumps(actor),
                "Idempotency-Key": str(uuid4()),
            },
        ) as client:
            prefix = f"/organizations/{org}/agents/{agent}/codex"
            history = await client.get(f"{prefix}/session/thr_codex/history")
            assert history.status_code == 200
            assert history.json() == retained
            generic = await client.get(
                f"/organizations/{org}/agents/{agent}/sessions/thr_codex/messages"
            )
            assert generic.status_code == 200 and generic.json() == retained
            info = await client.get(f"{prefix}/session/thr_codex")
            assert info.json()["frozen"] is True
            child = await client.get(f"{prefix}/session/thr_child")
            assert child.status_code == 200
            assert child.json()["directory"] == "/workspace/proof/child"
            assert (
                await client.get(f"{prefix}/pending", params={"sessionID": "thr_codex"})
            ).json() == []
            for path, method, body in (
                ("session/thr_codex", "PATCH", {"title": "Changed"}),
                ("session/thr_codex/prompt", "POST", {"text": "new work"}),
                ("session/thr_codex/abort", "POST", {}),
            ):
                response = await client.request(method, f"{prefix}/{path}", json=body)
                assert response.status_code == 409, response.text
                assert "permanently frozen" in response.text

    asyncio.run(exercise())
    assert len(native.calls) == before
