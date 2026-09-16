"""Human authorization and exact request scope at the workspace lifecycle boundary."""

import asyncio
import json
from uuid import uuid4

import httpx

from fesnyng_backend import control_host_routes
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.settings import ControlPlaneSessionSettings

ORIGIN = "https://workspace.example"


def test_members_inspect_and_remove_only_organization_workspaces(organization, monkeypatch):
    settings, control, owner, org, agents, host_id = organization
    control.add_member(
        org.id, "member", "Member", "correct horse battery staple", "member", actor_id=owner.id
    )
    other = control.create_organization(owner.id, "Other organization")
    agent = agents.create_agent(org.id, owner.id, {"name": "Engineer", "host_id": host_id})
    agents.set_host_credential(org.id, host_id, "x" * 32)
    calls = []
    stale = False
    expected = {"workspace_id": str(uuid4()), "generation": 1, "safety_digest": "a" * 64}

    def host(request):
        calls.append(request)
        if stale:
            return httpx.Response(409, json={"detail": "Workspace safety changed; inspect again"})
        return httpx.Response(
            200, json={**expected, "state": "ready", "directory": "/managed/thread"}
        )

    monkeypatch.setattr(
        control_host_routes,
        "host_client",
        lambda _: HostClient(agents, transport=httpx.MockTransport(host)),
    )
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=ORIGIN))

    async def exercise():
        nonlocal stale
        path = f"/organizations/{org.id}/agents/{agent['id']}/sessions/thread_alpha/workspace"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            assert (await client.get(path)).status_code == 401
            login = await client.post(
                "/auth/login", json={"login": "member", "password": "correct horse battery staple"}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            read = await client.get(path)
            assert read.status_code == 200
            assert read.json()["workspace_id"] == expected["workspace_id"]
            assert len(calls) == 1
            assert calls[-1].url.path.endswith("/sessions/thread_alpha/workspace")
            client.headers["X-Fesnyng-Actor"] = json.dumps(
                {"kind": "human", "id": str(uuid4()), "name": "Forged administrator"}
            )
            for action in ("remove", "discard", "replace"):
                result = await client.post(f"{path}/{action}", json=expected)
                assert result.status_code == 200
                assert json.loads(calls[-1].content) == expected
                actor = json.loads(calls[-1].headers["X-Fesnyng-Actor"])
                assert actor["kind"] == "human"
                assert actor["name"] == "Member"
            stale = True
            refused = await client.post(f"{path}/remove", json=expected)
            assert refused.status_code == 409
            assert refused.json() == {"detail": "Workspace safety changed; inspect again"}
            count = len(calls)
            assert (await client.get(path.replace(org.id, other.id))).status_code == 404
            assert (await client.get(path.replace(agent["id"], str(uuid4())))).status_code == 404
            assert (
                await client.post(f"{path}/remove", json={**expected, "directory": "/"})
            ).status_code == 422
            del client.headers["X-CSRF-Token"]
            assert (await client.post(f"{path}/remove", json=expected)).status_code == 403
            assert len(calls) == count

    asyncio.run(exercise())


def test_host_workspace_routes_require_bound_org_and_trusted_actor(tmp_path):
    from fesnyng_backend.agent_host import create_app as create_host
    from fesnyng_backend.host_models import HostAgentConfiguration
    from fesnyng_backend.settings import ServiceSettings

    app = create_host(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "host",
        )
    )
    org, agent = str(uuid4()), str(uuid4())
    app.state.host_store.bind_organization(org, "a" * 32)
    app.state.host_store.stage_agent(
        HostAgentConfiguration(
            host_id=app.state.host_store.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name="Legacy engineer",
        )
    )
    app.state.host_store.save_session(org, agent, "thread_alpha", "/workspace/legacy", "Legacy")
    expected = {"workspace_id": str(uuid4()), "generation": 1, "safety_digest": "a" * 64}

    async def exercise():
        path = f"/organizations/{org}/agents/{agent}/sessions/thread_alpha/workspace"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:
            assert (await client.get(path)).status_code == 401
            client.headers["Authorization"] = "Bearer " + "a" * 32
            legacy = await client.get(path)
            assert legacy.status_code == 200
            assert legacy.json()["state"] == "legacy"
            assert legacy.json()["directory"] == "/workspace/legacy"
            assert legacy.json()["repository"]["state"] == "unavailable"
            assert legacy.json()["git"]["state"] == "unavailable"
            assert "ahead" not in legacy.json()["git"]
            assert legacy.json()["history"]["state"] == "unavailable"
            assert not any(action["available"] for action in legacy.json()["cleanup"].values())
            assert (await client.get(path.replace(org, str(uuid4())))).status_code == 403
            assert (await client.post(f"{path}/remove", json=expected)).status_code == 400
            client.headers["X-Fesnyng-Actor"] = "not json"
            assert (await client.post(f"{path}/discard", json=expected)).status_code == 400
            assert (await client.post(f"{path}/replace", json=expected)).status_code == 400

    asyncio.run(exercise())
