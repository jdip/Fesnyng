"""Public control-plane behavior for organization Projects and thread grouping."""

import asyncio
import json
from uuid import uuid4

import httpx

from fesnyng_backend import (
    control_codex_routes,
    control_host_routes,
    control_workspace_routes,
)
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://projects.example"


async def _sign_in(client: httpx.AsyncClient, login: str, password: str) -> None:
    response = await client.post("/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def test_members_can_group_accessible_threads_and_managers_control_project_lifecycle(
    organization, monkeypatch
):
    settings, control, owner, org, agents, host_id = organization
    member = control.add_member(
        org.id,
        "member",
        "Member",
        "correct horse battery staple",
        "member",
        actor_id=owner.id,
    )
    other = control.create_organization(owner.id, "Other organization")
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    agents.set_host_credential(org.id, host_id, "x" * 32)
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    def host(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path.endswith("/project-provenance")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        control_host_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(host)),
    )

    async def exercise() -> None:
        project_path = f"/organizations/{org.id}/projects"
        session_path = f"/organizations/{org.id}/agents/{agent['id']}/sessions/thread_alpha/project"
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as owner_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as member_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            ) as anonymous,
        ):
            assert (await anonymous.get(project_path)).status_code == 401
            await _sign_in(owner_client, "owner", "correct horse battery staple")
            await _sign_in(member_client, "member", "correct horse battery staple")

            invalid = await owner_client.post(
                project_path,
                json={
                    "name": "Missing branch",
                    "target_repository_url": "https://example.test/repo.git",
                },
            )
            assert invalid.status_code == 422
            credentialed_url = await owner_client.post(
                project_path,
                json={
                    "name": "Credentialed origin",
                    "target_repository_url": "https://token@example.test/repo.git",
                    "default_checkout_branch": "test",
                },
            )
            assert credentialed_url.status_code == 422

            created = await member_client.post(
                project_path,
                json={
                    "name": "Launch",
                    "description": "A focused release",
                    "target_repository_url": "https://example.test/launch.git",
                    "default_checkout_branch": "test",
                },
            )
            assert created.status_code == 201
            project = created.json()
            assert project == {
                "id": project["id"],
                "organization_id": org.id,
                "name": "Launch",
                "description": "A focused release",
                "target_repository_url": "https://example.test/launch.git",
                "default_checkout_branch": "test",
                "archived": False,
            }
            assert (await member_client.get(project_path)).json() == [project]

            grouped = await member_client.put(session_path, json={"project_id": project["id"]})
            assert grouped.status_code == 200
            assert grouped.json() == {"project_id": project["id"]}
            assert (await owner_client.get(session_path)).json() == {"project_id": project["id"]}
            assert (
                await owner_client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/thread-projects"
                )
            ).json() == {"threads": [{"session_id": "thread_alpha", "project_id": project["id"]}]}

            assert (
                await member_client.post(f"{project_path}/{project['id']}/archive")
            ).status_code == 403
            archived = await owner_client.post(f"{project_path}/{project['id']}/archive")
            assert archived.status_code == 200
            assert archived.json()["archived"] is True
            assert (
                await member_client.put(session_path, json={"project_id": project["id"]})
            ).status_code == 422
            assert (await owner_client.get(session_path)).json() == {"project_id": project["id"]}

            restored = await owner_client.post(f"{project_path}/{project['id']}/restore")
            assert restored.status_code == 200
            assert restored.json()["archived"] is False
            assert (await owner_client.delete(f"{project_path}/{project['id']}")).status_code == 204
            assert (await owner_client.get(session_path)).json() == {"project_id": None}

            assert (
                await owner_client.get(f"/organizations/{other.id}/projects/{project['id']}")
            ).status_code == 404
            assert (
                await owner_client.put(
                    f"/organizations/{other.id}/agents/{uuid4()}/sessions/thread_alpha/project",
                    json={"project_id": None},
                )
            ).status_code == 404

    asyncio.run(exercise())
    control.remove_member(org.id, member.user_id, actor_id=owner.id)


