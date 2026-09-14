import asyncio
import secrets
from uuid import uuid4

import httpx

from fesnyng_backend import control_thread_acknowledgement_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://workspace.example"


def test_codex_completed_turn_has_a_stable_read_acknowledgement_identity():
    assert (
        control_thread_acknowledgement_routes._outcome_id(
            {
                "state": "completed",
                "outcome": {"kind": "codex_turn_completed", "turn_id": "turn_42"},
            },
            "read",
            "delivery",
        )
        == "codex:turn_42"
    )


def test_thread_acknowledgements_are_personal_and_durable(organization, monkeypatch):
    settings, control, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    agent = agents.create_agent(org.id, owner.id, {"name": "Workspace", "host_id": host_id})
    member = control.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    other_org = control.create_organization(owner.id, "Other organization")
    read_id, resolution_id, failed_id, resolved_failure_id, mismatch_id = (
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )
    resolution = str(uuid4())

    def native(request: httpx.Request) -> httpx.Response:
        delivery_id = request.url.path.rsplit("/", 1)[-1]
        receipts = {
            read_id: {
                "id": read_id,
                "session_id": "ses_native",
                "state": "completed",
                "outcome": {"kind": "native_run_completed", "message_id": "msg_result"},
            },
            resolution_id: {
                "id": resolution_id,
                "session_id": "ses_resolution",
                "state": "completed",
                "outcome": {
                    "kind": "operator_resolution",
                    "operation_id": resolution,
                    "outcome": "completed",
                },
            },
            failed_id: {
                "id": failed_id,
                "session_id": "ses_failed",
                "state": "failed",
                "outcome": {"kind": "native_command_unavailable"},
            },
            resolved_failure_id: {
                "id": resolved_failure_id,
                "session_id": "ses_resolved_failure",
                "state": "failed",
                "outcome": {
                    "kind": "operator_resolution",
                    "operation_id": str(uuid4()),
                    "outcome": "failed",
                },
            },
            mismatch_id: {
                "id": str(uuid4()),
                "session_id": "ses_mismatch",
                "state": "completed",
                "outcome": {"kind": "native_run_completed", "message_id": "msg_wrong"},
            },
        }
        receipt = receipts.get(delivery_id)
        return httpx.Response(200, json=receipt) if receipt else httpx.Response(404)

    monkeypatch.setattr(
        control_thread_acknowledgement_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(native)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def login(client: httpx.AsyncClient, name: str):
        response = await client.post(
            "/auth/login", json={"login": name, "password": "correct horse battery staple"}
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    async def exercise():
        path = f"/organizations/{org.id}/agents/{agent['id']}/thread-acknowledgements"
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
            await login(owner_device, "owner")
            await login(member_device, "member")
            acknowledgements = await owner_device.get(path)
            assert acknowledgements.status_code == 200
            assert acknowledgements.json() == {"acknowledgements": []}
            no_csrf = await owner_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_native/acknowledgements",
                headers={"X-CSRF-Token": "missing"},
                json={"delivery_id": read_id, "kind": "read", "outcome_id": "native:msg_result"},
            )
            assert no_csrf.status_code == 403

            read = await owner_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_native/acknowledgements",
                json={"delivery_id": read_id, "kind": "read", "outcome_id": "native:msg_result"},
            )
            assert read.status_code == 200
            assert read.json() == {
                "acknowledgements": [
                    {
                        "session_id": "ses_native",
                        "delivery_id": read_id,
                        "kind": "read",
                        "outcome_id": "native:msg_result",
                    }
                ]
            }
            assert (
                await owner_device.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_native/acknowledgements",
                    json={
                        "delivery_id": read_id,
                        "kind": "read",
                        "outcome_id": "native:msg_result",
                    },
                )
            ).json() == read.json()
            await login(owner_second_device, "owner")
            assert (await owner_second_device.get(path)).json() == read.json()

            assert (await member_device.get(path)).json() == {"acknowledgements": []}
            member_read = await member_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_native/acknowledgements",
                json={"delivery_id": read_id, "kind": "read", "outcome_id": "native:msg_result"},
            )
            assert member_read.status_code == 200
            assert (await owner_device.get(path)).json() == read.json()

            resolution_read = await owner_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_resolution/acknowledgements",
                json={
                    "delivery_id": resolution_id,
                    "kind": "read",
                    "outcome_id": f"resolution:{resolution}",
                },
            )
            assert resolution_read.status_code == 200
            failure_handled = await owner_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_failed/acknowledgements",
                json={
                    "delivery_id": failed_id,
                    "kind": "failure_handled",
                    "outcome_id": f"failed:{failed_id}",
                },
            )
            assert failure_handled.status_code == 200
            assert {
                entry["outcome_id"] for entry in failure_handled.json()["acknowledgements"]
            } == {
                "native:msg_result",
                f"resolution:{resolution}",
                f"failed:{failed_id}",
            }

            stale = await owner_device.post(
                f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_native/acknowledgements",
                json={"delivery_id": read_id, "kind": "read", "outcome_id": "native:stale"},
            )
            assert stale.status_code == 409
            assert (
                await owner_device.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_failed/acknowledgements",
                    json={
                        "delivery_id": failed_id,
                        "kind": "read",
                        "outcome_id": f"failed:{failed_id}",
                    },
                )
            ).status_code == 409
            assert (
                await owner_device.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_resolved_failure/acknowledgements",
                    json={
                        "delivery_id": resolved_failure_id,
                        "kind": "failure_handled",
                        "outcome_id": f"failed:{resolved_failure_id}",
                    },
                )
            ).status_code == 409
            assert (
                await owner_device.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_mismatch/acknowledgements",
                    json={
                        "delivery_id": mismatch_id,
                        "kind": "read",
                        "outcome_id": "native:msg_wrong",
                    },
                )
            ).status_code == 404
            assert (
                await owner_device.post(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/ses_wrong/acknowledgements",
                    json={
                        "delivery_id": read_id,
                        "kind": "read",
                        "outcome_id": "native:msg_result",
                    },
                )
            ).status_code == 404
            assert (
                await owner_device.get(
                    f"/organizations/{other_org.id}/agents/{agent['id']}/thread-acknowledgements"
                )
            ).status_code == 404
            assert (
                await owner_device.get(
                    f"/organizations/{org.id}/agents/{uuid4()}/thread-acknowledgements"
                )
            ).status_code == 404

    asyncio.run(exercise())
    control.remove_member(org.id, member.user_id, actor_id=owner.id)
    assert control.list_thread_acknowledgements(org.id, member.user_id, agent["id"]) == []
    assert control.list_thread_acknowledgements(org.id, owner.id, agent["id"])
