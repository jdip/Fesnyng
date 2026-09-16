"""Service links preserve target identity without claiming network reachability."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from fesnyng_backend.host_docker_resources import DockerResources
from fesnyng_backend.host_docker_services import (
    ConfiguredTailscaleServe,
    DockerServices,
    ServiceRegistration,
    ServiceUpdate,
)
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import ServiceSettings, TailscaleServeSettings


class Engine:
    async def require(self, org, agent_id=None):
        return SimpleNamespace(engine_id="engine")

    async def command(self, org, *args):
        if args[:2] == ("container", "ls"):
            return b""
        raise AssertionError(args)


class Tailscale:
    def __init__(self, result):
        self.result = result

    async def status(self, organization_id, endpoint_url):
        return self.result


class Runtime:
    def __init__(self):
        self.containers = {}
        self.failure = None

    async def inspect(self, organization_id, agent_id):
        if self.failure:
            raise RuntimeUnavailable(self.failure)
        return self.containers.get(agent_id)


@pytest.fixture
def services(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    host.initialize()
    org, agent, other = str(uuid4()), str(uuid4()), str(uuid4())
    host.bind_organization(org, "a" * 32)
    for identity, name in ((agent, "Engineer"), (other, "Designer")):
        host.stage_agent(
            HostAgentConfiguration(
                host_id=host.instance_id,
                organization_id=org,
                agent_id=identity,
                version=1,
                name=name,
            )
        )
        host.save_session(org, identity, f"thread-{name}", "/work", name)
    resources = DockerResources(host, Engine())
    resources.initialize()
    runtime = Runtime()
    runtime.containers[agent] = {"state": {"Running": False}}
    runtime.containers[other] = {"state": {"Running": False}}
    service = DockerServices(host, resources, runtime, Tailscale({"status": "configured"}))
    service.initialize()
    return host, org, agent, other, runtime, service


def test_employee_service_registers_when_raw_docker_is_disabled_and_unregistration_is_metadata_only(
    services,
):
    host, org, agent, _, _, service = services

    async def exercise():
        item = await service.register(
            org,
            ServiceRegistration(
                name="Preview",
                target_kind="employee",
                target_id=agent,
                endpoint_url="http://127.0.0.1:8081",
                route="custom",
                threads=[{"agent_id": agent, "session_id": "thread-Engineer"}],
            ),
        )
        assert item["target"] == {
            "kind": "employee",
            "id": agent,
            "status": "available",
            "running": False,
        }
        assert item["route_status"] == {
            "status": "configured",
            "reason": "Custom route is registered; network reachability is not checked",
            "network_reachability": "unverified",
        }
        assert await service.unregister(org, item["id"], item["revision"]) == {"removed": True}
        assert host.agent(org, agent)["agent_id"] == agent

    asyncio.run(exercise())


def test_services_preserve_missing_targets_and_only_validate_new_associations(services):
    host, org, agent, other, _, service = services

    async def exercise():
        resource_id = str(uuid4())
        with host.connect() as connection:
            connection.execute(
                "INSERT INTO host_docker_resources VALUES(?,?,?,?,?,?,?,1)",
                (resource_id, org, "engine", "a" * 64, "Retained", "[]", "[]"),
            )
        item = await service.register(
            org,
            ServiceRegistration(
                name="Preview",
                target_kind="resource",
                target_id=resource_id,
                endpoint_url="https://preview.tailnet.ts.net/app",
                route="tailscale",
                threads=[{"agent_id": agent, "session_id": "thread-Engineer"}],
            ),
        )
        with host.connect() as connection:
            connection.execute(
                "UPDATE host_sessions SET deleted_at=1 WHERE session_id='thread-Engineer'"
            )
        changed = await service.update(
            org,
            item["id"],
            ServiceUpdate(
                name="Shared preview",
                target_kind="resource",
                target_id=resource_id,
                endpoint_url="https://preview.tailnet.ts.net/app",
                route="tailscale",
                threads=[
                    {"agent_id": agent, "session_id": "thread-Engineer"},
                    {"agent_id": other, "session_id": "thread-Designer"},
                ],
                expected_revision=1,
            ),
        )
        assert len(changed["threads"]) == 2
        with host.connect() as connection:
            connection.execute("DELETE FROM host_docker_resources WHERE id=?", (resource_id,))
        retained = await service.inspect(org, item["id"])
        assert retained["target"] == {"kind": "resource", "id": resource_id, "status": "missing"}
        with pytest.raises(ValueError, match="changed"):
            await service.unregister(org, item["id"], 1)

    asyncio.run(exercise())


def test_retained_missing_targets_remain_editable_but_replacement_is_validated(services):
    host, org, agent, _, _, service = services

    async def exercise():
        retained_employee, replacement = str(uuid4()), str(uuid4())
        service_id = str(uuid4())
        with host.connect() as connection:
            connection.execute(
                "INSERT INTO host_docker_services VALUES(?,?,?,?,?,?,?,?,?,1)",
                (
                    service_id,
                    org,
                    "Retained",
                    "employee",
                    retained_employee,
                    "http://127.0.0.1:8081",
                    "custom",
                    "[]",
                    "[]",
                ),
            )
        updated = await service.update(
            org,
            service_id,
            ServiceUpdate(
                name="Detached retained service",
                target_kind="employee",
                target_id=retained_employee,
                endpoint_url="http://127.0.0.1:8081",
                route="custom",
                expected_revision=1,
            ),
        )
        assert updated["target"] == {
            "kind": "employee",
            "id": retained_employee,
            "status": "missing",
        }
        retained_resource = str(uuid4())
        resource_service_id = str(uuid4())
        with host.connect() as connection:
            connection.execute(
                "INSERT INTO host_docker_services VALUES(?,?,?,?,?,?,?,?,?,1)",
                (
                    resource_service_id,
                    org,
                    "Retained resource",
                    "resource",
                    retained_resource,
                    "http://127.0.0.1:8081",
                    "custom",
                    "[]",
                    "[]",
                ),
            )
        retained_resource_update = await service.update(
            org,
            resource_service_id,
            ServiceUpdate(
                name="Detached retained resource",
                target_kind="resource",
                target_id=retained_resource,
                endpoint_url="http://127.0.0.1:8081",
                route="custom",
                expected_revision=1,
            ),
        )
        assert retained_resource_update["target"]["status"] == "missing"
        with pytest.raises(LookupError):
            await service.update(
                org,
                service_id,
                ServiceUpdate(
                    name="Invalid replacement",
                    target_kind="employee",
                    target_id=replacement,
                    endpoint_url="http://127.0.0.1:8081",
                    route="custom",
                    expected_revision=2,
                ),
            )
        with pytest.raises(LookupError):
            await service.update(
                org,
                service_id,
                ServiceUpdate(
                    name="Invalid resource replacement",
                    target_kind="resource",
                    target_id=uuid4(),
                    endpoint_url="http://127.0.0.1:8081",
                    route="custom",
                    expected_revision=2,
                ),
            )
        assert host.agent(org, agent)["agent_id"] == agent

    asyncio.run(exercise())


def test_employee_target_uses_current_container_inspection_not_stale_store_state(services):
    _, org, agent, _, runtime, service = services

    async def exercise():
        item = await service.register(
            org,
            ServiceRegistration(
                name="Preview",
                target_kind="employee",
                target_id=agent,
                endpoint_url="http://127.0.0.1:8081",
                route="custom",
            ),
        )
        runtime.containers[agent] = {"state": {"Running": True}}
        assert (await service.inspect(org, item["id"]))["target"]["running"] is True
        runtime.containers[agent] = {"state": {"Running": False}}
        assert (await service.inspect(org, item["id"]))["target"]["running"] is False
        runtime.containers.pop(agent)
        assert (await service.inspect(org, item["id"]))["target"]["status"] == "missing"
        runtime.failure = "Docker operation unavailable; check host operations"
        unavailable = await service.inspect(org, item["id"])
        assert unavailable["target"] == {
            "kind": "employee",
            "id": agent,
            "status": "unavailable",
            "reason": "Docker operation unavailable; check host operations",
        }

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "endpoint",
    [
        "unix:///var/run/docker.sock",
        "ftp://service.example",
        "http://user:secret@example.com",
        "http://service.example:2375",
        "https://service.example:2376",
        "http://service.example:4096",
    ],
)
def test_service_endpoints_reject_unsafe_or_native_destinations(endpoint):
    with pytest.raises(ValueError):
        ServiceRegistration(
            name="Unsafe",
            target_kind="employee",
            target_id=uuid4(),
            endpoint_url=endpoint,
            route="custom",
        )


def test_tailscale_status_requires_matching_private_route_and_scope(monkeypatch):
    settings = TailscaleServeSettings(organization_id=uuid4(), command=("tailscale",))
    status = ConfiguredTailscaleServe(settings)

    async def exercise():
        assert (await status.status(str(uuid4()), "https://service.tailnet.ts.net/app"))[
            "status"
        ] == "unavailable"

    asyncio.run(exercise())


def test_tailscale_status_matches_browser_paths_and_rejects_funnel_or_protocol(monkeypatch):
    org = uuid4()
    settings = TailscaleServeSettings(organization_id=org, command=("tailscale",))
    status = ConfiguredTailscaleServe(settings)
    payload: dict[str, Any] = {
        "Web": {"service.tailnet.ts.net:443": {"Handlers": {"/app/": {}}}},
        "TCP": {"443": {"HTTPS": True}},
    }

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(
        "fesnyng_backend.host_docker_services.asyncio.create_subprocess_exec", spawn
    )

    async def exercise():
        assert ConfiguredTailscaleServe._matching_handler({"/": {}}, "//child") is True
        for endpoint in (
            "https://service.tailnet.ts.net/app",
            "https://service.tailnet.ts.net/app/child",
            "https://service.tailnet.ts.net/%61pp/child",
            "https://service.tailnet.ts.net//app",
        ):
            assert (await status.status(str(org), endpoint))["status"] == "configured"
        assert (await status.status(str(org), "https://service.tailnet.ts.net/app/../not-served"))[
            "status"
        ] == "unavailable"
        assert (await status.status(str(org), "http://service.tailnet.ts.net:443/app"))[
            "status"
        ] == "unavailable"
        payload["TCP"]["443"] = {"HTTP": True}
        assert (await status.status(str(org), "http://service.tailnet.ts.net:443/app"))[
            "status"
        ] == "configured"
        payload["TCP"]["443"] = {"HTTP": True, "HTTPS": True}
        assert (await status.status(str(org), "http://service.tailnet.ts.net:443/app"))[
            "status"
        ] == "unavailable"
        payload["TCP"]["443"] = {"HTTPS": True}
        payload["AllowFunnel"] = {"service.tailnet.ts.net:443": True}
        assert (await status.status(str(org), "https://service.tailnet.ts.net/app"))[
            "status"
        ] == "unavailable"

    asyncio.run(exercise())
