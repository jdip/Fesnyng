import asyncio
from pathlib import Path

import httpx
import pytest

from fesnyng_backend.agent_host import create_app
from fesnyng_backend.control_plane import create_app as create_control_plane
from fesnyng_backend.settings import ServiceSettings


def get_health(app):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    return asyncio.run(request())


def test_agent_host_uses_independent_durable_state(tmp_path: Path):
    control_plane = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control-plane.sqlite3",
        state_directory=tmp_path / "control-plane-state",
    )
    agent_host = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "agent-host.sqlite3",
        state_directory=tmp_path / "agent-host-state",
    )

    control_health = get_health(create_control_plane(control_plane)).json()
    host_health = get_health(create_app(agent_host)).json()

    assert host_health["service"] == "agent-host"
    assert host_health["schema_version"] == 1
    assert host_health["instance_id"] != control_health["instance_id"]
    assert agent_host.database_path.exists()
    assert agent_host.state_directory.is_dir()


def test_agent_host_preserves_a_configured_identity(tmp_path: Path):
    configured_id = "123e4567-e89b-12d3-a456-426614174000"
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "agent-host.sqlite3",
        state_directory=tmp_path / "agent-host-state",
        instance_id=configured_id,
    )

    health = get_health(create_app(settings))

    assert health.json()["instance_id"] == configured_id


def test_agent_host_rejects_control_plane_settings(tmp_path: Path):
    control_plane = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control-plane.sqlite3",
        state_directory=tmp_path / "control-plane-state",
    )

    with pytest.raises(ValueError, match="agent-host settings"):
        create_app(control_plane)


def test_agent_host_restricts_local_state_permissions(tmp_path: Path):
    settings = ServiceSettings(
        service="agent-host",
        database_path=tmp_path / "agent-host-state" / "fesnyng.sqlite3",
        state_directory=tmp_path / "agent-host-state",
    )

    create_app(settings)

    assert settings.state_directory.stat().st_mode & 0o777 == 0o700
    assert settings.database_path.stat().st_mode & 0o777 == 0o600


def test_agent_host_rejects_preexisting_public_state_directory(tmp_path: Path):
    state_directory = tmp_path / "shared-state"
    state_directory.mkdir(mode=0o755)
    state_directory.chmod(0o755)
    settings = ServiceSettings(
        service="agent-host",
        database_path=state_directory / "fesnyng.sqlite3",
        state_directory=state_directory,
    )

    with pytest.raises(ValueError, match="state directory must be private"):
        create_app(settings)
