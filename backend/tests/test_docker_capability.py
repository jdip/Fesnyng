import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fesnyng_backend.host_docker_capability import DockerCapability
from fesnyng_backend.host_models import HostAgentConfiguration
from fesnyng_backend.host_runtime import DockerRuntime, RuntimeUnavailable
from fesnyng_backend.host_store import HostStore
from fesnyng_backend.settings import (
    DockerCapabilitySettings,
    ServiceSettings,
    settings_from_environment,
)


class DockerBoundary:
    def __init__(self, engine_id: str = "engine-a", foreign_organizations: set[str] | None = None):
        self.engine_id = engine_id
        self.foreign_organizations = foreign_organizations or set()
        self.commands: list[tuple[Path, tuple[str, ...]]] = []

    async def docker_engine_id(self, socket_path: Path | None = None) -> str:
        return self.engine_id

    async def docker_capability_command(self, socket_path: Path, *args: str) -> bytes:
        self.commands.append((socket_path, args))
        return b"registered resource"

    async def docker_capability_foreign_organizations(
        self, socket_path: Path, organization_id: str
    ) -> set[str]:
        return self.foreign_organizations - {organization_id}


def _settings(
    tmp_path: Path, capability: DockerCapabilitySettings | None = None
) -> ServiceSettings:
    return ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "host.sqlite3",
        state_directory=tmp_path / "state",
        docker_capability=capability,
    )


def _capability(organization_id: str, employee_id: str) -> DockerCapabilitySettings:
    return DockerCapabilitySettings(
        organization_id=organization_id,
        engine_id="engine-a",
        socket_path=Path("/var/run/docker.sock"),
        employee_ids=[employee_id],
    )


def test_agent_host_reads_an_optional_docker_capability_from_its_environment(tmp_path, monkeypatch):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    monkeypatch.setenv("FESNYNG_AGENT_HOST_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setenv(
        "FESNYNG_AGENT_HOST_DOCKER_CAPABILITY",
        f'{{"organization_id":"{organization_id}","engine_id":"engine-a","socket_path":"/var/run/docker.sock","employee_ids":["{employee_id}"]}}',
    )

    settings = settings_from_environment("agent-host")

    assert settings.docker_capability == _capability(organization_id, employee_id)


@pytest.mark.parametrize(
    "payload",
    [
        {"engine_id": "engine-a", "socket_path": "/var/run/docker.sock"},
        {"organization_id": str(uuid4()), "engine_id": "", "socket_path": "/var/run/docker.sock"},
        {"organization_id": str(uuid4()), "engine_id": "engine-a", "socket_path": "relative.sock"},
    ],
)
def test_docker_capability_configuration_rejects_incomplete_or_unsafe_values(payload):
    with pytest.raises(ValidationError):
        DockerCapabilitySettings.model_validate(payload)


def test_dedicated_docker_capability_refuses_a_second_organization_binding(tmp_path):
    dedicated_organization, employee_id, other_organization = (
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )
    store = HostStore(_settings(tmp_path, _capability(dedicated_organization, employee_id)))
    store.initialize()

    store.bind_organization(dedicated_organization, "a" * 32)
    with pytest.raises(ValueError, match="dedicated Docker capability"):
        store.bind_organization(other_organization, "b" * 32)


def test_dedicated_docker_capability_refuses_an_existing_multi_organization_host(tmp_path):
    dedicated_organization, employee_id, other_organization = (
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )
    database_path, state_directory = tmp_path / "host.sqlite3", tmp_path / "state"
    unconfigured = HostStore(
        ServiceSettings(
            service="agent-host", database_path=database_path, state_directory=state_directory
        )
    )
    unconfigured.initialize()
    unconfigured.bind_organization(dedicated_organization, "a" * 32)
    unconfigured.bind_organization(other_organization, "b" * 32)

    configured = HostStore(
        ServiceSettings(
            service="agent-host",
            database_path=database_path,
            state_directory=state_directory,
            docker_capability=_capability(dedicated_organization, employee_id),
        )
    )

    with pytest.raises(ValueError, match="another organization binding"):
        configured.initialize()


def test_disabled_docker_capability_is_not_available(tmp_path):
    store = HostStore(_settings(tmp_path))
    store.initialize()
    capability = DockerCapability(store, DockerBoundary())

    status = asyncio.run(capability.status(str(uuid4())))

    assert status.model_dump(mode="json") == {
        "enabled": False,
        "available": False,
        "reason": "Docker capability is not configured",
        "engine_id": None,
    }
    with pytest.raises(RuntimeUnavailable, match="not configured"):
        asyncio.run(capability.require(str(uuid4())))


