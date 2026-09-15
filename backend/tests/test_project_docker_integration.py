"""Opt-in Project grouping proof across real control, host and native boundaries."""

import asyncio
import os
import secrets
import tempfile
from pathlib import Path

import httpx
import pytest

from docker_proof import _defer_proof_interrupts, _dispose_proof, _run_proof
from fesnyng_backend import control_host_routes
from fesnyng_backend.agent_host import create_app as create_host
from fesnyng_backend.agent_models import AgentConfiguration
from fesnyng_backend.control_plane import create_app
from fesnyng_backend.host_client import HostClient
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.settings import ControlPlaneSessionSettings, ServiceSettings


@pytest.mark.skipif(
    os.environ.get("FESNYNG_PROJECT_DOCKER_TESTS") != "true",
    reason="Requires the pinned dual-harness Docker image",
)
@pytest.mark.parametrize("harness", ["opencode", "codex"])
def test_project_lifecycle_preserves_real_native_thread(organization, monkeypatch, harness):
    settings, _control, owner, org, agents, _ = organization
    directory = Path(tempfile.mkdtemp(prefix="fesnyng-project-integration-"))
    host_app = create_host(
        ServiceSettings(
            service="agent-host", state_directory=directory, database_path=directory / "host.db"
        )
    )
    host = host_app.state.host_store
    runtime = host_app.state.host_runtime
    token = secrets.token_urlsafe(32)
    host.bind_organization(org.id, token)
    agents.register_host(str(host.instance_id), "Project proof host", "http://host.test", org.id)
    agents.set_host_credential(org.id, str(host.instance_id), token)
    agent = agents.create_agent(
        org.id,
        owner.id,
        {
            "name": "Project engineer",
            "host_id": str(host.instance_id),
            "configuration": {"runtime_type": harness},
        },
    )
    agent_id = str(agent["id"])
    envelope = HostAgentConfiguration(
        host_id=host.instance_id,
        organization_id=org.id,
        agent_id=agent_id,
        version=1,
        name="Project engineer",
        configuration=AgentConfiguration(runtime_type=harness),
    )
    monkeypatch.setattr(
        control_host_routes,
        "HostClient",
        lambda store: HostClient(store, transport=httpx.ASGITransport(host_app)),
    )
    origin = "https://project-proof.example"
    app = create_app(settings, ControlPlaneSessionSettings(allowed_origin=origin))
    print(f"Project proof: state={directory}, container={runtime.name(agent_id)}", flush=True)

    async def exercise():
        try:
            await host_app.state.host_configuration.apply(envelope)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=origin, headers={"Origin": origin}
            ) as client:
                login = await client.post(
                    "/auth/login",
                    json={"login": "owner", "password": "correct horse battery staple"},
                )
                assert login.status_code == 200
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                projects = f"/organizations/{org.id}/projects"
                created = await client.post(projects, json={"name": "Release"})
                assert created.status_code == 201, created.text
                project = created.json()["id"]
                base = f"/organizations/{org.id}/agents/{agent_id}"
                native = f"{base}/{harness}/session"
                response = await client.post(
                    native, json={"title": "Independent work", "project_id": project}
                )
                assert response.status_code == 201, response.text
                session = response.json()
                thread_id = session["id"]
                grouping = f"{base}/sessions/{thread_id}/project"
                assert (await client.get(grouping)).json() == {"project_id": project}
                raw_inventory = await client.get(f"{base}/sessions")
                assert raw_inventory.status_code == 200, raw_inventory.text
                assert thread_id in [item["session_id"] for item in raw_inventory.json()]
                assert all("fesnyng_project_id" not in item for item in raw_inventory.json())
                grouped = await client.put(grouping, json={"project_id": project})
                assert grouped.status_code == 200, grouped.text
                assert (await client.get(grouping)).json() == {"project_id": project}
                original = host.session(org.id, agent_id, thread_id)
                assert (await client.post(f"{projects}/{project}/archive")).status_code == 200
                assert (await client.get(grouping)).json() == {"project_id": project}
                assert (await client.post(f"{projects}/{project}/restore")).status_code == 200
                assert (await client.delete(f"{projects}/{project}")).status_code == 204
                assert (await client.get(grouping)).json() == {"project_id": None}
                inventory = await client.get(native)
                assert inventory.status_code == 200, inventory.text
                assert thread_id in [item["id"] for item in inventory.json()]
                assert all("fesnyng_project_id" not in item for item in inventory.json())
                retained = host.session(org.id, agent_id, thread_id)
                assert retained["directory"] == original["directory"]
                assert retained["runtime_type"] == harness
                assert retained["frozen_at"] is None
                suffix = "history" if harness == "codex" else "message"
                history = await client.get(f"{native}/{thread_id}/{suffix}")
                assert history.status_code == 200, history.text
        finally:
            await runtime.codex.transport.close()

    async def bounded():
        async with asyncio.timeout(120):
            await exercise()

    verified = False
    interrupts: list[int] = []
    try:
        _run_proof(bounded(), interrupts)
        verified = True
    finally:
        with _defer_proof_interrupts() as deferred:
            cleaned = asyncio.run(
                _dispose_proof(runtime, org.id, agent_id, directory, verified=verified)
            )
        interrupts.extend(deferred)
        if verified and not cleaned:
            raise RuntimeError("Project proof fixture cleanup failed")
        if interrupts:
            raise KeyboardInterrupt("Project proof interrupted")
