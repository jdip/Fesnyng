import asyncio
import secrets
from uuid import uuid4

import httpx

from fesnyng_backend import control_dispatch_routes
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app as create_control_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_dispatch import Dispatcher, DispatchStore, Submission
from fesnyng_backend.host_dispatch_routes import router as host_dispatch_router
from fesnyng_backend.host_models import Actor, HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


def test_dispatch_routes_persist_attributed_receipts_and_keep_them_scoped(
    organization, monkeypatch, tmp_path
):
    settings, control, owner, org, agents, host_id = organization
    host_token = secrets.token_urlsafe(32)
    agents.set_host_credential(org.id, host_id, host_token)
    agent = agents.create_agent(org.id, owner.id, {"name": "Dispatch agent", "host_id": host_id})
    host_app = create_host_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "host-state",
        )
    )
    host_app.state.dispatch_store = DispatchStore(host_app.state.host_store)
    host_app.state.dispatch_store.initialize()
    host_app.state.dispatcher = Dispatcher(
        host_app.state.dispatch_store, host_app.state.host_runtime
    )
    host_app.include_router(host_dispatch_router)
    host_store = host_app.state.host_store
    host_store.bind_organization(org.id, host_token)
    host_store.stage_agent(
        HostAgentConfiguration(
            host_id=host_store.instance_id,
            organization_id=org.id,
            agent_id=agent["id"],
            version=1,
            name=agent["name"],
        )
    )
    host_store.save_session(org.id, agent["id"], "ses_primary", "/workspace/primary", "Primary")
    host_store.save_session(org.id, agent["id"], "ses_other", "/workspace/other", "Other")
    control.add_member(
        org.id,
        "member",
        "Dispatch Member",
        "a separate member password",
        "member",
        actor_id=owner.id,
    )
    other = control.create_organization(owner.id, "Other organization")
    control_app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    control_app.include_router(control_dispatch_routes.router)
    host_client = HostClient(agents, transport=httpx.ASGITransport(app=host_app))
    monkeypatch.setattr(control_dispatch_routes, "host_client", lambda _: host_client)

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
            httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers=headers) as anonymous,
        ):
            owner_user = await _sign_in(owner_client, "owner", "correct horse battery staple")
            member_user = await _sign_in(member_client, "member", "a separate member password")
            path = f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_primary/dispatches"
            delivery_id = str(uuid4())
            body = {"id": delivery_id, "text": "Inspect the current request."}

            assert (await anonymous.post(path, json=body)).status_code == 401
            forged = await member_client.post(
                path,
                json={
                    **body,
                    "author": {"kind": "human", "id": str(uuid4()), "name": "Forged"},
                },
            )
            assert forged.status_code == 422

            created = await member_client.post(path, json=body)
            assert created.status_code == 202
            receipt = created.json()
            assert receipt["state"] == "queued"
            assert receipt["author"] == {
                "kind": "human",
                "id": member_user["id"],
                "name": "Dispatch Member",
                "session_id": None,
            }
            assert host_app.state.dispatcher.changed.is_set()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host_app),
                base_url="http://host",
                headers={"Authorization": f"Bearer {host_token}"},
            ) as host_client:
                agent_delivery = await host_client.post(
                    path,
                    json={
                        "id": str(uuid4()),
                        "text": "Trusted peer instruction.",
                        "author": {
                            "kind": "agent",
                            "id": str(uuid4()),
                            "name": "Peer agent",
                        },
                    },
                )
            assert agent_delivery.status_code == 202
            assert agent_delivery.json()["author"]["kind"] == "agent"

            repeated = await member_client.post(path, json=body)
            assert repeated.status_code == 202 and repeated.json() == receipt
            conflict = await member_client.post(path, json={**body, "text": "Different request."})
            assert conflict.status_code == 409

            listed = await owner_client.get(path)
            assert listed.status_code == 200 and listed.json() == [receipt, agent_delivery.json()]
            detail = await owner_client.get(f"{path}/{delivery_id}")
            assert detail.status_code == 200 and detail.json() == receipt
            assert (
                await owner_client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_other/dispatches/{delivery_id}"
                )
            ).status_code == 404

            stopped = await owner_client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_primary/stop",
                json={"id": str(uuid4()), "cancel_queued": True},
            )
            assert stopped.status_code == 202
            assert stopped.json()["payload"]["mode"] == "stop"
            assert stopped.json()["payload"]["cancel_queued"] is True
            assert stopped.json()["author"]["id"] == owner_user["id"]

            assert (
                await member_client.get(
                    f"/organizations/{other.id}/agents/{agent['id']}/sessions/ses_primary/dispatches"
                )
            ).status_code == 404
            assert (
                await member_client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses.invalid/dispatches"
                )
            ).status_code == 422

    asyncio.run(check())