def test_capability_requires_its_dedicated_organization_and_allowed_employee(tmp_path):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    capability = DockerCapability(store, DockerBoundary())

    with pytest.raises(RuntimeUnavailable, match="dedicated organization"):
        asyncio.run(capability.require(str(uuid4())))
    with pytest.raises(RuntimeUnavailable, match="not allowed"):
        asyncio.run(capability.require(organization_id, str(uuid4())))

    assert asyncio.run(capability.require(organization_id, employee_id)).engine_id == "engine-a"


def test_capability_rejects_a_misconfigured_or_cross_organization_engine(tmp_path):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()

    misconfigured = DockerCapability(store, DockerBoundary(engine_id="wrong-engine"))
    assert (
        asyncio.run(misconfigured.status(organization_id)).reason
        == "Configured Docker engine is unavailable"
    )
    with pytest.raises(RuntimeUnavailable, match="engine is unavailable"):
        asyncio.run(misconfigured.require(organization_id))

    foreign = DockerCapability(store, DockerBoundary(foreign_organizations={str(uuid4())}))
    with pytest.raises(RuntimeUnavailable, match="another organization"):
        asyncio.run(foreign.require(organization_id))


def test_runtime_discovers_foreign_fesnyng_resources_from_any_host_on_the_selected_engine(
    tmp_path, monkeypatch
):
    organization_id, foreign_organization = str(uuid4()), str(uuid4())
    runtime = DockerRuntime(HostStore(_settings(tmp_path)), "http://host.invalid")
    calls: list[tuple[str, ...]] = []

    async def docker(_socket_path: Path, *args: str, **_kwargs: object) -> bytes:
        calls.append(args)
        if args[:2] == ("container", "ls"):
            return b"foreign-container\n"
        if args[:2] == ("volume", "ls"):
            return b""
        assert args[:2] == ("container", "inspect")
        return (
            f'{{"fesnyng.host":"another-host","fesnyng.organization":"{foreign_organization}"}}'
        ).encode()

    monkeypatch.setattr(runtime, "_docker", docker)

    foreign = asyncio.run(
        runtime.docker_capability_foreign_organizations(
            Path("/var/run/docker.sock"), organization_id
        )
    )

    assert foreign == {foreign_organization}
    assert (
        "container",
        "ls",
        "-a",
        "--filter",
        "label=fesnyng.host",
        "--format",
        "{{.Names}}",
    ) in calls


def test_runtime_discovers_a_foreign_organization_labeled_resource_without_a_host_label(
    tmp_path, monkeypatch
):
    organization_id, foreign_organization = str(uuid4()), str(uuid4())
    runtime = DockerRuntime(HostStore(_settings(tmp_path)), "http://host.invalid")

    async def docker(_socket_path: Path, *args: str, **_kwargs: object) -> bytes:
        if args[:2] == ("container", "ls"):
            return b"foreign-org-only\n" if "label=fesnyng.organization" in args else b""
        if args[:2] == ("volume", "ls"):
            return b""
        assert args[:2] == ("container", "inspect")
        return f'{{"fesnyng.organization":"{foreign_organization}"}}'.encode()

    monkeypatch.setattr(runtime, "_docker", docker)

    assert asyncio.run(
        runtime.docker_capability_foreign_organizations(
            Path("/var/run/docker.sock"), organization_id
        )
    ) == {foreign_organization}


