import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://control.example"


async def _sign_in(client: AsyncClient, login: str, password: str) -> None:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def _department_management(organization) -> None:
    settings, control, owner, organization_record, _, host_id = organization
    other_organization = control.create_organization(owner.id, "Other organization")
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    base = f"/organizations/{organization_record.id}"

    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as owner_client:
        await _sign_in(owner_client, "owner", "correct horse battery staple")
        csrf_token = owner_client.headers["X-CSRF-Token"]
        member = await owner_client.put(
            f"{base}/members",
            json={
                "login": "member",
                "display_name": "Member",
                "password": "a separate member password",
                "role": "member",
            },
        )
        assert member.status_code == 200
        owner_client.headers.pop("X-CSRF-Token")
        assert (
            await owner_client.post(f"{base}/departments", json={"name": "Forged"})
        ).status_code == 403
        owner_client.headers["X-CSRF-Token"] = csrf_token

        async with AsyncClient(
            transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as member_client:
            await _sign_in(member_client, "member", "a separate member password")
            assert (await member_client.get(f"{base}/departments")).json() == []
            assert (
                await member_client.post(f"{base}/departments", json={"name": "Forbidden"})
            ).status_code == 403

        chief = await owner_client.post(
            f"{base}/agents", json={"name": "Chief", "host_id": host_id}
        )
        assert chief.status_code == 201
        product = await owner_client.post(
            f"{base}/departments", json={"name": "Product", "head_agent_id": chief.json()["id"]}
        )
        assert product.status_code == 201
        platform = await owner_client.post(
            f"{base}/departments",
            json={"name": "Platform", "parent_id": product.json()["id"]},
        )
        assert platform.status_code == 201

        engineer = await owner_client.post(
            f"{base}/agents",
            json={
                "name": "Engineer",
                "host_id": host_id,
                "reports_to_agent_id": chief.json()["id"],
                "department_id": platform.json()["id"],
            },
        )
        assert engineer.status_code == 201
        assert engineer.json()["department_id"] == platform.json()["id"]
        assert engineer.json()["reports_to_agent_id"] == chief.json()["id"]

        renamed = await owner_client.patch(
            f"{base}/departments/{platform.json()['id']}",
            json={"name": "Core Platform", "head_agent_id": engineer.json()["id"]},
        )
        assert renamed.status_code == 200
        assert renamed.json() == {
            "id": platform.json()["id"],
            "organization_id": organization_record.id,
            "name": "Core Platform",
            "parent_id": product.json()["id"],
            "head_agent_id": engineer.json()["id"],
        }
        assert (
            await owner_client.patch(
                f"{base}/departments/{platform.json()['id']}", json={"name": None}
            )
        ).status_code == 422
        assert (
            await owner_client.patch(
                f"{base}/departments/{product.json()['id']}",
                json={"parent_id": platform.json()["id"]},
            )
        ).status_code == 422
        assert (
            await owner_client.delete(f"{base}/departments/{product.json()['id']}")
        ).status_code == 422
        assert (
            await owner_client.delete(f"{base}/departments/{platform.json()['id']}")
        ).status_code == 422

        unassigned = await owner_client.patch(
            f"{base}/agents/{engineer.json()['id']}",
            json={"expected_version": 1, "department_id": None},
        )
        assert unassigned.status_code == 200
        assert unassigned.json()["department_id"] is None
        assert (
            await owner_client.delete(f"{base}/departments/{platform.json()['id']}")
        ).status_code == 204
        assert (
            await owner_client.delete(f"{base}/departments/{product.json()['id']}")
        ).status_code == 204

        foreign = await owner_client.post(
            f"/organizations/{other_organization.id}/departments", json={"name": "Foreign"}
        )
        assert foreign.status_code == 201
        assert (
            await owner_client.post(
                f"{base}/departments", json={"name": "Invalid", "parent_id": foreign.json()["id"]}
            )
        ).status_code == 422
        assert (
            await owner_client.post(
                f"/organizations/{other_organization.id}/departments",
                json={"name": "Invalid head", "head_agent_id": chief.json()["id"]},
            )
        ).status_code == 422
        assert (
            await owner_client.patch(
                f"{base}/agents/{engineer.json()['id']}",
                json={"expected_version": 2, "department_id": foreign.json()["id"]},
            )
        ).status_code == 422
        assert (
            await owner_client.get(
                f"/organizations/{other_organization.id}/departments/{product.json()['id']}"
            )
        ).status_code == 404
        assert (await owner_client.get(f"{base}/departments/{uuid4()}")).status_code == 404


def test_department_management_preserves_organization_and_reporting_boundaries(organization):
    asyncio.run(_department_management(organization))


async def _migrated_agent_remains_unassigned(organization) -> None:
    settings, control, owner, organization_record, _, host_id = organization
    agent_id = str(uuid4())
    with control.connect() as connection:
        connection.execute("DROP TABLE agents")
        connection.execute(
            """
            CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                host_id TEXT NOT NULL,
                reports_to_agent_id TEXT,
                desired_version INTEGER NOT NULL,
                applied_version INTEGER,
                UNIQUE (organization_id, id)
            )
            """
        )
        connection.execute(
            "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
            (agent_id, organization_record.id, "Existing", "", host_id, None, 1, None),
        )
        connection.execute(
            "INSERT INTO agent_configurations(agent_id,version,configuration,created_by) VALUES(?,?,?,?)",
            (agent_id, 1, AgentConfiguration().model_dump_json(), owner.id),
        )
        connection.execute("UPDATE control_plane_schema SET schema_version=1 WHERE singleton=1")

    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client, "owner", "correct horse battery staple")
        response = await client.get(f"/organizations/{organization_record.id}/agents/{agent_id}")
    assert response.status_code == 200
    assert response.json()["department_id"] is None


