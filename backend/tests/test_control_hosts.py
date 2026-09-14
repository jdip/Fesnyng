import asyncio
import json
from typing import Any

import httpx
import pytest

from fesnyng_backend import agents as agent_routes
from fesnyng_backend import control_host_routes
from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient, HostResponse, HostUnavailable
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://control.example"


def _configured_agent(organization) -> tuple[AgentStore, str, dict[str, Any], str]:
    _, _, owner, org, agents, host_id = organization
    host_token = "host-binding-token-that-is-never-returned-to-callers"
    agents.set_host_credential(org.id, host_id, host_token)
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    return agents, host_id, agent, host_token


def _acknowledgement(agent: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "host_id": agent["host_id"],
        "organization_id": agent["organization_id"],
        "agent_id": agent["id"],
        "applied_version": agent["desired_version"],
        "applied_policy_version": policy["desired_version"],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_id", "wrong-host"),
        ("organization_id", "wrong-organization"),
        ("agent_id", "wrong-agent"),
        ("applied_version", 99),
        ("applied_policy_version", 99),
    ],
)
def test_apply_rejects_acknowledgements_for_another_configuration(
    organization, field: str, value: object
) -> None:
    async def exercise() -> None:
        agents, _, agent, _ = _configured_agent(organization)
        policy = agents.get_policy(agent["organization_id"])

        def host(_: httpx.Request) -> httpx.Response:
            reply = _acknowledgement(agent, policy)
            reply[field] = value
            return httpx.Response(200, json=reply)

        transport = httpx.MockTransport(host)
        async with httpx.AsyncClient(transport=transport):
            with pytest.raises(HostUnavailable, match="acknowledgement"):
                await HostClient(agents, transport=transport).apply(
                    agent["organization_id"], agent["id"]
                )

        assert agents.get_agent(agent["organization_id"], agent["id"])["applied_version"] is None

    asyncio.run(exercise())


def test_apply_keeps_newer_desired_configuration_pending_and_redacts_host_token(
    organization,
) -> None:
    async def exercise() -> None:
        agents, _, agent, host_token = _configured_agent(organization)
        policy = agents.get_policy(agent["organization_id"])

        def host(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {host_token}"
            agents.update_agent(
                agent["organization_id"],
                agent["id"],
                organization[2].id,
                {"expected_version": agent["desired_version"], "name": "Principal Engineer"},
            )
            return httpx.Response(200, json=_acknowledgement(agent, policy))

        transport = httpx.MockTransport(host)
        async with httpx.AsyncClient(transport=transport):
            result = await HostClient(agents, transport=transport).apply(
                agent["organization_id"], agent["id"]
            )

        assert result["desired_version"] == 2
        assert result["applied_version"] == 1
        assert result["configuration_status"] == "pending"
        assert host_token not in json.dumps(result)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["failed", "unreachable", "malformed"])
def test_apply_maps_host_failures_to_clear_unavailability(organization, kind: str) -> None:
    async def exercise() -> None:
        agents, _, agent, _ = _configured_agent(organization)

        def host(request: httpx.Request) -> httpx.Response:
            if kind == "failed":
                return httpx.Response(502)
            if kind == "unreachable":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, content=b"not json")

        transport = httpx.MockTransport(host)
        async with httpx.AsyncClient(transport=transport):
            with pytest.raises(HostUnavailable):
                await HostClient(agents, transport=transport).apply(
                    agent["organization_id"], agent["id"]
                )
        assert agents.get_agent(agent["organization_id"], agent["id"])["applied_version"] is None

    asyncio.run(exercise())


def test_apply_route_maps_invalid_host_json_to_service_unavailable(
    organization, monkeypatch
) -> None:
    async def exercise() -> None:
        settings, _, owner, org, agents, host_id = organization
        host_token = "host-binding-token-that-is-never-returned-to-callers"
        agents.set_host_credential(org.id, host_id, host_token)
        agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
        host = HostClient(
            agents,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not json")),
        )
        monkeypatch.setattr(control_host_routes, "host_client", lambda _: host)
        app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            response = await client.post(f"/organizations/{org.id}/agents/{agent['id']}/apply")

        assert response.status_code == 503
        assert "invalid JSON" in response.json()["detail"]

    asyncio.run(exercise())


def test_harness_switch_selects_target_only_after_verified_host_freeze(organization, monkeypatch):
    async def exercise() -> None:
        settings, _, owner, org, agents, host_id = organization
        agents.set_host_credential(org.id, host_id, "host-binding-token-that-is-never-returned")
        agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})

        class FrozenHost:
            async def raw_request(self, organization_id, host, path, *, method="GET", body=None):
                assert organization_id == org.id and host == host_id
                assert path == f"/agents/{agent['id']}/harness-switch"
                assert method == "POST"
                assert body == {"expected_version": 1, "target_runtime_type": "codex"}
                return HostResponse(
                    200,
                    json.dumps(
                        {
                            "organization_id": org.id,
                            "agent_id": agent["id"],
                            "switch_state": "frozen",
                            "switch_source_version": 1,
                            "switch_target_runtime": "codex",
                        }
                    ).encode(),
                    "application/json",
                )

        monkeypatch.setattr(agent_routes, "host_client", lambda _: FrozenHost())
        app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            response = await client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/harness-switch",
                json={"expected_version": 1, "target_runtime_type": "codex"},
            )

        assert response.status_code == 202
        body = response.json()
        assert body["switch_state"] == "pending_apply"
        assert body["agent"]["configuration_status"] == "pending"
        assert body["agent"]["configuration"]["runtime_type"] == "codex"
        assert agents.harness_switch(org.id, agent["id"]) is None

    asyncio.run(exercise())


