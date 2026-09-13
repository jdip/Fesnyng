import asyncio
import secrets
from uuid import uuid4

import httpx
import pytest

from fesnyng_backend import control_memory_routes
from fesnyng_backend.agent_host import create_app as create_host_app
from fesnyng_backend.control_plane import create_app as create_control_app
from fesnyng_backend.host_client import HostClient, HostRejected, HostUnavailable
from fesnyng_backend.host_memory_routes import router as host_memory_router
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings

ORIGIN = "https://control.example"


def test_control_memory_routes_authorize_writes_and_preserve_host_conflicts(
    organization, monkeypatch, tmp_path
):
    settings, control, owner, org, agents, host_id = organization
    host_token = secrets.token_urlsafe(32)
    agents.set_host_credential(org.id, host_id, host_token)
    agent = agents.create_agent(org.id, owner.id, {"name": "Memory agent", "host_id": host_id})
    host_app = create_host_app(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.sqlite3",
            state_directory=tmp_path / "host-state",
        )
    )
    host_app.include_router(host_memory_router)
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
    control.add_member(
        org.id,
        "member",
        "Memory Member",
        "a separate member password",
        "member",
        actor_id=owner.id,
    )
    other = control.create_organization(owner.id, "Other organization")
    control_app = create_control_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    control_app.include_router(control_memory_routes.router)
    client = HostClient(agents, transport=httpx.ASGITransport(app=host_app))
    monkeypatch.setattr(control_memory_routes, "host_client", lambda _: client)

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
            await _sign_in(owner_client, "owner", "correct horse battery staple")
            member = await _sign_in(member_client, "member", "a separate member password")
            path = f"/organizations/{org.id}/agents/{agent['id']}/memory"
            assert (await anonymous.get(path)).status_code == 401
            assert (await member_client.get(path)).json() == []
            forged = await member_client.put(
                path,
                json={
                    "key": "context",
                    "content": "Must not be written",
                    "expected_revision": 0,
                    "author": {"kind": "human", "id": str(uuid4()), "name": "Forged"},
                },
            )
            assert forged.status_code == 422
            created = await member_client.put(
                path,
                json={"key": "context", "content": "Member context", "expected_revision": 0},
            )
            assert created.status_code == 200
            assert created.json()["author"] == {
                "kind": "human",
                "id": member["id"],
                "name": "Memory Member",
                "session_id": None,
            }
            updated = await owner_client.put(
                path,
                json={"key": "context", "content": "Owner context", "expected_revision": 1},
            )
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            stale = await member_client.put(
                path,
                json={"key": "context", "content": "Stale context", "expected_revision": 1},
            )
            assert stale.status_code == 409
            assert stale.json() == {"detail": "Agent host rejected the request"}
            assert (
                await member_client.get(f"/organizations/{other.id}/agents/{agent['id']}/memory")
            ).status_code == 404
            detail = await member_client.get(f"{path}/context")
            assert detail.status_code == 200 and detail.json()["content"] == "Owner context"

    asyncio.run(check())


async def _sign_in(client: httpx.AsyncClient, login: str, password: str) -> dict[str, str]:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]


@pytest.mark.parametrize("status_code", [400, 403, 404, 409, 422])
def test_host_client_preserves_safe_host_rejections(organization, status_code):
    _, _, _, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))
    secret = "native-host-private-detail"
    client = HostClient(
        agents,
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, text=secret)),
    )

    async def request():
        with pytest.raises(HostRejected) as rejected:
            await client.request(org.id, host_id, "/agents/example/memory")
        assert rejected.value.status_code == status_code
        assert str(rejected.value) == "Agent host rejected the request"
        assert secret not in str(rejected.value)

    asyncio.run(request())


@pytest.mark.parametrize("failure", ["unauthorized", "unreachable"])
def test_host_client_maps_unavailable_failures_to_service_unavailable(organization, failure):
    _, _, _, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, secrets.token_urlsafe(32))

    def host(request: httpx.Request) -> httpx.Response:
        if failure == "unreachable":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(401, text="private authentication detail")

    client = HostClient(agents, transport=httpx.MockTransport(host))

    async def request():
        with pytest.raises(HostUnavailable) as unavailable:
            await client.request(org.id, host_id, "/agents/example/memory")
        assert type(unavailable.value) is HostUnavailable
        assert "private authentication detail" not in str(unavailable.value)
        if failure == "unreachable":
            assert str(unavailable.value) == "Agent host is unreachable"
        else:
            assert str(unavailable.value) == "Agent host operation failed (HTTP 401)"

    asyncio.run(request())
