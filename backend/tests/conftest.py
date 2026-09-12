from pathlib import Path
from uuid import uuid4

import pytest

from fesnyng_backend.agent_storage import AgentStore
from fesnyng_backend.control_store import ControlPlaneStore
from fesnyng_backend.settings import ServiceSettings
from fesnyng_backend.storage import initialize_service_state


@pytest.fixture
def organization(tmp_path: Path):
    settings = ServiceSettings(
        service="control-plane",
        database_path=tmp_path / "control.sqlite3",
        state_directory=tmp_path / "state",
    )
    initialize_service_state(settings)
    control = ControlPlaneStore(settings.database_path)
    control.initialize()
    owner = control.bootstrap_owner("owner", "Owner", "correct horse battery staple")
    org = control.create_organization(owner.id, "Example organization")
    agents = AgentStore(control)
    host_id = str(uuid4())
    agents.register_host(host_id, "Local host", "http://127.0.0.1:8001", org.id)
    return settings, control, owner, org, agents, host_id
