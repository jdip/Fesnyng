import asyncio
import secrets
from uuid import uuid4

import httpx

from fesnyng_backend import control_interaction_routes
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app as create_control_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_interaction_routes import router as host_interaction_router
from fesnyng_backend.host_interactions import Interactions
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


def test_interaction_routes_scope_native_requests_and_retain_pending_policy_changes(
    organization, monkeypatch, tmp_path
):
    settings, control, owner, org, agents, host_id = organization
    host_token = secrets.token_urlsafe(32)
    agents.set_host_credential(org.id, host_id, host_token)
    agent = agents.create_agent(org.id, owner.id, {"name": "Interaction agent", "host_id": host_id})
    host_app = create_host_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "host-state",
        )
    )
    host_store = host_app.state.host_store
    host_store.bind_organization(org.id, host_token)
    envelope = HostAgentConfiguration(
        host_id=host_store.instance_id,
        organization_id=org.id,
        agent_id=agent["id"],
        version=1,
        name=agent["name"],
    )
    host_store.stage_agent(envelope)
    host_store.mark_applied(envelope)
    host_store.save_session(org.id, agent["id"], "ses_main", "/workspace/main", "Main")
    host_store.save_session(org.id, agent["id"], "ses_other", "/workspace/other", "Other")
    native = NativeInteractions("ses_main")
    host_app.state.interactions = Interactions(host_store, native)
    host_app.state.interactions.initialize()
    asyncio.run(host_app.state.interactions.apply_policy(org.id, agent["id"], "ses_main"))
    host_app.include_router(host_interaction_router)
    control.add_member(
        org.id,
        "member",
        "Interaction Member",
        "a separate member password",
        "member",
        actor_id=owner.id,
    )
    other = control.create_organization(owner.id, "Other organization")
    control_app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    control_app.include_router(control_interaction_routes.router)
    host_client = HostClient(agents, transport=httpx.ASGITransport(app=host_app))
    monkeypatch.setattr(control_interaction_routes, "host_client", lambda _: host_client)

    async def check():
        transport = httpx.ASGITransport(app=control_app)
        headers = {"Origin": ORIGIN}
        async with (
            httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as owner_client,
            httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as member_client,
        ):
            member = await _sign_in(member_client, "member", "a separate member password")
            await _sign_in(owner_client, "owner", "correct horse battery staple")
            base = f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_main"

            questions = await member_client.get(f"{base}/questions")
            assert questions.status_code == 200 and questions.json() == native.questions
            forged = await member_client.post(
                f"{base}/questions/question_1/reply",
                json={
                    "operation_id": str(uuid4()),
                    "answers": [["Continue"]],
                    "author": {"kind": "human", "id": str(uuid4()), "name": "Forged"},
                },
            )
            assert forged.status_code == 422
            question_operation = str(uuid4())
            question = await member_client.post(
                f"{base}/questions/question_1/reply",
                json={"operation_id": question_operation, "answers": [["Continue"]]},
            )
            assert question.status_code == 200
            assert question.json()["author"]["id"] == member["id"]
            assert native.replies[-1] == ("/question/question_1/reply", {"answers": [["Continue"]]})
            receipt = await member_client.get(
                f"{base}/questions/question_1/replies/{question_operation}"
            )
            assert receipt.status_code == 200 and receipt.json() == question.json()
            assert (
                await member_client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_other/questions/question_1/replies/{question_operation}"
                )
            ).status_code == 404

            native.questions = [{"id": "question_other", "sessionID": "ses_other"}]
            foreign = await member_client.post(
                f"{base}/questions/question_other/reply",
                json={"operation_id": str(uuid4()), "answers": [["Continue"]]},
            )
            assert foreign.status_code == 409
            always = await member_client.post(
                f"{base}/permissions/permission_1/reply",
                json={"operation_id": str(uuid4()), "reply": "always"},
            )
            assert always.status_code == 422
            permission = await member_client.post(
                f"{base}/permissions/permission_1/reply",
                json={"operation_id": str(uuid4()), "reply": "once"},
            )
            assert permission.status_code == 200
            assert native.replies[-1] == ("/permission/permission_1/reply", {"reply": "once"})

            policy = await member_client.put(
                f"{base}/policy",
                json={
                    "expected_revision": 0,
                    "rules": [{"permission": "read", "pattern": "/workspace/*", "action": "allow"}],
                },
            )
            assert policy.status_code == 200
            assert policy.json()["desired_revision"] == policy.json()["applied_revision"] == 1
            assert policy.json()["author"]["id"] == member["id"]

            native.verify = False
            pending = await member_client.put(
                f"{base}/policy",
                json={
                    "expected_revision": 1,
                    "rules": [{"permission": "write", "pattern": "/workspace/*", "action": "ask"}],
                },
            )
            assert pending.status_code == 503
            retained = await member_client.get(f"{base}/policy")
            assert retained.status_code == 200
            assert retained.json()["desired_revision"] == 2
            assert retained.json()["applied_revision"] == 1
            assert (
                await member_client.get(
                    f"/organizations/{other.id}/agents/{agent['id']}/sessions/ses_main/questions"
                )
            ).status_code == 404

    asyncio.run(check())


async def _sign_in(client: httpx.AsyncClient, login: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]


class NativeInteractions:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.locks = {}
        self.questions = [{"id": "question_1", "sessionID": session_id}]
        self.pending_permissions = [{"id": "permission_1", "sessionID": session_id}]
        self.replies = []
        self.permissions = []
        self.verify = True

    def lock(self, agent_id: str):
        return self.locks.setdefault(agent_id, asyncio.Lock())

    async def request(
        self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
    ):
        if path.endswith("/children"):
            return []
        if path == "/question":
            return self.questions
        if path == "/permission":
            return self.pending_permissions
        if path.startswith(("/question/", "/permission/")):
            self.replies.append((path, body))
            return None
        if path == "/session/status":
            return {self.session_id: {"type": "idle"}}
        if path == "/instance/dispose":
            return None
        if path == f"/session/{self.session_id}" and method == "PATCH":
            if not isinstance(body, dict):
                raise AssertionError("Missing native policy body")
            self.permissions.extend(body["permission"])
            return None
        if path == f"/session/{self.session_id}":
            return {"permission": self.permissions if self.verify else []}
        raise AssertionError(path)
