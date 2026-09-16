"""Registered resources retain exact engine identity and shared associations."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fesnyng_backend.host_docker_resources import (
    DockerResources,
    ResourceRegistration,
    ResourceUpdate,
)
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import DockerCapabilitySettings, ServiceSettings


class Engine:
    def __init__(self):
        self.containers = {
            "a" * 64: {
                "Id": "a" * 64,
                "Name": "/shared-app",
                "State": {"Status": "running", "Running": True},
                "Config": {"Labels": {}},
                "Mounts": [
                    {"Type": "bind", "Source": "/work/thread", "Destination": "/app", "RW": True}
                ],
                "NetworkSettings": {
                    "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
                    "Networks": {"shared-default": {}},
                },
            }
        }
        self.calls = []

    async def require(self, org, agent_id=None):
        return SimpleNamespace(engine_id="dedicated-engine")

    async def command(self, org, *args):
        self.calls.append(args)
        if args[:2] == ("container", "ls"):
            return "\n".join(self.containers).encode()
        if args[:2] == ("container", "inspect"):
            return json.dumps([self.containers[args[-1]]]).encode()
        if args[0] in {"start", "stop"}:
            self.containers[args[-1]]["State"] = {
                "Status": "running" if args[0] == "start" else "exited",
                "Running": args[0] == "start",
            }
            return b""
        if args[0] == "rm":
            del self.containers[args[-1]]
            return b""
        raise AssertionError(args)


@pytest.fixture
def resource_host(tmp_path):
    host = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "host.db",
            state_directory=tmp_path / "state",
        )
    )
    host.initialize()
    org, agent = str(uuid4()), str(uuid4())
    host.bind_organization(org, "a" * 32)
    host.stage_agent(
        HostAgentConfiguration(
            host_id=host.instance_id,
            organization_id=org,
            agent_id=agent,
            version=1,
            name="Engineer",
        )
    )
    for session in ("first", "second"):
        host.save_session(org, agent, session, f"/workspace/{session}", session)
    engine = Engine()
    service = DockerResources(host, engine)
    service.initialize()
    return host, org, agent, engine, service


def test_shared_resource_registration_survives_restart_without_owning_lifetime(resource_host):
    host, org, agent, engine, service = resource_host

    async def exercise():
        item = await service.register(
            org,
            ResourceRegistration(
                container_id="a" * 64,
                name="Shared app",
                threads=[{"agent_id": agent, "session_id": name} for name in ("first", "second")],
            ),
        )
        assert len(item["threads"]) == 2
        assert item["inspection"]["mounts"][0]["source"] == "/work/thread"
        assert item["inspection"]["ports"][0]["host_port"] == "8080"
        restarted = DockerResources(host, engine)
        restarted.initialize()
        saved = await restarted.inspect(org, item["id"])
        assert saved["threads"] == item["threads"]
        assert saved["container_id"] == "a" * 64
        assert not any(call[0] in {"start", "stop", "rm"} for call in engine.calls)

    asyncio.run(exercise())


def test_explicit_operations_recheck_scope_revision_and_running_state(resource_host):
    _, org, agent, engine, service = resource_host

    async def exercise():
        item = await service.register(
            org,
            ResourceRegistration(
                container_id="a" * 64,
                name="Shared app",
                threads=[
                    {"agent_id": agent, "session_id": "first"},
                    {"agent_id": agent, "session_id": "second"},
                ],
            ),
        )
        with pytest.raises(LookupError):
            await service.operate(str(uuid4()), item["id"], 1, "stop")
        with pytest.raises(ValueError, match="changed"):
            await service.operate(org, item["id"], 2, "stop")
        with pytest.raises(ValueError, match="running"):
            await service.operate(org, item["id"], 1, "remove")
        assert not any(call[0] in {"stop", "rm"} for call in engine.calls)
        stopped = await service.operate(org, item["id"], 1, "stop")
        assert stopped["inspection"]["state"] == "exited"
        removed = await service.operate(org, item["id"], 1, "remove")
        assert removed["inspection"]["status"] == "missing"
        assert len(removed["threads"]) == 2
        assert ("rm", "a" * 64) in engine.calls
        assert await service.operate(org, item["id"], 1, "unregister") == {"removed": True}

    asyncio.run(exercise())


def test_association_updates_validate_threads_and_invalidate_old_actions(resource_host):
    _, org, agent, _, service = resource_host

    async def exercise():
        item = await service.register(
            org, ResourceRegistration(container_id="a" * 64, name="Shared")
        )
        with pytest.raises(LookupError):
            await service.update(
                org,
                item["id"],
                ResourceUpdate(
                    name="Shared",
                    expected_revision=1,
                    threads=[{"agent_id": str(uuid4()), "session_id": "first"}],
                ),
            )
        updated = await service.update(
            org,
            item["id"],
            ResourceUpdate(
                name="Shared by two",
                expected_revision=1,
                threads=[
                    {"agent_id": agent, "session_id": "first"},
                    {"agent_id": agent, "session_id": "second"},
                ],
            ),
        )
        assert updated["revision"] == 2
        with pytest.raises(ValueError, match="changed"):
            await service.operate(org, item["id"], 1, "stop")
        assert len(await service.inventory(org)) == 1

    asyncio.run(exercise())


def test_discovery_excludes_managed_or_foreign_resources_and_returns_no_secrets(resource_host):
    _, org, _, engine, service = resource_host

    async def exercise():
        engine.containers["a" * 64]["Config"]["Env"] = ["SECRET=do-not-expose"]
        discovered = await service.discover(org)
        assert discovered == [
            {
                "container_id": "a" * 64,
                "name": "shared-app",
                "state": "running",
                "compose_project": None,
                "compose_service": None,
            }
        ]
        for labels in ({"fesnyng.host": "a-host"}, {"fesnyng.organization": str(uuid4())}):
            engine.containers["a" * 64]["Config"]["Labels"] = labels
            assert await service.discover(org) == []
            with pytest.raises(PermissionError):
                await service.register(
                    org, ResourceRegistration(container_id="a" * 64, name="Forbidden")
                )

    asyncio.run(exercise())


def test_inspection_redacts_configured_socket_even_with_an_alternate_mount_name(resource_host):
    host, org, agent, engine, service = resource_host
    host.settings = host.settings.model_copy(
        update={
            "docker_capability": DockerCapabilitySettings(
                organization_id=org,
                engine_id="dedicated-engine",
                socket_path="/run/private-engine.sock",
                employee_ids=[agent],
            )
        }
    )
    engine.containers["a" * 64]["Mounts"].append(
        {
            "Type": "bind",
            "Source": "/run/private-engine.sock",
            "Destination": "/run/admin.sock",
            "RW": True,
        }
    )

    async def exercise():
        item = await service.register(
            org, ResourceRegistration(container_id="a" * 64, name="Example")
        )
        assert len(item["inspection"]["mounts"]) == 1
        assert "private-engine.sock" not in json.dumps(item)

    asyncio.run(exercise())
