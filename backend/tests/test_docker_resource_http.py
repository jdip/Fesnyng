"""Resource actions authenticate organization membership and actor provenance."""

import asyncio
import json
from uuid import uuid4

import httpx

from fesnyng_backend import control_host_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.settings import ControlPlaneSessionSettings


def test_docker_routes_enforce_membership_csrf_host_and_project_scope(organization, monkeypatch):
    settings, control, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, "x" * 32)
    control.add_member(
        org.id, "member", "Member", "correct horse battery staple", "member", actor_id=owner.id
    )
    calls = []

    def boundary(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        control_host_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(boundary)),
    )
    app = create_app(
        settings, ControlPlaneSessionSettings(allowed_origin="https://resource.example")
    )

    async def exercise():
        path = f"/organizations/{org.id}/hosts/{host_id}/docker"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://resource.example",
            headers={"Origin": "https://resource.example"},
        ) as client:
            assert (await client.get(path)).status_code == 401
            login = await client.post(
                "/auth/login", json={"login": "member", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            assert (await client.get(path)).status_code == 200
            client.headers["X-Fesnyng-Actor"] = json.dumps(
                {"kind": "human", "id": str(uuid4()), "name": "Forged"}
            )
            body = {
                "container_id": "a" * 64,
                "name": "Shared app",
                "threads": [],
                "project_ids": [],
            }
            assert (await client.post(path + "/resources", json=body)).status_code == 200
            assert json.loads(calls[-1].headers["X-Fesnyng-Actor"])["name"] == "Member"
            count = len(calls)
            assert (await client.get(path.replace(str(host_id), str(uuid4())))).status_code == 404
            assert (
                await client.post(path + "/resources", json={**body, "project_ids": [str(uuid4())]})
            ).status_code == 404
            assert (await client.get(path.replace(org.id, str(uuid4())))).status_code == 404
            assert (
                await client.post(path + "/resources", json={**body, "socket": "/arbitrary"})
            ).status_code == 422
            del client.headers["X-CSRF-Token"]
            assert (await client.post(path + "/resources", json=body)).status_code == 403
            assert len(calls) == count

    asyncio.run(exercise())


def test_host_resource_routes_require_binding_and_actor(tmp_path):
    from fesnyng_backend.agent_host import create_app as create_host
    from fesnyng_backend.settings import ServiceSettings

    app = create_host(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    org = str(uuid4())
    app.state.host_store.bind_organization(org, "a" * 32)

    async def exercise():
        path = f"/organizations/{org}/docker"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            assert (await client.get(path)).status_code == 401
            client.headers["Authorization"] = "Bearer " + "a" * 32
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json()["capability"]["enabled"] is False
            assert response.json()["resources"] == []
            assert (await client.get(path.replace(org, str(uuid4())))).status_code == 403
            body = {"name": "Example", "container_id": "a" * 64}
            assert (await client.post(path + "/resources", json=body)).status_code == 400
            client.headers["X-Fesnyng-Actor"] = "not json"
            assert (await client.post(path + "/resources", json=body)).status_code == 400
            client.headers["X-Fesnyng-Actor"] = json.dumps(
                {"kind": "human", "id": str(uuid4()), "name": "Member"}
            )
            assert (await client.post(path + "/resources", json=body)).status_code in {403, 503}

    asyncio.run(exercise())


def test_host_service_routes_keep_employee_services_when_raw_docker_is_disabled(tmp_path):
    from fesnyng_backend.agent_host import create_app as create_host
    from fesnyng_backend.host_models import HostAgentConfiguration
    from fesnyng_backend.settings import ServiceSettings

    app = create_host(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    org, employee = str(uuid4()), str(uuid4())
    app.state.host_store.bind_organization(org, "a" * 32)
    app.state.host_store.stage_agent(
        HostAgentConfiguration(
            host_id=app.state.host_store.instance_id,
            organization_id=org,
            agent_id=employee,
            version=1,
            name="Engineer",
        )
    )

    async def exercise():
        path = f"/organizations/{org}/docker/services"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            body = {
                "name": "Preview",
                "target_kind": "employee",
                "target_id": employee,
                "endpoint_url": "http://127.0.0.1:8081",
                "route": "custom",
            }
            assert (await client.post(path, json=body)).status_code == 400
            client.headers["X-Fesnyng-Actor"] = json.dumps(
                {"kind": "human", "id": str(uuid4()), "name": "Member"}
            )
            reply = await client.post(path, json=body)
            assert reply.status_code == 200
            item = reply.json()
            assert item["target"]["status"] in {"missing", "unavailable"}
            assert item["route_status"]["network_reachability"] == "unverified"
            assert (
                await client.post(f"{path}/{item['id']}/unregister", json={"expected_revision": 2})
            ).status_code == 409
            assert (
                await client.post(f"{path}/{item['id']}/unregister", json={"expected_revision": 1})
            ).json() == {"removed": True}

    asyncio.run(exercise())


def test_control_service_update_keeps_a_retained_missing_employee_target_editable(
    organization, monkeypatch
):
    settings, control, owner, org, agents, host_id = organization
    agents.set_host_credential(org.id, host_id, "x" * 32)
    control.add_member(
        org.id, "member", "Member", "correct horse battery staple", "member", actor_id=owner.id
    )
    service_id, retained_employee = str(uuid4()), str(uuid4())

    def boundary(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": service_id,
                    "target_kind": "employee",
                    "target_id": retained_employee,
                    "threads": [],
                    "project_ids": [],
                },
            )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        control_host_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(boundary)),
    )
    app = create_app(
        settings, ControlPlaneSessionSettings(allowed_origin="https://resource.example")
    )

    async def exercise():
        path = f"/organizations/{org.id}/hosts/{host_id}/docker/services/{service_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://resource.example",
            headers={"Origin": "https://resource.example"},
        ) as client:
            login = await client.post(
                "/auth/login", json={"login": "member", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            retained = {
                "name": "Retained",
                "target_kind": "employee",
                "target_id": retained_employee,
                "endpoint_url": "http://127.0.0.1:8081",
                "route": "custom",
                "threads": [],
                "project_ids": [],
                "expected_revision": 1,
            }
            assert (await client.put(path, json=retained)).status_code == 200
            replacement = {**retained, "target_id": str(uuid4())}
            assert (await client.put(path, json=replacement)).status_code == 404

    asyncio.run(exercise())