def test_existing_agents_migrate_without_department_assignment(organization):
    asyncio.run(_migrated_agent_remains_unassigned(organization))


async def _racing_department_operation(
    app, barrier: Barrier, path: str, payload: dict[str, str] | None
) -> tuple[int, dict[str, object] | None]:
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client, "owner", "correct horse battery staple")
        barrier.wait()
        response = await client.request("POST" if payload else "DELETE", path, json=payload)
    return response.status_code, response.json() if response.content else None


def _run_racing_department_operation(
    app, barrier: Barrier, path: str, payload: dict[str, str] | None
) -> tuple[int, dict[str, object] | None]:
    return asyncio.run(_racing_department_operation(app, barrier, path, payload))


def test_concurrent_department_delete_and_agent_assignment_leave_no_dangling_membership(
    organization,
):
    settings, _, _, organization_record, agents, host_id = organization
    departments_app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    department = asyncio.run(
        _create_department(departments_app, organization_record.id, "Ephemeral department")
    )
    barrier = Barrier(2)
    agent_path = f"/organizations/{organization_record.id}/agents"
    department_path = f"/organizations/{organization_record.id}/departments/{department['id']}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assignment = executor.submit(
            _run_racing_department_operation,
            departments_app,
            barrier,
            agent_path,
            {
                "name": "Race agent",
                "host_id": host_id,
                "department_id": department["id"],
            },
        )
        deletion = executor.submit(
            _run_racing_department_operation,
            departments_app,
            barrier,
            department_path,
            None,
        )
        assignment_status, assigned = assignment.result()
        deletion_status, _ = deletion.result()

    assert (assignment_status, deletion_status) in {(201, 422), (422, 204)}
    remaining_departments = asyncio.run(_list_departments(departments_app, organization_record.id))
    remaining_agents = [
        agent
        for agent in agents.list_agents(organization_record.id)
        if agent["name"] == "Race agent"
    ]
    if assignment_status == 201:
        assert remaining_departments == [department]
        assert remaining_agents == [assigned]
    else:
        assert remaining_departments == []
        assert remaining_agents == []


async def _create_department(app, organization_id: str, name: str) -> dict[str, str]:
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client, "owner", "correct horse battery staple")
        response = await client.post(
            f"/organizations/{organization_id}/departments", json={"name": name}
        )
    assert response.status_code == 201
    return response.json()


async def _list_departments(app, organization_id: str) -> list[dict[str, str]]:
    async with AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        await _sign_in(client, "owner", "correct horse battery staple")
        response = await client.get(f"/organizations/{organization_id}/departments")
    assert response.status_code == 200
    return response.json()