def test_explicit_reconcile_reads_recovery_history_without_admitting_queued_work(tmp_path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "host-state",
    )
    app = create_host_app(settings)
    store = app.state.host_store
    organization_id, agent_id = str(uuid4()), str(uuid4())
    binding = secrets.token_urlsafe(32)
    store.bind_organization(organization_id, binding)
    envelope = HostAgentConfiguration(
        host_id=store.instance_id,
        organization_id=organization_id,
        agent_id=agent_id,
        version=1,
        name="Recovery agent",
    )
    store.stage_agent(envelope)
    store.mark_applied(envelope)
    store.save_session(organization_id, agent_id, "ses_recovery", "/workspace/recovery", "Recovery")
    dispatches = DispatchStore(store)
    dispatches.initialize()
    owner = Actor(kind="human", id=uuid4(), name="Recovery owner")
    delivered = dispatches.enqueue(
        organization_id,
        agent_id,
        "ses_recovery",
        Submission(id=uuid4(), text="Already admitted"),
        owner,
    )
    assert dispatches.change(delivered, "submitting", message_id="msg_admitted")
    admitted = dispatches.get(organization_id, agent_id, delivered["id"])
    assert dispatches.change(admitted, "uncertain", error="Response unavailable")
    queued = dispatches.enqueue(
        organization_id,
        agent_id,
        "ses_recovery",
        Submission(id=uuid4(), text="Must remain queued"),
        owner,
    )
    store.set_lifecycle_state(organization_id, agent_id, state="recovery_required")

    class RecoveryNative:
        def __init__(self):
            self.locks = {}

        def lock(self, agent_id):
            return self.locks.setdefault(agent_id, asyncio.Lock())

        async def request(
            self, organization_id, agent_id, path, *, method="GET", body=None, directory=None
        ):
            assert method == "GET"
            if path == "/session/status":
                return {}
            if path == "/session/ses_recovery/message":
                return [
                    {
                        "info": {
                            "id": "msg_admitted",
                            "sessionID": "ses_recovery",
                            "role": "user",
                        },
                        "parts": [{"type": "text", "text": "Already admitted"}],
                    },
                    {
                        "info": {
                            "id": "msg_result",
                            "sessionID": "ses_recovery",
                            "role": "assistant",
                            "parentID": "msg_admitted",
                            "finish": "stop",
                            "time": {"completed": 1},
                        },
                        "parts": [{"type": "text", "text": "Completed"}],
                    },
                ]
            raise AssertionError((path, method, directory))

    native = RecoveryNative()
    app.state.dispatch_store = dispatches
    app.state.dispatcher = Dispatcher(dispatches, native)

    async def reconcile():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://host",
            headers={"Authorization": f"Bearer {binding}"},
        ) as client:
            path = (
                f"/organizations/{organization_id}/agents/{agent_id}/sessions/ses_recovery/"
                f"dispatches/{delivered['id']}/reconcile"
            )
            response = await client.post(path)
            assert response.status_code == 200
            return response.json()

    reconciled = asyncio.run(reconcile())

    assert reconciled["state"] == "completed"
    assert reconciled["receipt_validated"] is True
    preserved = dispatches.get(organization_id, agent_id, queued["id"])
    assert preserved["state"] == "queued"
    assert preserved["native_message_id"] is None
    assert store.agent_status(organization_id, agent_id)["lifecycle_state"] == ("recovery_required")


async def _sign_in(client: httpx.AsyncClient, login: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]