def test_native_session_creation_and_inventory_keep_project_grouping_control_plane_owned(
    organization, monkeypatch
):
    settings, _control, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    agents.set_host_credential(org.id, host_id, "x" * 32)
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    forwarded_bodies: list[dict[str, object]] = []

    def host(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert request.url.path.endswith("/project-provenance")
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            forwarded_bodies.append(json.loads(request.content))
            if "/opencode/" in request.url.path:
                return httpx.Response(200, json={"id": "session_new"})
            return httpx.Response(200, json={"id": "thread_new"})
        if request.method == "GET":
            if request.url.path.endswith(f"/agents/{agent['id']}/sessions"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "session_id": "session_inventory",
                            "fesnyng_project_id": project_id,
                            "project_provenance_initialized": True,
                        }
                    ],
                )
            if "/opencode/" in request.url.path:
                return httpx.Response(
                    200,
                    json=[{"id": "session_peer", "fesnyng_project_id": project_id}],
                )
            return httpx.Response(
                200,
                json=[{"id": "thread_peer", "fesnyng_project_id": project_id}],
            )
        raise AssertionError((request.method, request.url.path))

    client_factory = lambda _: HostClient(agents, transport=httpx.MockTransport(host))
    monkeypatch.setattr(control_host_routes, "host_client", client_factory)
    monkeypatch.setattr(control_workspace_routes, "host_client", client_factory)
    monkeypatch.setattr(control_codex_routes, "host_client", client_factory)

    async def exercise() -> None:
        nonlocal project_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            project = await client.post(
                f"/organizations/{org.id}/projects", json={"name": "Launch"}
            )
            assert project.status_code == 201
            project_id = project.json()["id"]
            opencode = f"/organizations/{org.id}/agents/{agent['id']}/opencode/session"
            codex = f"/organizations/{org.id}/agents/{agent['id']}/codex/session"
            assert (await client.post(opencode, json={"project_id": project_id})).json() == {
                "id": "session_new"
            }
            assert (await client.post(codex, json={"project_id": project_id})).json() == {
                "id": "thread_new"
            }
            assert forwarded_bodies == [{}, {}]
            assert (await client.get(opencode)).json() == [{"id": "session_peer"}]
            assert (await client.get(codex)).json() == [{"id": "thread_peer"}]
            assert (
                await client.get(f"/organizations/{org.id}/agents/{agent['id']}/sessions")
            ).json() == [{"session_id": "session_inventory"}]
            grouped = await client.get(
                f"/organizations/{org.id}/agents/{agent['id']}/thread-projects"
            )
            assert grouped.json() == {
                "threads": [
                    {"session_id": "session_inventory", "project_id": project_id},
                    {"session_id": "session_new", "project_id": project_id},
                    {"session_id": "session_peer", "project_id": project_id},
                    {"session_id": "thread_new", "project_id": project_id},
                    {"session_id": "thread_peer", "project_id": project_id},
                ]
            }

    project_id = ""
    asyncio.run(exercise())


def test_native_creation_remains_usable_when_grouping_is_pending_and_blocks_repo_projects(
    organization, monkeypatch
):
    settings, _control, owner, org, agents, host_id = organization
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    agents.set_host_credential(org.id, host_id, "x" * 32)
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))
    native_creates = 0

    def host(request: httpx.Request) -> httpx.Response:
        nonlocal native_creates
        if request.method == "PUT":
            return httpx.Response(503)
        if request.method == "POST":
            native_creates += 1
            return httpx.Response(200, json={"id": "session_pending"})
        raise AssertionError((request.method, request.url.path))

    client_factory = lambda _: HostClient(agents, transport=httpx.MockTransport(host))
    monkeypatch.setattr(control_host_routes, "host_client", client_factory)
    monkeypatch.setattr(control_workspace_routes, "host_client", client_factory)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            await _sign_in(client, "owner", "correct horse battery staple")
            ordinary = await client.post(
                f"/organizations/{org.id}/projects", json={"name": "Launch"}
            )
            project_id = ordinary.json()["id"]
            created = await client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/session",
                json={"project_id": project_id},
            )
            assert created.status_code == 200
            assert created.json()["id"] == "session_pending"
            assert created.json()["fesnyng_project_grouping"] == {
                "state": "ungrouped",
                "requested_project_id": project_id,
                "retry_path": (
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/session_pending/project"
                ),
                "detail": "Agent host operation failed (HTTP 503)",
            }
            assert (
                await client.get(
                    f"/organizations/{org.id}/agents/{agent['id']}/sessions/session_pending/project"
                )
            ).json() == {"project_id": None}

            repository = await client.post(
                f"/organizations/{org.id}/projects",
                json={
                    "name": "Repository launch",
                    "target_repository_url": "https://example.test/launch.git",
                    "default_checkout_branch": "test",
                },
            )
            blocked = await client.post(
                f"/organizations/{org.id}/agents/{agent['id']}/opencode/session",
                json={"project_id": repository.json()["id"]},
            )
            assert blocked.status_code == 409

    asyncio.run(exercise())
    assert native_creates == 1