def test_existing_employee_rebuild_accepts_its_unlabeled_legacy_retained_volumes(
    tmp_path, monkeypatch
):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    store.bind_organization(organization_id, "a" * 32)
    store.stage_agent(
        HostAgentConfiguration(
            host_id=store.instance_id,
            organization_id=organization_id,
            agent_id=employee_id,
            version=1,
            name="Employee",
        )
    )
    runtime = DockerRuntime(store, "http://host.invalid")
    home_volume = f"{runtime.name(employee_id)}-home"
    workspace_volume = f"{runtime.name(employee_id)}-workspace"
    commands: list[tuple[str, ...]] = []

    async def runtime_docker(*args: str, **_kwargs: object) -> bytes:
        commands.append(args)
        if args[:2] == ("volume", "ls"):
            return f"{home_volume}\n{workspace_volume}\n".encode()
        if args[:2] == ("volume", "inspect"):
            return (
                f'{{"fesnyng.host":"{store.instance_id}","fesnyng.agent":"{employee_id}"}}'.encode()
            )
        return b""

    async def selected_engine(_socket_path: Path, *args: str, **_kwargs: object) -> bytes:
        if args[0] == "info":
            return b"engine-a\n"
        if args[:2] == ("container", "ls"):
            return b""
        if args[:2] == ("volume", "ls"):
            return f"{home_volume}\n{workspace_volume}\n".encode()
        assert args[:2] == ("volume", "inspect")
        return f'{{"fesnyng.host":"{store.instance_id}","fesnyng.agent":"{employee_id}"}}'.encode()

    monkeypatch.setattr(runtime, "docker", runtime_docker)
    monkeypatch.setattr(runtime, "_docker", selected_engine)

    asyncio.run(
        runtime._create_container(organization_id, employee_id, "image", require_volumes=True)
    )

    assert any(command[:2] == ("run", "-d") for command in commands)
    assert not any(command[:2] == ("volume", "create") for command in commands)


def test_runtime_refuses_an_unattributed_volume_that_is_not_a_legacy_employee_volume(
    tmp_path, monkeypatch
):
    organization_id = str(uuid4())
    runtime = DockerRuntime(HostStore(_settings(tmp_path)), "http://host.invalid")

    async def selected_engine(_socket_path: Path, *args: str, **_kwargs: object) -> bytes:
        if args[:2] == ("container", "ls"):
            return b""
        if args[:2] == ("volume", "ls"):
            return b"unknown-volume\n"
        assert args[:2] == ("volume", "inspect")
        return b'{"fesnyng.host":"another-host"}'

    monkeypatch.setattr(runtime, "_docker", selected_engine)

    assert asyncio.run(
        runtime.docker_capability_foreign_organizations(
            Path("/var/run/docker.sock"), organization_id
        )
    ) == {"unattributed"}


def test_capability_commands_use_the_configured_socket_after_each_admission(tmp_path):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    boundary = DockerBoundary()
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    capability = DockerCapability(store, boundary)

    result = asyncio.run(capability.command(organization_id, "container", "ls", "--all"))

    assert result == b"registered resource"
    assert boundary.commands == [(Path("/var/run/docker.sock"), ("container", "ls", "--all"))]


def test_runtime_docker_capability_command_cannot_override_the_configured_endpoint(tmp_path):
    runtime = DockerRuntime(HostStore(_settings(tmp_path)), "http://host.invalid")

    with pytest.raises(RuntimeUnavailable, match="cannot change its engine endpoint"):
        asyncio.run(
            runtime.docker_capability_command(
                Path("/var/run/docker.sock"), "container", "ls", "--host=tcp://untrusted"
            )
        )


def test_allowed_employee_container_gets_only_the_configured_external_socket_mount(
    tmp_path, monkeypatch
):
    organization_id, allowed_employee, other_employee = str(uuid4()), str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, allowed_employee)))
    store.initialize()
    store.bind_organization(organization_id, "a" * 32)
    for employee_id in (allowed_employee, other_employee):
        store.stage_agent(
            HostAgentConfiguration(
                host_id=store.instance_id,
                organization_id=organization_id,
                agent_id=employee_id,
                version=1,
                name="Employee",
            )
        )
    runtime = DockerRuntime(store, "http://host.invalid")
    calls: list[tuple[str, ...]] = []

    async def docker(*args: str, **_kwargs: object) -> bytes:
        calls.append(args)
        return b""

    async def engine_id(_socket_path: Path | None = None) -> str:
        return "engine-a"

    async def foreign(_socket_path: Path, _organization_id: str) -> set[str]:
        return set()

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "docker_engine_id", engine_id)
    monkeypatch.setattr(runtime, "docker_capability_foreign_organizations", foreign)

    asyncio.run(
        runtime._create_container(organization_id, allowed_employee, "image", require_volumes=False)
    )
    allowed_run = calls[-1]
    mounts = [allowed_run[index + 1] for index, value in enumerate(allowed_run) if value == "-v"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in mounts
    assert "DOCKER_HOST=unix:///var/run/docker.sock" in allowed_run

    calls.clear()
    asyncio.run(
        runtime._create_container(organization_id, other_employee, "image", require_volumes=False)
    )
    assert "/var/run/docker.sock:/var/run/docker.sock" not in calls[-1]


def test_misconfigured_engine_refuses_before_employee_container_resources_are_created(
    tmp_path, monkeypatch
):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    store.bind_organization(organization_id, "a" * 32)
    store.stage_agent(
        HostAgentConfiguration(
            host_id=store.instance_id,
            organization_id=organization_id,
            agent_id=employee_id,
            version=1,
            name="Employee",
        )
    )
    runtime = DockerRuntime(store, "http://host.invalid")
    calls: list[tuple[str, ...]] = []

    async def docker(*args: str, **_kwargs: object) -> bytes:
        calls.append(args)
        return b""

    async def engine_id(_socket_path: Path | None = None) -> str:
        return "wrong-engine"

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "docker_engine_id", engine_id)

    with pytest.raises(RuntimeUnavailable, match="engine is unavailable"):
        asyncio.run(
            runtime._create_container(organization_id, employee_id, "image", require_volumes=False)
        )
    assert calls == []


