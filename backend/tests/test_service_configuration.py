from pathlib import Path

import pytest

from fesnyng_backend.agent_host import create_app as create_agent_host
from fesnyng_backend.control_plane import create_app as create_control_plane
from fesnyng_backend.settings import ServiceSettings


def test_service_rejects_database_reused_by_another_service(tmp_path: Path):
    database_path = tmp_path / "shared.sqlite3"
    control_plane = ServiceSettings(
        service="control-plane",
        database_path=database_path,
        state_directory=tmp_path / "control-plane-state",
    )
    agent_host = ServiceSettings(
        service="agent-host",
        database_path=database_path,
        state_directory=tmp_path / "agent-host-state",
    )

    create_control_plane(control_plane)

    with pytest.raises(ValueError, match="owned by control-plane"):
        create_agent_host(agent_host)


def test_service_rejects_database_path_equal_to_state_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="database path must not equal the state directory"):
        ServiceSettings(
            service="agent-host",
            database_path=tmp_path / "state",
            state_directory=tmp_path / "state",
        )


def test_settings_default_to_the_ignored_service_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FESNYNG_CONTROL_PLANE_STATE_DIRECTORY", raising=False)
    monkeypatch.delenv("FESNYNG_CONTROL_PLANE_DATABASE_PATH", raising=False)

    from fesnyng_backend.settings import settings_from_environment

    settings = settings_from_environment("control-plane")

    assert settings.state_directory == Path(".fesnyng/control-plane")
    assert settings.database_path == Path(".fesnyng/control-plane/fesnyng.sqlite3")