def test_harness_switch_capture_rejection_keeps_original_selection_and_writable_intent(
    organization, monkeypatch
):
    async def exercise() -> None:
        settings, _, owner, org, agents, host_id = organization
        agents.set_host_credential(org.id, host_id, "host-binding-token-that-is-never-returned")
        agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})

        class RejectedHost:
            async def raw_request(self, organization_id, host, path, *, method="GET", body=None):
                assert organization_id == org.id and host == host_id
                if path.endswith("/harness-switch"):
                    return HostResponse(
                        409, b'{"detail":"Active work must finish first"}', "application/json"
                    )
                assert path == f"/agents/{agent['id']}" and method == "GET"
                return HostResponse(200, b'{"switch_state":null}', "application/json")

        monkeypatch.setattr(agent_routes, "host_client", lambda _: RejectedHost())
        app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            response = await client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/harness-switch",
                json={"expected_version": 1, "target_runtime_type": "codex"},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "Active work must finish first"
        assert agents.get_agent(org.id, agent["id"])["configuration"]["runtime_type"] == "opencode"
        assert agents.harness_switch(org.id, agent["id"]) is None

    asyncio.run(exercise())


async def _sign_in(client: httpx.AsyncClient, login: str, password: str) -> None:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def test_runtime_routes_deny_unauthorized_requests_before_host_network(organization) -> None:
    async def exercise() -> None:
        settings, control, owner, org, agents, host_id = organization
        agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
        profile = agents.create_profile(org.id, owner.id, {"name": "Shared"})
        control.add_member(
            org.id,
            "member",
            "Member",
            "a separate member password",
            "member",
            actor_id=owner.id,
        )
        other = control.create_organization(owner.id, "Other organization")
        app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
        transport = httpx.ASGITransport(app=app)
        headers = {"Origin": ORIGIN}
        async with (
            httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as owner_client,
            httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, headers=headers
            ) as member_client,
        ):
            await _sign_in(owner_client, "owner", "correct horse battery staple")
            await _sign_in(member_client, "member", "a separate member password")
            member_csrf = member_client.headers["X-CSRF-Token"]
            apply_path = f"/organizations/{org.id}/agents/{agent['id']}/apply"
            lifecycle_path = f"/organizations/{org.id}/agents/{agent['id']}/lifecycle"
            session_path = f"/organizations/{org.id}/agents/{agent['id']}/sessions"
            login_path = f"/organizations/{org.id}/hosts/{host_id}/profiles/{profile['id']}/login"
            assert (await member_client.post(apply_path)).status_code == 403
            assert (
                await member_client.post(
                    lifecycle_path, json={"action": "start", "confirmed": False}
                )
            ).status_code == 403
            assert (await member_client.post(login_path)).status_code == 403
            member_client.headers.pop("X-CSRF-Token")
            assert (
                await member_client.post(session_path, json={"title": "Thread"})
            ).status_code == 403
            member_client.headers["X-CSRF-Token"] = member_csrf
            assert (
                await member_client.post(
                    f"/organizations/{other.id}/agents/{agent['id']}/sessions",
                    json={"title": "Thread"},
                )
            ).status_code == 404
            owner_client.headers.pop("X-CSRF-Token")
            assert (await owner_client.post(apply_path)).status_code == 403

    asyncio.run(exercise())


def test_manager_lifecycle_action_forwards_authenticated_actor(organization, monkeypatch) -> None:
    async def exercise() -> None:
        settings, _, owner, org, agents, host_id = organization
        agents.set_host_credential(org.id, host_id, "host-binding-token-that-is-never-returned")
        agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
        forwarded = {}

        def host(request: httpx.Request) -> httpx.Response:
            forwarded.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "agent_id": agent["id"],
                    "desired_state": "running",
                    "lifecycle_state": "running",
                    "container_state": "running",
                    "confirmation_required": True,
                    "message": "Confirm stop for this agent.",
                },
            )

        client = HostClient(agents, transport=httpx.MockTransport(host))
        monkeypatch.setattr(control_host_routes, "host_client", lambda _: client)
        app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        ) as browser:
            await _sign_in(browser, "owner", "correct horse battery staple")
            response = await browser.post(
                f"/organizations/{org.id}/agents/{agent['id']}/lifecycle",
                json={"action": "stop", "confirmed": False},
            )

        assert response.status_code == 200
        assert forwarded["action"] == "stop" and forwarded["confirmed"] is False
        assert forwarded["author"] == {
            "kind": "human",
            "id": owner.id,
            "name": owner.display_name,
            "session_id": None,
        }

    asyncio.run(exercise())