def test_native_access_refuses_an_allowed_employee_container_without_the_socket_mount(
    tmp_path, monkeypatch
):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    runtime = DockerRuntime(store, "http://host.invalid")

    async def inspect(_organization_id: str, _employee_id: str):
        return {
            "state": {"Running": True},
            "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4096"}]},
            "mounts": [],
        }

    monkeypatch.setattr(runtime, "inspect", inspect)

    with pytest.raises(RuntimeUnavailable, match="explicit rebuild flow"):
        asyncio.run(runtime.running_port(organization_id, employee_id))


def test_native_request_rechecks_the_engine_for_an_existing_allowed_socket_mount(
    tmp_path, monkeypatch
):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, employee_id)))
    store.initialize()
    store.bind_organization(organization_id, "a" * 32)
    store.stage_agent(
        HostAgentConfiguration(
            host_id=store.instance_id,
            organization_id=organization_id,
            agent_id=employee_id,
            version=1,
            name="Employee",
        )
    )
    runtime = DockerRuntime(store, "http://host.invalid")

    async def inspect(_organization_id: str, _employee_id: str):
        return {
            "state": {"Running": True},
            "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4096"}]},
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/var/run/docker.sock",
                    "Destination": "/var/run/docker.sock",
                }
            ],
        }

    async def engine_id(_socket_path: Path | None = None) -> str:
        return "changed-engine"

    monkeypatch.setattr(runtime, "inspect", inspect)
    monkeypatch.setattr(runtime, "docker_engine_id", engine_id)

    with pytest.raises(RuntimeUnavailable, match="Configured Docker engine is unavailable"):
        asyncio.run(runtime.request(organization_id, employee_id, "/global/health"))


def test_native_access_refuses_an_unallowed_employee_container_with_a_socket_mount(
    tmp_path, monkeypatch
):
    organization_id, allowed_employee, other_employee = str(uuid4()), str(uuid4()), str(uuid4())
    store = HostStore(_settings(tmp_path, _capability(organization_id, allowed_employee)))
    store.initialize()
    runtime = DockerRuntime(store, "http://host.invalid")

    async def inspect(_organization_id: str, _employee_id: str):
        return {
            "state": {"Running": True},
            "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4096"}]},
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/var/run/docker.sock",
                    "Destination": "/var/run/docker.sock",
                }
            ],
        }

    monkeypatch.setattr(runtime, "inspect", inspect)

    with pytest.raises(RuntimeUnavailable, match="unapproved Docker socket mount"):
        asyncio.run(runtime.running_port(organization_id, other_employee))


def test_native_access_refuses_a_socket_left_mounted_after_capability_configuration_is_removed(
    tmp_path, monkeypatch
):
    organization_id, employee_id = str(uuid4()), str(uuid4())
    runtime = DockerRuntime(HostStore(_settings(tmp_path)), "http://host.invalid")

    async def inspect(_organization_id: str, _employee_id: str):
        return {
            "state": {"Running": True},
            "ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4096"}]},
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/var/run/docker.sock",
                    "Destination": "/var/run/docker.sock",
                }
            ],
        }

    monkeypatch.setattr(runtime, "inspect", inspect)

    with pytest.raises(RuntimeUnavailable, match="configured Docker capability is removed"):
        asyncio.run(runtime.running_port(organization_id, employee_id))
